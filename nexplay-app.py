"""
NexPlay — app.py
================
Complete NexPlay backend in one file.
Combines the Flask API server + game processor pipeline.

WHAT THIS FILE DOES:
  - User registration and login
  - Game library (upload, track, delete)
  - Game processing (extract zip/7z/rar/exe, find correct exe, register with Sunshine)
  - GPU session management via Vast.ai
  - MTN / Airtel MoMo billing
  - Admin dashboard
  - Instance setup script generator

INSTALL (run once):
  pip install flask flask-cors requests python-dotenv waitress py7zr rarfile

RUN LOCALLY:
  python app.py

DEPLOY ON RENDER:
  1. Push this file + requirements.txt to GitHub
  2. New Web Service on Render
  3. Build command:  pip install -r requirements.txt
  4. Start command:  python app.py
  5. Add your env variables in Render dashboard

ENV VARIABLES — create a .env file:
  VAST_API_KEY=your_key_from_vast_ai
  SECRET_KEY=any_random_string
  ADMIN_KEY=your_admin_password
  INTERNAL_KEY=any_random_string_for_processor
  MTN_SUBSCRIPTION_KEY=from_developer_mtn_com
  MTN_API_USER=from_developer_mtn_com
  MTN_API_KEY=from_developer_mtn_com
  PORT=5000
"""

# ══════════════════════════════════════════════════════════════
#  IMPORTS
# ══════════════════════════════════════════════════════════════

import os
import sys
import uuid
import json
import shutil
import zipfile
import tarfile
import hashlib
import sqlite3
import argparse
import requests
import subprocess
from pathlib import Path
from datetime import datetime
from functools import wraps
from threading import Thread

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from dotenv import load_dotenv

# Optional — only needed when processing games on the GPU instance
try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False

load_dotenv()


# ══════════════════════════════════════════════════════════════
#  CONFIG & CONSTANTS
# ══════════════════════════════════════════════════════════════

# Flask
app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'nexplay-dev-secret-change-in-production')

# Database
DATABASE = 'nexplay.db'

# Vast.ai
VAST_API_KEY  = os.getenv('VAST_API_KEY', '')
VAST_API_URL  = 'https://console.vast.ai/api/v0'

# Pricing
RATE_PER_HOUR_UGX = 5000    # what we charge users per hour
COST_PER_HOUR_USD = 0.30    # what Vast.ai charges us (RTX 3070)
UGX_PER_USD       = 3800    # exchange rate

# Admin & internal keys
ADMIN_KEY    = os.getenv('ADMIN_KEY',    'nexplay-admin-change-this')
INTERNAL_KEY = os.getenv('INTERNAL_KEY', 'nexplay-internal-change-this')

# Game processor paths (used on Vast.ai GPU instance)
GAMES_DIR    = Path('/opt/nexplay/games')
UPLOADS_DIR  = Path('/opt/nexplay/uploads')
SUNSHINE_APPS = Path('/root/.config/sunshine/apps.json')
LOG_FILE      = Path('/opt/nexplay/processor.log')

# Exe names that are NEVER the main game — always skip these
IGNORE_EXE_NAMES = {
    'uninstall', 'unins000', 'uninst', 'setup', 'install',
    'installer', 'directx', 'dxsetup', 'vcredist', 'vc_redist',
    'dotnet', 'dotnetfx', 'netfx', 'oalinst', 'openal',
    'physxsetup', 'ue4prereqsetup', 'ue5prereqsetup',
    'crashreporter', 'crashpad', 'redist', 'prereq',
    'touchup', 'cleanup', 'repair', 'update', 'patch',
    'launcher', 'easyanticheat_setup', 'battleye',
}

# Dependencies to pre-install on every GPU instance via winetricks
REDIST_PACKAGES = [
    'vcrun2005', 'vcrun2008', 'vcrun2010', 'vcrun2012',
    'vcrun2013', 'vcrun2015', 'vcrun2019', 'vcrun2022',
    'directx9', 'd3dx9', 'd3dx10', 'd3dx11',
    'xna40', 'dotnet48', 'physx',
]


# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                phone       TEXT UNIQUE NOT NULL,
                password    TEXT NOT NULL,
                balance_ugx INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS games (
                id           TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                name         TEXT NOT NULL,
                size_gb      REAL DEFAULT 0,
                genre        TEXT DEFAULT 'unknown',
                status       TEXT DEFAULT 'uploading',
                progress     INTEGER DEFAULT 0,
                status_msg   TEXT DEFAULT 'Uploading...',
                exe_path     TEXT,
                file_path    TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id               TEXT PRIMARY KEY,
                user_id          TEXT NOT NULL,
                game_id          TEXT NOT NULL,
                vast_instance_id TEXT,
                stream_url       TEXT,
                gpu_type         TEXT DEFAULT 'RTX 3070',
                started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
                ended_at         TEXT,
                duration_secs    INTEGER DEFAULT 0,
                cost_ugx         INTEGER DEFAULT 0,
                status           TEXT DEFAULT 'starting',
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (game_id) REFERENCES games(id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                type        TEXT NOT NULL,
                amount_ugx  INTEGER NOT NULL,
                method      TEXT,
                reference   TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        ''')
        db.commit()
        print("✅ Database ready.")


# ══════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token(user_id):
    raw = f"{user_id}:{app.config['SECRET_KEY']}"
    return hashlib.sha256(raw.encode()).hexdigest()

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'No token provided'}), 401
        db = get_db()
        current_user = None
        for u in db.execute('SELECT * FROM users').fetchall():
            if generate_token(u['id']) == token:
                current_user = dict(u)
                break
        if not current_user:
            return jsonify({'error': 'Invalid or expired token'}), 401
        g.current_user = current_user
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-Admin-Key', '')
        if key != ADMIN_KEY:
            return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated

def require_internal(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-Internal-Key', '')
        if key != INTERNAL_KEY:
            return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/api/auth/register', methods=['POST'])
def register():
    data     = request.get_json()
    name     = data.get('name', '').strip()
    phone    = data.get('phone', '').strip()
    password = data.get('password', '')

    if not name or not phone or not password:
        return jsonify({'error': 'All fields are required'}), 400

    db = get_db()
    if db.execute('SELECT id FROM users WHERE phone=?', (phone,)).fetchone():
        return jsonify({'error': 'Phone number already registered'}), 409

    user_id = str(uuid.uuid4())
    db.execute(
        'INSERT INTO users (id, name, phone, password, balance_ugx) VALUES (?,?,?,?,?)',
        (user_id, name, phone, hash_password(password), 0)
    )
    db.commit()

    return jsonify({
        'message': 'Account created successfully',
        'token': generate_token(user_id),
        'user': {'id': user_id, 'name': name, 'phone': phone, 'balance_ugx': 0}
    }), 201


@app.route('/api/auth/login', methods=['POST'])
def login():
    data     = request.get_json()
    phone    = data.get('phone', '').strip()
    password = data.get('password', '')

    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
    if not user or user['password'] != hash_password(password):
        return jsonify({'error': 'Incorrect phone or password'}), 401

    return jsonify({
        'token': generate_token(user['id']),
        'user': {
            'id':          user['id'],
            'name':        user['name'],
            'phone':       user['phone'],
            'balance_ugx': user['balance_ugx']
        }
    })


@app.route('/api/auth/me', methods=['GET'])
@require_auth
def me():
    u = g.current_user
    return jsonify({
        'id':          u['id'],
        'name':        u['name'],
        'phone':       u['phone'],
        'balance_ugx': u['balance_ugx'],
        'joined':      u['created_at']
    })


# ══════════════════════════════════════════════════════════════
#  GAME LIBRARY ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/api/games', methods=['GET'])
@require_auth
def list_games():
    db    = get_db()
    games = db.execute(
        'SELECT * FROM games WHERE user_id=? ORDER BY created_at DESC',
        (g.current_user['id'],)
    ).fetchall()
    return jsonify([dict(game) for game in games])


@app.route('/api/games/upload', methods=['POST'])
@require_auth
def upload_game():
    """
    Register a game upload and trigger background processing.

    In production the actual file comes via a chunked multipart upload.
    The processor then runs on the Vast.ai GPU instance.
    For now we register the metadata and simulate processing.
    """
    data      = request.get_json()
    name      = data.get('name', 'Unknown Game').strip()
    genre     = data.get('genre', 'unknown')
    size_gb   = float(data.get('size_gb', 0))
    file_path = data.get('file_path', '')   # path on GPU instance after upload

    game_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        '''INSERT INTO games (id, user_id, name, genre, size_gb, status, progress, status_msg, file_path)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (game_id, g.current_user['id'], name, genre, size_gb, 'uploading', 0, 'Upload received...', file_path)
    )
    db.commit()

    return jsonify({
        'message':  'Game registered. Processing will begin after upload completes.',
        'game_id':  game_id,
        'status':   'uploading'
    }), 201


@app.route('/api/games/<game_id>', methods=['GET'])
@require_auth
def get_game(game_id):
    db   = get_db()
    game = db.execute(
        'SELECT * FROM games WHERE id=? AND user_id=?',
        (game_id, g.current_user['id'])
    ).fetchone()
    if not game:
        return jsonify({'error': 'Game not found'}), 404
    return jsonify(dict(game))


@app.route('/api/games/<game_id>', methods=['DELETE'])
@require_auth
def delete_game(game_id):
    db = get_db()
    db.execute('DELETE FROM games WHERE id=? AND user_id=?', (game_id, g.current_user['id']))
    db.commit()
    return jsonify({'message': 'Game removed'})


# ══════════════════════════════════════════════════════════════
#  INTERNAL ROUTE — called by game processor on GPU instance
# ══════════════════════════════════════════════════════════════

@app.route('/api/internal/game-status', methods=['POST'])
@require_internal
def internal_game_status():
    """
    The game processor running on the Vast.ai GPU instance
    calls this to update the game status in real time.
    """
    data       = request.get_json()
    game_id    = data.get('game_id')
    status     = data.get('status')       # 'processing' | 'ready' | 'error'
    message    = data.get('message', '')
    progress   = data.get('progress', 0)
    exe_path   = data.get('exe_path', '')

    if not game_id or not status:
        return jsonify({'error': 'game_id and status required'}), 400

    db = get_db()
    if exe_path:
        db.execute(
            'UPDATE games SET status=?, progress=?, status_msg=?, exe_path=? WHERE id=?',
            (status, progress, message, exe_path, game_id)
        )
    else:
        db.execute(
            'UPDATE games SET status=?, progress=?, status_msg=? WHERE id=?',
            (status, progress, message, game_id)
        )
    db.commit()

    return jsonify({'message': 'Status updated'})


# ══════════════════════════════════════════════════════════════
#  VAST.AI GPU MANAGEMENT
# ══════════════════════════════════════════════════════════════

def vast_headers():
    return {'Authorization': f'Bearer {VAST_API_KEY}'}


def find_cheapest_gpu():
    """Search Vast.ai for the cheapest verified RTX 3070."""
    try:
        resp = requests.get(
            f'{VAST_API_URL}/bundles/',
            headers=vast_headers(),
            params={
                'q': json.dumps({
                    'verified':    {'eq': True},
                    'rentable':    {'eq': True},
                    'num_gpus':    {'eq': 1},
                    'gpu_name':    {'like': 'RTX 3070'},
                    'reliability2':{'gte': 0.95},
                    'order':       [['dph_total', 'asc']]
                })
            },
            timeout=10
        )
        offers = resp.json().get('offers', [])
        return offers[0] if offers else None
    except Exception as e:
        print(f"Vast.ai search error: {e}")
        return None


def rent_gpu_instance(offer_id, game_name):
    """
    Rent a GPU and run the NexPlay setup + game processor automatically.
    The onstart script installs everything needed to stream the game.
    """
    onstart_script = f"""#!/bin/bash
set -e
apt-get update -y
apt-get install -y wget curl python3 python3-pip p7zip-full unrar-free wine winetricks xvfb ffmpeg

pip3 install flask flask-cors requests python-dotenv py7zr rarfile

# Install Sunshine streaming server
wget -q https://github.com/LizardByte/Sunshine/releases/latest/download/sunshine-ubuntu-22.04-amd64.deb -O /tmp/sunshine.deb
dpkg -i /tmp/sunshine.deb || apt-get install -f -y

# Directories
mkdir -p /opt/nexplay/games /opt/nexplay/uploads
mkdir -p /root/.config/sunshine

# Sunshine config
cat > /root/.config/sunshine/sunshine.conf << 'CONF'
sunshine_name = NexPlay Stream
port = 47989
upnp = off
min_log_level = 2
CONF

echo '{{"apps":[]}}' > /root/.config/sunshine/apps.json

# Start virtual display (games need a screen)
Xvfb :1 -screen 0 1920x1080x24 &>/dev/null &
export DISPLAY=:1

# Start Sunshine
sunshine &>/var/log/sunshine.log &

echo "NexPlay GPU instance ready — game: {game_name}"
"""

    try:
        resp = requests.put(
            f'{VAST_API_URL}/asks/{offer_id}/',
            headers=vast_headers(),
            json={
                'client_id': 'me',
                'image':     'ubuntu:22.04',
                'onstart':   onstart_script,
                'runtype':   'ssh',
                'disk':      80,
                'label':     f'nexplay-{game_name[:20]}'
            },
            timeout=30
        )
        return resp.json()
    except Exception as e:
        print(f"Vast.ai rent error: {e}")
        return None


def get_instance_info(instance_id):
    try:
        resp = requests.get(
            f'{VAST_API_URL}/instances/{instance_id}/',
            headers=vast_headers(),
            timeout=10
        )
        instances = resp.json().get('instances', [])
        return instances[0] if instances else {}
    except Exception as e:
        print(f"Vast.ai instance info error: {e}")
        return {}


def destroy_instance(instance_id):
    try:
        requests.delete(
            f'{VAST_API_URL}/instances/{instance_id}/',
            headers=vast_headers(),
            timeout=10
        )
        return True
    except Exception as e:
        print(f"Vast.ai destroy error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  SESSION ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/api/sessions/start', methods=['POST'])
@require_auth
def start_session():
    data    = request.get_json()
    game_id = data.get('game_id')
    user    = g.current_user

    if user['balance_ugx'] < RATE_PER_HOUR_UGX:
        return jsonify({'error': f'Insufficient balance. Need at least {RATE_PER_HOUR_UGX:,} UGX (1 hour).'}), 402

    db   = get_db()
    game = db.execute(
        'SELECT * FROM games WHERE id=? AND user_id=?',
        (game_id, user['id'])
    ).fetchone()
    if not game:
        return jsonify({'error': 'Game not found'}), 404
    if game['status'] != 'ready':
        return jsonify({'error': f'Game not ready yet. Status: {game["status"]}'}), 400

    offer = find_cheapest_gpu()
    if not offer:
        return jsonify({'error': 'No GPU available right now. Try again shortly.'}), 503

    instance_result = rent_gpu_instance(offer['id'], game['name'])
    if not instance_result:
        return jsonify({'error': 'Failed to allocate GPU. Please try again.'}), 503

    instance_id = instance_result.get('new_contract')
    session_id  = str(uuid.uuid4())

    db.execute(
        '''INSERT INTO sessions (id, user_id, game_id, vast_instance_id, gpu_type, status)
           VALUES (?,?,?,?,?,?)''',
        (session_id, user['id'], game_id, str(instance_id), offer.get('gpu_name', 'RTX 3070'), 'starting')
    )
    db.commit()

    return jsonify({
        'session_id':  session_id,
        'status':      'starting',
        'message':     'GPU allocated. Server booting — ready in ~60 seconds.',
        'gpu':         offer.get('gpu_name', 'RTX 3070'),
        'instance_id': instance_id
    }), 201


@app.route('/api/sessions/<session_id>/status', methods=['GET'])
@require_auth
def session_status(session_id):
    db      = get_db()
    session = db.execute(
        'SELECT * FROM sessions WHERE id=? AND user_id=?',
        (session_id, g.current_user['id'])
    ).fetchone()
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    session = dict(session)

    if session['status'] == 'starting' and session['vast_instance_id']:
        info          = get_instance_info(session['vast_instance_id'])
        actual_status = info.get('actual_status', 'loading')

        if actual_status == 'running':
            ip         = info.get('public_ipaddr', '')
            stream_url = f"moonlight://{ip}:47989"
            db.execute(
                'UPDATE sessions SET status=?, stream_url=? WHERE id=?',
                ('active', stream_url, session_id)
            )
            db.commit()
            session['status']     = 'active'
            session['stream_url'] = stream_url

    return jsonify(session)


@app.route('/api/sessions/<session_id>/end', methods=['POST'])
@require_auth
def end_session(session_id):
    db      = get_db()
    session = db.execute(
        "SELECT * FROM sessions WHERE id=? AND user_id=? AND status='active'",
        (session_id, g.current_user['id'])
    ).fetchone()
    if not session:
        return jsonify({'error': 'Active session not found'}), 404

    session = dict(session)

    started_at    = datetime.fromisoformat(session['started_at'])
    ended_at      = datetime.utcnow()
    duration_secs = int((ended_at - started_at).total_seconds())
    cost_ugx      = int((duration_secs / 3600) * RATE_PER_HOUR_UGX)

    # Stop the GPU immediately — this stops Vast.ai billing
    if session['vast_instance_id']:
        destroy_instance(session['vast_instance_id'])

    db.execute('UPDATE users SET balance_ugx=balance_ugx-? WHERE id=?', (cost_ugx, g.current_user['id']))
    db.execute(
        'UPDATE sessions SET status=?, ended_at=?, duration_secs=?, cost_ugx=? WHERE id=?',
        ('ended', ended_at.isoformat(), duration_secs, cost_ugx, session_id)
    )

    tx_id = str(uuid.uuid4())
    db.execute(
        'INSERT INTO transactions (id,user_id,type,amount_ugx,method,reference,status) VALUES (?,?,?,?,?,?,?)',
        (tx_id, g.current_user['id'], 'session_charge', cost_ugx, 'balance', session_id, 'completed')
    )
    db.commit()

    updated = db.execute('SELECT balance_ugx FROM users WHERE id=?', (g.current_user['id'],)).fetchone()
    h, rem  = divmod(duration_secs, 3600)
    m, s    = divmod(rem, 60)

    return jsonify({
        'message':         'Session ended',
        'duration':        f'{h:02d}:{m:02d}:{s:02d}',
        'duration_secs':   duration_secs,
        'cost_ugx':        cost_ugx,
        'new_balance_ugx': updated['balance_ugx']
    })


@app.route('/api/sessions', methods=['GET'])
@require_auth
def list_sessions():
    db = get_db()
    sessions = db.execute(
        '''SELECT s.*, g.name as game_name FROM sessions s
           JOIN games g ON s.game_id=g.id
           WHERE s.user_id=? ORDER BY s.started_at DESC LIMIT 20''',
        (g.current_user['id'],)
    ).fetchall()
    return jsonify([dict(s) for s in sessions])


# ══════════════════════════════════════════════════════════════
#  BILLING ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/api/billing/topup/initiate', methods=['POST'])
@require_auth
def initiate_topup():
    data       = request.get_json()
    amount_ugx = int(data.get('amount_ugx', 0))
    method     = data.get('method', 'mtn')   # 'mtn' or 'airtel'
    phone      = data.get('phone', g.current_user['phone'])

    if amount_ugx < 1000:
        return jsonify({'error': 'Minimum top-up is 1,000 UGX'}), 400

    tx_id     = str(uuid.uuid4())
    reference = f"NP-{tx_id[:8].upper()}"

    # ── MTN MoMo API call goes here ──────────────────────────
    # Uncomment when you have your MTN developer credentials:
    #
    # mtn_resp = requests.post(
    #     'https://proxy.momoapi.mtn.com/collection/v1_0/requesttopay',
    #     headers={
    #         'Authorization': f'Bearer {get_mtn_token()}',
    #         'X-Reference-Id': tx_id,
    #         'X-Target-Environment': 'production',
    #         'Ocp-Apim-Subscription-Key': os.getenv('MTN_SUBSCRIPTION_KEY'),
    #     },
    #     json={
    #         'amount':    str(amount_ugx),
    #         'currency':  'UGX',
    #         'externalId': reference,
    #         'payer':     {'partyIdType': 'MSISDN', 'partyId': phone},
    #         'payerMessage': f'NexPlay top-up {reference}',
    #         'payeeNote':    'NexPlay gaming credit'
    #     }
    # )
    # ─────────────────────────────────────────────────────────

    db = get_db()
    db.execute(
        'INSERT INTO transactions (id,user_id,type,amount_ugx,method,reference,status) VALUES (?,?,?,?,?,?,?)',
        (tx_id, g.current_user['id'], 'topup', amount_ugx, method, reference, 'pending')
    )
    db.commit()

    return jsonify({
        'message':        f'Payment prompt sent to {phone}. Approve on your phone.',
        'reference':      reference,
        'transaction_id': tx_id,
        'amount_ugx':     amount_ugx,
        'method':         method,
        'status':         'pending'
    })


@app.route('/api/billing/topup/confirm', methods=['POST'])
def momo_webhook():
    """
    MTN/Airtel calls this URL when a payment is confirmed.
    Set in your MoMo developer dashboard:
      https://yourapp.onrender.com/api/billing/topup/confirm
    """
    data      = request.get_json()
    reference = data.get('reference') or data.get('externalId')
    status    = data.get('status', '').lower()

    if not reference:
        return jsonify({'error': 'No reference provided'}), 400

    db = get_db()
    tx = db.execute('SELECT * FROM transactions WHERE reference=?', (reference,)).fetchone()
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    if status in ('successful', 'completed', 'success'):
        db.execute('UPDATE users SET balance_ugx=balance_ugx+? WHERE id=?', (tx['amount_ugx'], tx['user_id']))
        db.execute("UPDATE transactions SET status='completed' WHERE reference=?", (reference,))
        db.commit()
        return jsonify({'message': f'Credited {tx["amount_ugx"]:,} UGX'})

    if status in ('failed', 'cancelled'):
        db.execute("UPDATE transactions SET status='failed' WHERE reference=?", (reference,))
        db.commit()
        return jsonify({'message': 'Payment failed or cancelled'})

    return jsonify({'message': 'Noted', 'status': status})


@app.route('/api/billing/balance', methods=['GET'])
@require_auth
def get_balance():
    db   = get_db()
    user = db.execute('SELECT balance_ugx FROM users WHERE id=?', (g.current_user['id'],)).fetchone()
    return jsonify({'balance_ugx': user['balance_ugx']})


@app.route('/api/billing/transactions', methods=['GET'])
@require_auth
def list_transactions():
    db  = get_db()
    txs = db.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
        (g.current_user['id'],)
    ).fetchall()
    return jsonify([dict(t) for t in txs])


# ══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def admin_stats():
    db = get_db()
    return jsonify({
        'total_users':     db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c'],
        'total_sessions':  db.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c'],
        'active_sessions': db.execute("SELECT COUNT(*) as c FROM sessions WHERE status='active'").fetchone()['c'],
        'total_revenue_ugx': db.execute("SELECT COALESCE(SUM(amount_ugx),0) as t FROM transactions WHERE type='session_charge' AND status='completed'").fetchone()['t'],
        'total_topups_ugx':  db.execute("SELECT COALESCE(SUM(amount_ugx),0) as t FROM transactions WHERE type='topup' AND status='completed'").fetchone()['t'],
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_users():
    db    = get_db()
    users = db.execute('SELECT id,name,phone,balance_ugx,created_at FROM users ORDER BY created_at DESC').fetchall()
    return jsonify([dict(u) for u in users])


@app.route('/api/admin/sessions/active', methods=['GET'])
@require_admin
def admin_active_sessions():
    db = get_db()
    rows = db.execute(
        '''SELECT s.*, u.name as user_name, g.name as game_name
           FROM sessions s JOIN users u ON s.user_id=u.id JOIN games g ON s.game_id=g.id
           WHERE s.status='active' '''
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/sessions/<session_id>/kill', methods=['POST'])
@require_admin
def admin_kill_session(session_id):
    db      = get_db()
    session = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    if session['vast_instance_id']:
        destroy_instance(session['vast_instance_id'])
    db.execute(
        "UPDATE sessions SET status='killed', ended_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), session_id)
    )
    db.commit()
    return jsonify({'message': f'Session {session_id} killed and GPU destroyed.'})


@app.route('/api/admin/topup/manual', methods=['POST'])
@require_admin
def admin_manual_topup():
    """Credit a user manually — for testing before MoMo is live."""
    data       = request.get_json()
    phone      = data.get('phone')
    amount_ugx = int(data.get('amount_ugx', 0))
    db         = get_db()
    user       = db.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    tx_id = str(uuid.uuid4())
    db.execute('UPDATE users SET balance_ugx=balance_ugx+? WHERE id=?', (amount_ugx, user['id']))
    db.execute(
        'INSERT INTO transactions (id,user_id,type,amount_ugx,method,reference,status) VALUES (?,?,?,?,?,?,?)',
        (tx_id, user['id'], 'topup', amount_ugx, 'admin_manual', f'ADMIN-{tx_id[:8].upper()}', 'completed')
    )
    db.commit()
    return jsonify({'message': f'Credited {amount_ugx:,} UGX to {user["name"]}'})


# ══════════════════════════════════════════════════════════════
#  GAME PROCESSOR
#  This section runs ON the Vast.ai GPU instance, not the main server.
#  The main server calls process_game() in a background thread
#  after a game upload, or you can run it from CLI on the instance.
# ══════════════════════════════════════════════════════════════

def proc_log(msg, level='INFO'):
    line = f"[{datetime.utcnow().strftime('%H:%M:%S')}] [{level}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


class StatusReporter:
    """Sends processing progress back to NexPlay API so user sees it live."""
    def __init__(self, api_url, game_id, api_key):
        self.api_url = api_url
        self.game_id = game_id
        self.api_key = api_key

    def report(self, status, message, progress=None, exe_path=None):
        payload = {'game_id': self.game_id, 'status': status, 'message': message}
        if progress is not None:
            payload['progress'] = progress
        if exe_path:
            payload['exe_path'] = str(exe_path)
        try:
            requests.post(
                f"{self.api_url}/api/internal/game-status",
                json=payload,
                headers={'X-Internal-Key': self.api_key},
                timeout=10
            )
        except Exception as e:
            proc_log(f"Status report failed: {e}", 'WARN')
        proc_log(f"[{status}] {message}" + (f" ({progress}%)" if progress else ""))


def detect_file_type(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    name   = file_path.name.lower()

    # Check multipart FIRST before individual archive types
    if any(x in name for x in ['.part1.', '.part01.', '.z01', '.001', '.r00']):
        return 'multi_part'

    if suffix == '.zip':  return 'zip'
    if suffix == '.7z':   return '7z'
    if suffix == '.rar':  return 'rar'
    if suffix in ('.tar', '.gz', '.bz2', '.xz'): return 'tar'

    if suffix == '.exe':
        name_lower = file_path.stem.lower()
        size_mb    = file_path.stat().st_size / (1024 * 1024)
        if any(k in name_lower for k in ['setup', 'install', 'installer', 'gog_']):
            return 'exe_installer'
        return 'exe_game' if size_mb > 100 else 'exe_installer'

    if file_path.is_dir():
        return 'folder'

    # Magic bytes fallback
    try:
        with open(file_path, 'rb') as f:
            h = f.read(8)
        if h[:2] == b'PK':               return 'zip'
        if h[:6] == b'7z\xbc\xaf\x27\x1c': return '7z'
        if h[:4] == b'Rar!':             return 'rar'
        if h[:2] == b'MZ':               return 'exe_installer'
    except Exception:
        pass

    return 'unknown'


def extract_zip(fp: Path, dest: Path, rep: StatusReporter) -> bool:
    rep.report('processing', 'Extracting ZIP...', 20)
    try:
        with zipfile.ZipFile(fp, 'r') as z:
            members = z.infolist()
            total   = len(members)
            for i, m in enumerate(members):
                z.extract(m, dest)
                if i % max(1, total // 10) == 0:
                    rep.report('processing', f'Extracting... {i}/{total} files', 20 + int(i / total * 35))
        return True
    except zipfile.BadZipFile as e:
        rep.report('error', f'ZIP is corrupted: {e}')
        return False


def extract_7z(fp: Path, dest: Path, rep: StatusReporter, password=None) -> bool:
    if not HAS_PY7ZR:
        rep.report('error', 'py7zr not installed on this instance.')
        return False
    rep.report('processing', 'Extracting 7Z...', 20)
    try:
        kw = {'password': password} if password else {}
        with py7zr.SevenZipFile(fp, mode='r', **kw) as z:
            z.extractall(path=dest)
        return True
    except Exception as e:
        if 'password' in str(e).lower():
            rep.report('error', 'Archive is password protected. Re-upload and provide the password.')
        else:
            rep.report('error', f'7Z extraction failed: {e}')
        return False


def extract_rar(fp: Path, dest: Path, rep: StatusReporter, password=None) -> bool:
    if not HAS_RAR:
        rep.report('error', 'rarfile not installed on this instance.')
        return False
    rep.report('processing', 'Extracting RAR...', 20)
    try:
        with rarfile.RarFile(fp) as rf:
            if password:
                rf.setpassword(password)
            rf.extractall(dest)
        return True
    except Exception as e:
        if 'password' in str(e).lower():
            rep.report('error', 'RAR is password protected. Please provide the password.')
        else:
            rep.report('error', f'RAR extraction failed: {e}')
        return False


def extract_tar(fp: Path, dest: Path, rep: StatusReporter) -> bool:
    rep.report('processing', 'Extracting TAR...', 20)
    try:
        with tarfile.open(fp) as tf:
            tf.extractall(dest)
        return True
    except Exception as e:
        rep.report('error', f'TAR extraction failed: {e}')
        return False


def extract_multipart(fp: Path, dest: Path, rep: StatusReporter) -> bool:
    rep.report('processing', 'Joining multi-part archive...', 15)
    try:
        result = subprocess.run(
            ['7z', 'x', str(fp), f'-o{dest}', '-y'],
            capture_output=True, text=True, timeout=1800
        )
        if result.returncode == 0:
            return True
        rep.report('error', f'Multi-part extraction failed: {result.stderr[:200]}')
        return False
    except subprocess.TimeoutExpired:
        rep.report('error', 'Extraction timed out.')
        return False
    except Exception as e:
        rep.report('error', f'Multi-part error: {e}')
        return False


def run_installer(exe: Path, install_dir: Path, rep: StatusReporter) -> bool:
    rep.report('processing', f'Running installer: {exe.name}...', 45)
    silent_flags = [
        ['/S', f'/D={install_dir}'],
        ['/SILENT', f'/DIR={install_dir}'],
        ['/quiet', f'/InstallDir={install_dir}'],
        ['/verysilent', f'/dir={install_dir}'],
        ['/qn', f'INSTALLDIR={install_dir}'],
    ]
    for flags in silent_flags:
        try:
            cmd    = ['wine', str(exe)] + flags
            result = subprocess.run(cmd, capture_output=True, timeout=600, cwd=str(exe.parent))
            if result.returncode in (0, 1, 3010):
                proc_log(f"Installer succeeded with flags: {flags}")
                return True
        except (subprocess.TimeoutExpired, Exception):
            continue
    rep.report('error', 'Could not run installer silently. Try uploading the already-installed game folder.')
    return False


def find_game_exe(search_dir: Path, game_name: str = '') -> Path | None:
    candidates = []
    for exe in search_dir.rglob('*.exe'):
        stem = exe.stem.lower()
        if stem in IGNORE_EXE_NAMES:
            continue
        path_str = str(exe).lower()
        if any(skip in path_str for skip in ['redist', 'directx', 'vcredist', '_commonredist', 'prerequisites', 'crashreport']):
            continue
        try:
            size_mb = exe.stat().st_size / (1024 * 1024)
        except Exception:
            continue

        score = 0
        if   size_mb > 5000: score += 100
        elif size_mb > 1000: score += 80
        elif size_mb > 100:  score += 60
        elif size_mb > 10:   score += 40
        elif size_mb > 1:    score += 20
        else:                score += 5

        if game_name:
            gw = set(game_name.lower().replace(':', '').split())
            ew = set(stem.replace('_', ' ').replace('-', ' ').split())
            score += len(gw & ew) * 15

        if any(p in stem for p in ['game', 'launch', 'play', 'start', 'run', 'client']):
            score += 10

        depth  = len(exe.relative_to(search_dir).parts)
        score -= (depth - 1) * 5

        candidates.append((exe, score))
        proc_log(f"  Candidate: {exe.name} | {size_mb:.0f}MB | score={score}")

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    winner = candidates[0][0]
    proc_log(f"Selected: {winner}", 'OK')
    return winner


def install_dependencies(rep: StatusReporter):
    rep.report('processing', 'Installing DirectX, Visual C++ runtimes...', 70)
    for pkg in REDIST_PACKAGES:
        try:
            subprocess.run(['winetricks', '-q', pkg], capture_output=True, timeout=300)
            proc_log(f"Installed: {pkg}", 'OK')
        except FileNotFoundError:
            proc_log("winetricks not found — skipping dependencies")
            break
        except Exception as e:
            proc_log(f"Error installing {pkg}: {e}", 'WARN')


def register_with_sunshine(game_name: str, exe_path: Path, game_id: str) -> bool:
    SUNSHINE_APPS.parent.mkdir(parents=True, exist_ok=True)
    config = {'apps': []}
    if SUNSHINE_APPS.exists():
        try:
            with open(SUNSHINE_APPS) as f:
                config = json.load(f)
        except Exception:
            config = {'apps': []}

    config['apps'] = [a for a in config['apps'] if a.get('nexplay_id') != game_id]
    config['apps'].append({
        'name':        game_name,
        'nexplay_id':  game_id,
        'cmd':         str(exe_path),
        'working-dir': str(exe_path.parent),
        'auto-detach': True,
        'wait-all':    True,
        'exit-timeout': 5,
    })
    try:
        with open(SUNSHINE_APPS, 'w') as f:
            json.dump(config, f, indent=2)
        subprocess.run(['pkill', '-HUP', 'sunshine'], capture_output=True, timeout=5)
        proc_log(f"Registered with Sunshine: {game_name}", 'OK')
        return True
    except Exception as e:
        proc_log(f"Sunshine registration failed: {e}", 'ERROR')
        return False


def process_game(file_path: str, game_id: str, game_name: str,
                 api_url: str, api_key: str, password: str = None) -> bool:
    """
    Main game processing pipeline.
    Detects file type → extracts → finds exe → installs deps → registers with Sunshine.
    """
    fp       = Path(file_path)
    reporter = StatusReporter(api_url, game_id, api_key)

    proc_log(f"═══ Processing: {fp.name} ═══")
    reporter.report('processing', f'Starting: {fp.name}', 5)

    ftype = detect_file_type(fp)
    reporter.report('processing', f'Detected: {ftype.upper()}', 10)

    if ftype == 'unknown':
        reporter.report('error', 'Unknown file type. Upload a .zip, .7z, .rar, or .exe file.')
        return False

    game_dir     = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    extracted    = game_dir / 'files'
    extracted.mkdir(exist_ok=True)
    success      = False

    if   ftype == 'zip':          success = extract_zip(fp, extracted, reporter)
    elif ftype == '7z':           success = extract_7z(fp, extracted, reporter, password)
    elif ftype == 'rar':          success = extract_rar(fp, extracted, reporter, password)
    elif ftype == 'tar':          success = extract_tar(fp, extracted, reporter)
    elif ftype == 'multi_part':   success = extract_multipart(fp, extracted, reporter)
    elif ftype == 'exe_installer':
        install_target = game_dir / 'installed'
        install_target.mkdir(exist_ok=True)
        success    = run_installer(fp, install_target, reporter)
        if success: extracted = install_target
    elif ftype in ('exe_game', 'folder'):
        if ftype == 'exe_game':
            shutil.copy2(fp, extracted / fp.name)
        else:
            extracted = fp
        reporter.report('processing', 'File ready.', 30)
        success = True

    if not success:
        return False

    reporter.report('processing', 'Finding game executable...', 60)
    exe = find_game_exe(extracted, game_name) or find_game_exe(game_dir, game_name)

    if not exe:
        reporter.report('error',
            'Could not find game executable automatically. '
            'If multi-disc, upload all parts. Contact support with game name.'
        )
        return False

    install_dependencies(reporter)

    reporter.report('processing', 'Registering with streaming server...', 85)
    if not register_with_sunshine(game_name, exe, game_id):
        reporter.report('error', 'Failed to register with Sunshine streaming server.')
        return False

    (game_dir / 'metadata.json').write_text(json.dumps({
        'game_id':      game_id,
        'game_name':    game_name,
        'exe_path':     str(exe),
        'processed_at': datetime.utcnow().isoformat(),
        'file_type':    ftype,
    }, indent=2))

    reporter.report('ready', f'{game_name} is ready to play! 🎮', 100, exe)
    proc_log(f"═══ {game_name} complete ═══", 'OK')
    return True


# ══════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
def health():
    return jsonify({
        'service':   'NexPlay API',
        'status':    'running',
        'version':   '2.0.0',
        'timestamp': datetime.utcnow().isoformat(),
        'endpoints': [
            'POST /api/auth/register',
            'POST /api/auth/login',
            'GET  /api/auth/me',
            'GET  /api/games',
            'POST /api/games/upload',
            'GET  /api/games/<id>',
            'DEL  /api/games/<id>',
            'POST /api/internal/game-status',
            'POST /api/sessions/start',
            'GET  /api/sessions/<id>/status',
            'POST /api/sessions/<id>/end',
            'GET  /api/sessions',
            'POST /api/billing/topup/initiate',
            'POST /api/billing/topup/confirm',
            'GET  /api/billing/balance',
            'GET  /api/billing/transactions',
            'GET  /api/admin/stats',
            'GET  /api/admin/users',
            'GET  /api/admin/sessions/active',
            'POST /api/admin/sessions/<id>/kill',
            'POST /api/admin/topup/manual',
        ]
    })


# ══════════════════════════════════════════════════════════════
#  CLI — run as game processor on GPU instance
#  python app.py process --file /uploads/re4.zip --game-id abc --game-name "Resident Evil 4" --api http://nexplay.onrender.com --api-key yourkey
# ══════════════════════════════════════════════════════════════

def cli():
    parser = argparse.ArgumentParser(description='NexPlay — API server or game processor')
    sub    = parser.add_subparsers(dest='cmd')

    srv = sub.add_parser('serve', help='Run the Flask API server (default)')

    proc = sub.add_parser('process', help='Process a game file on GPU instance')
    proc.add_argument('--file',      required=True)
    proc.add_argument('--game-id',   required=True)
    proc.add_argument('--game-name', required=True)
    proc.add_argument('--api',       required=True, help='NexPlay backend URL')
    proc.add_argument('--api-key',   required=True, help='Internal API key')
    proc.add_argument('--password',  default=None,  help='Archive password if needed')

    args = parser.parse_args()

    if args.cmd == 'process':
        ok = process_game(
            file_path = args.file,
            game_id   = args.game_id,
            game_name = args.game_name,
            api_url   = args.api,
            api_key   = args.api_key,
            password  = args.password,
        )
        sys.exit(0 if ok else 1)
    else:
        # Default: run the Flask server
        init_db()
        port = int(os.getenv('PORT', 5000))
        print(f"""
╔══════════════════════════════════════════╗
║         NexPlay Backend v2.0             ║
║    Cloud Gaming · Built for Africa      ║
╠══════════════════════════════════════════╣
║  API:  http://localhost:{port:<5}            ║
║  Docs: http://localhost:{port}               ║
╚══════════════════════════════════════════╝
        """)
        app.run(host='0.0.0.0', port=port, debug=False)


if __name__ == '__main__':
    cli()
