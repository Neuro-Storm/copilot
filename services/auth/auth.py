#!/usr/bin/env python3
"""
Микросервис авторизации для RAG Copilot.

Функциональность:
- Регистрация и аутентификация пользователей
- Выдача и обновление токенов доступа
- Управление пользователями (CRUD) для администраторов
- Смена пароля пользователем
- CLI для создания первого администратора

Запуск как сервис:
    python auth.py

Создание администратора из CLI:
    python auth.py --create-user admin --password секрет --role admin

Сброс пароля из CLI:
    python auth.py --reset-password admin --new-password новый_пароль

Список пользователей:
    python auth.py --list-users
"""

import os
import sys
import json
import sqlite3
import secrets
import logging
import argparse
import time
from datetime import datetime
from pathlib import Path
from functools import wraps
from contextlib import contextmanager

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# Загрузка .env из корня проекта и из директории сервиса
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')
load_dotenv()

# Добавляем common в путь импорта
_common_path = str(Path(__file__).parent.parent / 'common')
if _common_path not in sys.path:
    sys.path.append(_common_path)
from tokens import create_token, verify_token

# --- Конфигурация ---

def load_config():
    """Загружает конфигурацию из config.json."""
    config_path = Path(__file__).parent / 'config.json'
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

CONFIG = load_config()

# Проверка SECRET_KEY: критично для безопасности!
_SECRET_KEY_RAW = os.getenv('AUTH_SECRET_KEY')
if not _SECRET_KEY_RAW or _SECRET_KEY_RAW == 'change-me-in-production':
    raise RuntimeError(
        "AUTH_SECRET_KEY не установлен или использует значение по умолчанию! "
        "Задайте безопасный ключ в .env файле: AUTH_SECRET_KEY=ваш_секретный_ключ"
    )
SECRET_KEY = _SECRET_KEY_RAW

HOST = CONFIG.get('host', '0.0.0.0')
PORT = int(os.getenv('AUTH_SERVICE_PORT', CONFIG.get('port', 5050)))
DB_PATH = Path(__file__).parent / CONFIG.get('db_path', 'auth.db')
ACCESS_TOKEN_TTL = CONFIG.get('access_token_ttl', 1800)
REFRESH_TOKEN_TTL = CONFIG.get('refresh_token_ttl', 604800)
MIN_PASSWORD_LENGTH = CONFIG.get('min_password_length', 6)
MAX_LOGIN_ATTEMPTS = CONFIG.get('max_login_attempts', 10)
LOCKOUT_DURATION = CONFIG.get('lockout_duration', 300)

# --- Логирование ---

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[
        logging.FileHandler(Path(__file__).parent / 'auth.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('auth')

# --- База данных ---

@contextmanager
def get_db():
    """Контекстный менеджер для работы с SQLite."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Инициализирует таблицы в базе данных."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                role TEXT DEFAULT 'user' CHECK (role IN ('admin', 'user', 'viewer')),
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                refresh_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                details TEXT DEFAULT '',
                ip_address TEXT DEFAULT '',
                username TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(refresh_token);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
            CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username);
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        """)
    logger.info("База данных инициализирована")


def cleanup_expired_sessions():
    """Удаляет истёкшие refresh-сессии."""
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM sessions WHERE expires_at < datetime('now')"
        )
        if result.rowcount > 0:
            logger.info(f"Удалено {result.rowcount} истёкших сессий")


# --- Вспомогательные функции ---

def log_audit(user_id, action, details='', ip_address='', username=''):
    """Записывает действие в журнал аудита."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, details, ip_address, username) VALUES (?, ?, ?, ?, ?)",
                (user_id, action, details, ip_address, username)
            )
    except Exception as e:
        logger.error(f"Ошибка записи в журнал аудита: {e}")


def get_client_ip():
    """Получает IP-адрес клиента."""
    return request.remote_addr or 'unknown'


def validate_password(password):
    """Проверяет что пароль соответствует требованиям."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Пароль должен быть не менее {MIN_PASSWORD_LENGTH} символов"
    return True, ""


def validate_username(username):
    """Проверяет что имя пользователя допустимо."""
    if not username or len(username) < 2:
        return False, "Имя пользователя должно быть не менее 2 символов"
    if len(username) > 50:
        return False, "Имя пользователя слишком длинное (максимум 50)"
    if not username.replace('_', '').replace('-', '').replace('.', '').isalnum():
        return False, "Имя пользователя может содержать только буквы, цифры, _, -, ."
    return True, ""


def _check_lockout(username):
    """
    Проверяет, не заблокирован ли пользователь из-за множества неудачных попыток входа.
    Возвращает (is_locked, seconds_remaining).

    Исправлено: учитываются только неудачные попытки ПОСЛЕ последнего успешного входа.
    """
    with get_db() as conn:
        # Получаем время последнего успешного входа
        last_success = conn.execute(
            """SELECT MAX(created_at) FROM audit_log
               WHERE action = 'login' AND username = ?""",
            (username,)
        ).fetchone()[0]

        cutoff = time.time() - LOCKOUT_DURATION

        # Считаем неудачные попытки только после последнего успешного входа
        query = """SELECT COUNT(*) as cnt, MAX(strftime('%s', created_at)) as last_attempt
                   FROM audit_log
                   WHERE action = 'login_failed'
                   AND username = ?
                   AND created_at > datetime(?, 'unixepoch')"""
        params = [username, cutoff]

        # Если был успешный вход, учитываем неудачные попытки только после него
        if last_success:
            query += " AND created_at > ?"
            params.append(last_success)

        row = conn.execute(query, params).fetchone()

        if row and row['cnt'] >= MAX_LOGIN_ATTEMPTS and row['last_attempt']:
            last_attempt_ts = float(row['last_attempt'])
            remaining = int(LOCKOUT_DURATION - (time.time() - last_attempt_ts))
            return True, max(1, remaining)
    return False, 0


# --- Middleware авторизации для auth-сервиса ---

def auth_require_role(*allowed_roles):
    """Декоратор для защиты эндпоинтов auth-сервиса."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            token = request.cookies.get('access_token')
            if not token:
                auth_header = request.headers.get('Authorization', '')
                if auth_header.startswith('Bearer '):
                    token = auth_header[7:]

            if not token:
                return jsonify({'error': 'Требуется авторизация'}), 401

            try:
                payload = verify_token(token, SECRET_KEY)
            except ValueError as e:
                return jsonify({'error': str(e)}), 401

            if not payload.get('is_active', True):
                return jsonify({'error': 'Пользователь заблокирован'}), 403

            if payload.get('role') not in allowed_roles:
                return jsonify({'error': 'Недостаточно прав'}), 403

            kwargs['current_user'] = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


# --- Flask приложение ---

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 MB
CORS(app, supports_credentials=True)


# ==================== АУТЕНТИФИКАЦИЯ ====================

@app.route('/api/auth/login', methods=['POST'])
def login():
    """
    Аутентификация пользователя.

    Принимает JSON: {"username": "...", "password": "..."}
    Возвращает: access_token + refresh_token (в cookie и в теле ответа)
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Требуется JSON'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    client_ip = get_client_ip()

    if not username or not password:
        return jsonify({'error': 'Укажите имя пользователя и пароль'}), 400

    # Проверка блокировки
    is_locked, remaining = _check_lockout(username)
    if is_locked:
        logger.warning(f"[{username}] Попытка входа при блокировке, ip={client_ip}")
        return jsonify({
            'error': f'Слишком много неудачных попыток. Повторите через {remaining} сек.'
        }), 429

    # Поиск пользователя
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    if not user or not check_password_hash(user['password_hash'], password):
        log_audit(None, 'login_failed', f'username={username}', client_ip, username=username)
        logger.warning(f"[{username}] Неудачная попытка входа, ip={client_ip}")
        return jsonify({'error': 'Неверные учётные данные'}), 401

    if not user['is_active']:
        log_audit(user['id'], 'login_blocked', 'Аккаунт заблокирован', client_ip)
        logger.warning(f"[{username}] Попытка входа в заблокированный аккаунт, ip={client_ip}")
        return jsonify({'error': 'Аккаунт заблокирован'}), 403

    # Создание токенов
    access_payload = {
        'user_id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'] or user['username'],
        'role': user['role'],
        'is_active': bool(user['is_active'])
    }
    access_token = create_token(access_payload, SECRET_KEY, ttl=ACCESS_TOKEN_TTL)

    refresh_token = secrets.token_hex(32)
    refresh_expires = datetime.utcfromtimestamp(time.time() + REFRESH_TOKEN_TTL)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, refresh_token, expires_at) VALUES (?, ?, ?)",
            (user['id'], refresh_token, refresh_expires.isoformat())
        )
        conn.execute(
            "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?",
            (user['id'],)
        )

    log_audit(user['id'], 'login', f'Успешный вход', client_ip)
    logger.info(f"[{username}] Успешный вход, role={user['role']}, ip={client_ip}")

    response = jsonify({
        'access_token': access_token,
        'username': user['username'],
        'display_name': user['display_name'] or user['username'],
        'role': user['role']
    })
    response.set_cookie('access_token', access_token, httponly=True,
                        samesite='Lax', max_age=ACCESS_TOKEN_TTL)
    response.set_cookie('refresh_token', refresh_token, httponly=True,
                        samesite='Lax', max_age=REFRESH_TOKEN_TTL)
    return response


@app.route('/api/auth/refresh', methods=['POST'])
def refresh():
    """
    Обновление access-токена по refresh-токену.
    Refresh-токен берётся из cookie или из JSON тела запроса.
    """
    refresh_token = request.cookies.get('refresh_token')
    if not refresh_token:
        data = request.get_json() or {}
        refresh_token = data.get('refresh_token')

    if not refresh_token:
        return jsonify({'error': 'Refresh-токен не предоставлен'}), 401

    with get_db() as conn:
        session = conn.execute(
            """SELECT s.*, u.username, u.display_name, u.role, u.is_active
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.refresh_token = ? AND s.expires_at > datetime('now')""",
            (refresh_token,)
        ).fetchone()

    if not session:
        return jsonify({'error': 'Невалидный или истёкший refresh-токен'}), 401

    if not session['is_active']:
        return jsonify({'error': 'Аккаунт заблокирован'}), 403

    # Новый access-токен
    access_payload = {
        'user_id': session['user_id'],
        'username': session['username'],
        'display_name': session['display_name'] or session['username'],
        'role': session['role'],
        'is_active': bool(session['is_active'])
    }
    access_token = create_token(access_payload, SECRET_KEY, ttl=ACCESS_TOKEN_TTL)

    response = jsonify({'access_token': access_token})
    response.set_cookie('access_token', access_token, httponly=True,
                        samesite='Lax', max_age=ACCESS_TOKEN_TTL)
    return response


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Выход: удаляет refresh-сессию и очищает cookie."""
    refresh_token = request.cookies.get('refresh_token')
    client_ip = get_client_ip()

    if refresh_token:
        with get_db() as conn:
            session = conn.execute(
                "SELECT user_id FROM sessions WHERE refresh_token = ?",
                (refresh_token,)
            ).fetchone()
            if session:
                log_audit(session['user_id'], 'logout', '', client_ip)
            conn.execute(
                "DELETE FROM sessions WHERE refresh_token = ?",
                (refresh_token,)
            )

    response = jsonify({'success': True})
    response.delete_cookie('access_token')
    response.delete_cookie('refresh_token')
    return response


@app.route('/api/auth/me', methods=['GET'])
def me():
    """Возвращает информацию о текущем пользователе."""
    token = request.cookies.get('access_token')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]

    if not token:
        return jsonify({'error': 'Не авторизован'}), 401

    try:
        payload = verify_token(token, SECRET_KEY)
    except ValueError as e:
        return jsonify({'error': str(e)}), 401

    return jsonify({
        'user_id': payload['user_id'],
        'username': payload['username'],
        'display_name': payload.get('display_name', payload['username']),
        'role': payload['role']
    })


# ==================== СМЕНА СОБСТВЕННОГО ПАРОЛЯ ====================

@app.route('/api/auth/change-password', methods=['PUT'])
@auth_require_role('admin', 'user', 'viewer')
def change_own_password(current_user=None):
    """
    Смена собственного пароля.
    Принимает JSON: {"old_password": "...", "new_password": "..."}
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Требуется JSON'}), 400

    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    valid, msg = validate_password(new_password)
    if not valid:
        return jsonify({'error': msg}), 400

    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (current_user['user_id'],)
        ).fetchone()

    if not user or not check_password_hash(user['password_hash'], old_password):
        return jsonify({'error': 'Неверный текущий пароль'}), 400

    new_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_hash, current_user['user_id'])
        )
        # Удаляем все сессии пользователя (принудительный перелогин)
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ?",
            (current_user['user_id'],)
        )

    log_audit(current_user['user_id'], 'password_changed', 'Самостоятельная смена пароля', get_client_ip())
    logger.info(f"[{current_user['username']}] Сменил пароль")

    return jsonify({'success': True, 'message': 'Пароль изменён. Войдите заново.'})


# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (только admin) ====================

@app.route('/api/users', methods=['GET'])
@auth_require_role('admin')
def list_users(current_user=None):
    """Список всех пользователей."""
    with get_db() as conn:
        users = conn.execute(
            """SELECT id, username, display_name, role, is_active,
                      created_at, updated_at, last_login
               FROM users ORDER BY id"""
        ).fetchall()

    return jsonify({
        'users': [dict(u) for u in users]
    })


@app.route('/api/users', methods=['POST'])
@auth_require_role('admin')
def create_user(current_user=None):
    """
    Создание нового пользователя.
    Принимает JSON: {"username": "...", "password": "...", "role": "user", "display_name": "..."}
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Требуется JSON'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'user')
    display_name = data.get('display_name', '').strip()

    # Валидация
    valid, msg = validate_username(username)
    if not valid:
        return jsonify({'error': msg}), 400

    valid, msg = validate_password(password)
    if not valid:
        return jsonify({'error': msg}), 400

    if role not in ('admin', 'user', 'viewer'):
        return jsonify({'error': 'Роль должна быть: admin, user или viewer'}), 400

    password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO users (username, password_hash, display_name, role)
                   VALUES (?, ?, ?, ?)""",
                (username, password_hash, display_name, role)
            )
    except sqlite3.IntegrityError:
        return jsonify({'error': f'Пользователь "{username}" уже существует'}), 409

    log_audit(current_user['user_id'], 'user_created',
              f'Создан пользователь {username} (role={role})', get_client_ip())
    logger.info(f"[{current_user['username']}] Создал пользователя {username} (role={role})")

    return jsonify({'success': True, 'message': f'Пользователь "{username}" создан'}), 201


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@auth_require_role('admin')
def update_user(user_id, current_user=None):
    """
    Обновление пользователя (роль, display_name, is_active).
    Принимает JSON: {"role": "...", "display_name": "...", "is_active": true/false}
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Требуется JSON'}), 400

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    # Запрет редактирования самого себя (защита от случайного лишения прав)
    if user_id == current_user['user_id']:
        return jsonify({'error': 'Нельзя редактировать свой аккаунт через этот эндпоинт'}), 400

    updates = []
    params = []
    details_parts = []

    if 'role' in data:
        role = data['role']
        if role not in ('admin', 'user', 'viewer'):
            return jsonify({'error': 'Роль должна быть: admin, user или viewer'}), 400
        updates.append("role = ?")
        params.append(role)
        details_parts.append(f"role→{role}")

    if 'display_name' in data:
        updates.append("display_name = ?")
        params.append(data['display_name'].strip())
        details_parts.append(f"display_name изменено")

    if 'is_active' in data:
        is_active = bool(data['is_active'])
        updates.append("is_active = ?")
        params.append(is_active)
        details_parts.append(f"is_active→{is_active}")

        # При блокировке — удаляем все сессии пользователя
        if not is_active:
            with get_db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    if not updates:
        return jsonify({'error': 'Нет данных для обновления'}), 400

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(user_id)

    with get_db() as conn:
        conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            params
        )

    details = f"Пользователь {user['username']}: {', '.join(details_parts)}"
    log_audit(current_user['user_id'], 'user_updated', details, get_client_ip())
    logger.info(f"[{current_user['username']}] {details}")

    return jsonify({'success': True, 'message': 'Пользователь обновлён'})


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@auth_require_role('admin')
def delete_user(user_id, current_user=None):
    """Удаление пользователя."""
    if user_id == current_user['user_id']:
        return jsonify({'error': 'Нельзя удалить самого себя'}), 400

    with get_db() as conn:
        user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404

        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    log_audit(current_user['user_id'], 'user_deleted',
              f'Удалён пользователь {user["username"]}', get_client_ip())
    logger.info(f"[{current_user['username']}] Удалил пользователя {user['username']}")

    return jsonify({'success': True, 'message': f'Пользователь "{user["username"]}" удалён'})


@app.route('/api/users/<int:user_id>/password', methods=['PUT'])
@auth_require_role('admin')
def reset_user_password(user_id, current_user=None):
    """
    Сброс пароля пользователя администратором.
    Принимает JSON: {"new_password": "..."}
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Требуется JSON'}), 400

    new_password = data.get('new_password', '')
    valid, msg = validate_password(new_password)
    if not valid:
        return jsonify({'error': msg}), 400

    with get_db() as conn:
        user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404

        new_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_hash, user_id)
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    log_audit(current_user['user_id'], 'password_reset',
              f'Сброшен пароль пользователя {user["username"]}', get_client_ip())
    logger.info(f"[{current_user['username']}] Сбросил пароль пользователя {user['username']}")

    return jsonify({'success': True, 'message': 'Пароль сброшен'})


# ==================== ЖУРНАЛ АУДИТА (только admin) ====================

@app.route('/api/audit', methods=['GET'])
@auth_require_role('admin')
def get_audit_log(current_user=None):
    """
    Просмотр журнала аудита.
    Параметры: ?limit=50&offset=0&action=login&username=ivanov
    """
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    action_filter = request.args.get('action', '')
    username_filter = request.args.get('username', '')

    query = """
        SELECT a.*, u.username
        FROM audit_log a
        LEFT JOIN users u ON a.user_id = u.id
        WHERE 1=1
    """
    params = []

    if action_filter:
        query += " AND a.action = ?"
        params.append(action_filter)

    if username_filter:
        query += " AND u.username = ?"
        params.append(username_filter)

    query += " ORDER BY a.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        logs = conn.execute(query, params).fetchall()

    return jsonify({
        'logs': [dict(log) for log in logs],
        'limit': limit,
        'offset': offset
    })


# ==================== HEALTH CHECK ====================

@app.route('/health', methods=['GET'])
def health():
    """Проверка работоспособности сервиса."""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({'status': 'healthy', 'service': 'auth'})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


# ==================== CLI ====================

def cli_create_user(username, password, role, display_name=''):
    """Создаёт пользователя из командной строки."""
    valid, msg = validate_username(username)
    if not valid:
        print(f"Ошибка: {msg}")
        return False

    valid, msg = validate_password(password)
    if not valid:
        print(f"Ошибка: {msg}")
        return False

    if role not in ('admin', 'user', 'viewer'):
        print("Ошибка: роль должна быть admin, user или viewer")
        return False

    password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
                (username, password_hash, display_name or username, role)
            )
        print(f'Пользователь "{username}" создан (роль: {role})')
        return True
    except sqlite3.IntegrityError:
        print(f'Ошибка: пользователь "{username}" уже существует')
        return False


def cli_reset_password(username, new_password):
    """Сбрасывает пароль из командной строки."""
    valid, msg = validate_password(new_password)
    if not valid:
        print(f"Ошибка: {msg}")
        return False

    new_hash = generate_password_hash(new_password, method='pbkdf2:sha256')

    with get_db() as conn:
        result = conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
            (new_hash, username)
        )
        if result.rowcount == 0:
            print(f'Ошибка: пользователь "{username}" не найден')
            return False
        conn.execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
            (username,)
        )

    print(f'Пароль пользователя "{username}" сброшен')
    return True


def cli_list_users():
    """Выводит список пользователей."""
    with get_db() as conn:
        users = conn.execute(
            "SELECT id, username, display_name, role, is_active, created_at, last_login FROM users ORDER BY id"
        ).fetchall()

    if not users:
        print("Пользователей нет. Создайте первого:")
        print("  python auth.py --create-user admin --password ваш_пароль --role admin")
        return

    print(f"\n{'ID':<4} {'Логин':<16} {'Имя':<20} {'Роль':<8} {'Активен':<8} {'Последний вход'}")
    print("-" * 80)
    for u in users:
        active = "Да" if u['is_active'] else "Нет"
        last = u['last_login'] or "—"
        print(f"{u['id']:<4} {u['username']:<16} {(u['display_name'] or '—'):<20} {u['role']:<8} {active:<8} {last}")
    print()


# ==================== ТОЧКА ВХОДА ====================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Auth Service для RAG Copilot')

    # CLI команды
    parser.add_argument('--create-user', metavar='USERNAME', help='Создать пользователя')
    parser.add_argument('--password', metavar='PASSWORD', help='Пароль для нового пользователя')
    parser.add_argument('--role', default='user', choices=['admin', 'user', 'viewer'],
                        help='Роль пользователя (по умолчанию: user)')
    parser.add_argument('--display-name', default='', help='Отображаемое имя')
    parser.add_argument('--reset-password', metavar='USERNAME', help='Сбросить пароль пользователя')
    parser.add_argument('--new-password', metavar='PASSWORD', help='Новый пароль')
    parser.add_argument('--list-users', action='store_true', help='Список пользователей')

    args = parser.parse_args()

    # Инициализация БД в любом случае
    init_db()

    if args.create_user:
        if not args.password:
            print("Ошибка: укажите --password")
            sys.exit(1)
        success = cli_create_user(args.create_user, args.password, args.role, args.display_name)
        sys.exit(0 if success else 1)

    elif args.reset_password:
        if not args.new_password:
            print("Ошибка: укажите --new-password")
            sys.exit(1)
        success = cli_reset_password(args.reset_password, args.new_password)
        sys.exit(0 if success else 1)

    elif args.list_users:
        cli_list_users()
        sys.exit(0)

    else:
        # Запуск как сервис
        cleanup_expired_sessions()
        logger.info(f"Auth сервис запускается на {HOST}:{PORT}")
        app.run(host=HOST, port=PORT, debug=False)
