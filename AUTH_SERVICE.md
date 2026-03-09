# Микросервис авторизации (auth) — Полное описание

## 1. Обзор

Микросервис `auth` — централизованная система авторизации для RAG Copilot. Хранит пользователей в SQLite, выдаёт подписанные токены (HMAC-SHA256), предоставляет API для логина и управления пользователями.

**Принцип работы:** пользователь логинится → получает access-токен (30 мин) и refresh-токен (7 дней) → access-токен передаётся в каждом запросе → любой сервис проверяет его локально по общему секретному ключу, без обращения к auth-сервису.

**Новые зависимости Python:** не требуются. Используется только то, что уже есть в проекте (Flask, werkzeug) и стандартная библиотека Python.

---

## 2. Структура файлов

```
RAG-Copilot/
├── .env                            # Добавить: AUTH_SECRET_KEY
├── services/
│   ├── common/                     # НОВАЯ директория — общие модули
│   │   ├── __init__.py
│   │   ├── tokens.py               # Создание и проверка токенов
│   │   └── auth_middleware.py       # Декораторы авторизации для Flask
│   │   └── auth_middleware_fastapi.py  # Декораторы авторизации для FastAPI
│   │
│   ├── auth/                       # НОВЫЙ микросервис
│   │   ├── auth.py                 # Основной файл сервиса
│   │   ├── config.json             # Конфигурация
│   │   └── README.md               # Документация
│   │
│   ├── websearch/                  # Изменения в существующих сервисах
│   │   └── websearch.py            # + авторизация + логирование пользователя
│   ├── web_ui/
│   │   └── web_ui_service.py       # Замена Basic Auth на токены
│   ├── webconfig/
│   │   └── webconfig.py            # Замена Basic Auth на токены
│   └── webadmin/
│       └── ...                     # + управление пользователями
```

---

## 3. Общий секретный ключ

В корневой `.env` проекта добавить:

```env
# Секретный ключ для подписи токенов (общий для всех сервисов)
# Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"
AUTH_SECRET_KEY=your_random_secret_key_here

# Порт auth-сервиса
AUTH_SERVICE_PORT=5050
AUTH_SERVICE_HOST=localhost
```

Каждый сервис загружает этот ключ через `python-dotenv` (уже используется в проекте).

---

## 4. Код: `services/common/__init__.py`

```python
# Пустой файл — делает common пакетом Python
```

---

## 5. Код: `services/common/tokens.py`

```python
"""
Модуль создания и проверки подписанных токенов.
Аналог JWT на стандартной библиотеке Python (hmac + hashlib + base64 + json).
Ноль внешних зависимостей.
"""

import hmac
import hashlib
import json
import base64
import time


def _b64encode(data: bytes) -> str:
    """URL-safe base64 кодирование без паддинга."""
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _b64decode(s: str) -> bytes:
    """URL-safe base64 декодирование с восстановлением паддинга."""
    s += '=' * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(payload: dict, secret: str, ttl: int = 1800) -> str:
    """
    Создаёт подписанный токен.

    Args:
        payload: данные для включения в токен (username, role и т.д.)
        secret: секретный ключ для подписи
        ttl: время жизни токена в секундах (по умолчанию 1800 = 30 минут)

    Returns:
        строка формата "base64_payload.hmac_signature"
    """
    token_data = {
        **payload,
        'iat': int(time.time()),
        'exp': int(time.time()) + ttl
    }
    body = _b64encode(json.dumps(token_data, ensure_ascii=False).encode('utf-8'))
    signature = hmac.new(
        secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def verify_token(token: str, secret: str) -> dict:
    """
    Проверяет подпись и срок действия токена.

    Args:
        token: строка токена
        secret: секретный ключ

    Returns:
        dict с payload токена

    Raises:
        ValueError: при невалидном токене, неверной подписи или истечении срока
    """
    if not token or not isinstance(token, str):
        raise ValueError("Токен не предоставлен")

    parts = token.split('.')
    if len(parts) != 2:
        raise ValueError("Неверный формат токена")

    body, signature = parts

    # Проверка подписи (compare_digest защищает от timing-атак)
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Неверная подпись токена")

    # Декодирование payload
    try:
        payload = json.loads(_b64decode(body))
    except (json.JSONDecodeError, Exception):
        raise ValueError("Невозможно декодировать payload токена")

    # Проверка срока действия
    if payload.get('exp', 0) < time.time():
        raise ValueError("Токен истёк")

    return payload
```

---

## 6. Код: `services/common/auth_middleware.py`

```python
"""
Middleware авторизации для Flask-сервисов (web_ui, webconfig, manager, auth).
"""

from functools import wraps
from flask import request, jsonify, redirect
import os
import sys

# Добавляем common в путь импорта
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from tokens import verify_token


def _extract_token():
    """
    Извлекает токен из запроса.
    Проверяет в порядке: заголовок Authorization → cookie access_token.
    """
    # 1. Заголовок Authorization: Bearer <token>
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]

    # 2. Cookie
    token = request.cookies.get('access_token')
    if token:
        return token

    return None


def require_auth(secret_key):
    """
    Декоратор: требует авторизации (любая роль).

    Использование:
        @app.route('/api/something')
        @require_auth(SECRET_KEY)
        def my_endpoint(current_user=None):
            print(current_user['username'])
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            token = _extract_token()
            if not token:
                # Если запрос из браузера (Accept: text/html) — редирект на логин
                if 'text/html' in request.headers.get('Accept', ''):
                    return redirect('/login')
                return jsonify({'error': 'Требуется авторизация'}), 401

            try:
                payload = verify_token(token, secret_key)
            except ValueError as e:
                if 'text/html' in request.headers.get('Accept', ''):
                    return redirect('/login')
                return jsonify({'error': str(e)}), 401

            # Проверяем что пользователь активен
            if not payload.get('is_active', True):
                return jsonify({'error': 'Пользователь заблокирован'}), 403

            kwargs['current_user'] = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_role(*allowed_roles, secret_key):
    """
    Декоратор: требует авторизации + определённую роль.

    Использование:
        @app.route('/api/admin/users')
        @require_role('admin', secret_key=SECRET_KEY)
        def admin_only(current_user=None):
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            token = _extract_token()
            if not token:
                if 'text/html' in request.headers.get('Accept', ''):
                    return redirect('/login')
                return jsonify({'error': 'Требуется авторизация'}), 401

            try:
                payload = verify_token(token, secret_key)
            except ValueError as e:
                if 'text/html' in request.headers.get('Accept', ''):
                    return redirect('/login')
                return jsonify({'error': str(e)}), 401

            if not payload.get('is_active', True):
                return jsonify({'error': 'Пользователь заблокирован'}), 403

            if payload.get('role') not in allowed_roles:
                return jsonify({'error': 'Недостаточно прав'}), 403

            kwargs['current_user'] = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator
```

---

## 7. Код: `services/common/auth_middleware_fastapi.py`

```python
"""
Middleware авторизации для FastAPI-сервисов (websearch).
"""

import os
import sys
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from tokens import verify_token


async def get_current_user(request: Request, secret_key: str) -> dict:
    """
    Извлекает и проверяет текущего пользователя из запроса.

    Args:
        request: FastAPI Request
        secret_key: секретный ключ для проверки токена

    Returns:
        dict с данными пользователя (username, role, user_id)

    Raises:
        HTTPException 401: если токен отсутствует или невалиден
        HTTPException 403: если пользователь заблокирован
    """
    # Извлекаем токен: cookie → заголовок
    token = request.cookies.get('access_token')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Требуется авторизация")

    try:
        payload = verify_token(token, secret_key)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    if not payload.get('is_active', True):
        raise HTTPException(status_code=403, detail="Пользователь заблокирован")

    return payload


async def get_current_user_optional(request: Request, secret_key: str) -> dict | None:
    """
    То же что get_current_user, но возвращает None вместо ошибки
    если пользователь не авторизован. Для страниц, которые работают
    и без авторизации, но показывают разный контент.
    """
    try:
        return await get_current_user(request, secret_key)
    except HTTPException:
        return None
```

---

## 8. Код: `services/auth/config.json`

```json
{
    "host": "0.0.0.0",
    "port": 5050,
    "db_path": "auth.db",
    "access_token_ttl": 1800,
    "refresh_token_ttl": 604800,
    "min_password_length": 6,
    "max_login_attempts": 10,
    "lockout_duration": 300
}
```

Описание параметров:
- `access_token_ttl` — время жизни access-токена в секундах (1800 = 30 минут)
- `refresh_token_ttl` — время жизни refresh-токена в секундах (604800 = 7 дней)
- `min_password_length` — минимальная длина пароля
- `max_login_attempts` — максимум неудачных попыток входа до блокировки
- `lockout_duration` — длительность временной блокировки в секундах (300 = 5 минут)

---

## 9. Код: `services/auth/auth.py`

```python
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
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
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
SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(refresh_token);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
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

def log_audit(user_id, action, details='', ip_address=''):
    """Записывает действие в журнал аудита."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?, ?, ?, ?)",
                (user_id, action, details, ip_address)
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
    """
    with get_db() as conn:
        cutoff = time.time() - LOCKOUT_DURATION
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM audit_log
               WHERE action = 'login_failed'
               AND details LIKE ?
               AND created_at > datetime(?, 'unixepoch')""",
            (f'%username={username}%', cutoff)
        ).fetchone()
        if row and row['cnt'] >= MAX_LOGIN_ATTEMPTS:
            return True, LOCKOUT_DURATION
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
CORS(app)


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
        log_audit(None, 'login_failed', f'username={username}', client_ip)
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
```

---

## 10. Интеграция с websearch (FastAPI) — логирование пользователя

### Изменения в `services/websearch/websearch.py`

#### 10.1. Добавить в начало файла (импорты):

```python
# Добавить к существующим импортам:
from pathlib import Path
from dotenv import load_dotenv

# Загрузка секретного ключа
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')
load_dotenv()

# Добавить common в путь
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
from auth_middleware_fastapi import get_current_user, get_current_user_optional

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')
```

#### 10.2. Добавить эндпоинт страницы логина:

```python
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа."""
    return templates.TemplateResponse("login.html", {"request": request})
```

#### 10.3. Изменить эндпоинт поиска:

```python
@app.post("/api/search")
async def search(query_data: SearchIn, request: Request):
    # Получаем текущего пользователя
    user = await get_current_user(request, AUTH_SECRET_KEY)

    query = query_data.query.strip()
    enable_generation = query_data.enable_generation and config.ENABLE_GENERATION

    # Логирование с указанием пользователя
    logger.info(
        f"[{user['username']}] role={user['role']} "
        f"query=\"{query[:100]}\" "
        f"generation={enable_generation} "
        f"ip={request.client.host}"
    )

    # ... далее существующая логика поиска без изменений ...
```

#### 10.4. Защитить главную страницу:

```python
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    user = await get_current_user_optional(request, AUTH_SECRET_KEY)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user
    })
```

#### 10.5. Формат логов websearch после интеграции:

```
2025-01-15 14:32:01 - INFO - [ivanov] role=user query="как настроить индексацию" generation=True ip=192.168.1.45
2025-01-15 14:32:15 - INFO - [petrov] role=viewer query="политика безопасности" generation=False ip=192.168.1.12
2025-01-15 14:33:02 - INFO - [ivanov] role=user query="ошибки при конвертации PDF" generation=True ip=192.168.1.45
```

---

## 11. Интеграция с web_ui (Flask) — замена Basic Auth

### Изменения в `services/web_ui/web_ui_service.py`

#### 11.1. Заменить блок импортов и аутентификации:

```python
# УБРАТЬ:
# AUTH_REQUIRED = os.getenv("WEB_UI_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no")
# AUTH_USERNAME = os.getenv("WEB_UI_USERNAME", "")
# AUTH_PASSWORD = os.getenv("WEB_UI_PASSWORD", "")

# ДОБАВИТЬ:
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
from auth_middleware import require_auth, require_role

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')
```

#### 11.2. Заменить enforce_auth и декораторы:

```python
# УБРАТЬ: @app.before_request enforce_auth() и все функции _auth_required_response, _is_auth_valid

# ЗАМЕНИТЬ декораторы на маршрутах:

# Было:
# @require_auth
# def protected_api_start_manager():

# Стало:
@app.route('/api/control/start', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def protected_api_start_manager(current_user=None):
    logger.info(f"[{current_user['username']}] Запуск обработки")
    return api_start_manager()

@app.route('/api/control/stop', methods=['POST'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def protected_api_stop_manager(current_user=None):
    logger.info(f"[{current_user['username']}] Остановка обработки")
    return api_stop_manager()

@app.route('/api/file/<int:file_id>/retry', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def protected_api_retry_file(file_id, current_user=None):
    logger.info(f"[{current_user['username']}] Повторная обработка файла {file_id}")
    return api_retry_file_processing(file_id)

@app.route('/api/file/<int:file_id>', methods=['DELETE'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def protected_api_delete_file(file_id, current_user=None):
    logger.info(f"[{current_user['username']}] Удаление файла {file_id}")
    return api_delete_file(file_id)

@app.route('/api/manual_scan', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def protected_api_manual_scan(current_user=None):
    logger.info(f"[{current_user['username']}] Ручное сканирование")
    return api_manual_scan()
```

---

## 12. Интеграция с webconfig (Flask) — замена Basic Auth

### Изменения в `services/webconfig/webconfig.py`

Аналогично web_ui:

```python
# УБРАТЬ блок:
# AUTH_REQUIRED = ...
# AUTH_USERNAME = ...
# AUTH_PASSWORD = ...
# def _auth_required_response(): ...
# def _is_auth_valid(): ...
# @app.before_request def enforce_auth(): ...

# ДОБАВИТЬ:
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
from auth_middleware import require_auth

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')

# Защитить ВСЕ эндпоинты webconfig (только admin):
@app.before_request
def enforce_auth():
    # Пропускаем health check
    if request.path == '/health':
        return None
    # Пропускаем страницу логина
    if request.path == '/login':
        return None
    # Пропускаем статику
    if request.path.startswith('/static/'):
        return None

    token = request.cookies.get('access_token')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]

    if not token:
        if 'text/html' in request.headers.get('Accept', ''):
            return redirect('/login')
        return jsonify({'error': 'Требуется авторизация'}), 401

    try:
        payload = verify_token(token, AUTH_SECRET_KEY)
    except ValueError as e:
        if 'text/html' in request.headers.get('Accept', ''):
            return redirect('/login')
        return jsonify({'error': str(e)}), 401

    if payload.get('role') != 'admin':
        return jsonify({'error': 'Только для администраторов'}), 403

    # Сохраняем пользователя в контексте запроса Flask
    from flask import g
    g.current_user = payload
```

---

## 13. Интеграция с manager (Flask) — замена Basic Auth

### Изменения в `services/manager/manager.py`

```python
# УБРАТЬ:
# AUTH_REQUIRED = os.getenv("MANAGER_REQUIRE_AUTH", "false").lower() in ("1", "true", "yes")
# AUTH_USERNAME = os.getenv("MANAGER_USERNAME", "admin")
# AUTH_PASSWORD = os.getenv("MANAGER_PASSWORD", "admin")
# Все функции _auth_required_response, _is_auth_valid, require_auth

# ДОБАВИТЬ:
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
from auth_middleware import require_role

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')

# ЗАМЕНИТЬ декораторы:
# Было: @require_auth
# Стало: @require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
```

---

## 14. Матрица доступа по ролям

| Сервис / Действие          | admin | user | viewer |
|---------------------------|-------|------|--------|
| **websearch** — поиск      | ✓     | ✓    | ✓      |
| **websearch** — генерация  | ✓     | ✓    | ✗      |
| **web_ui** — просмотр      | ✓     | ✓    | ✓      |
| **web_ui** — запуск/стоп   | ✓     | ✓    | ✗      |
| **web_ui** — удаление      | ✓     | ✗    | ✗      |
| **webconfig** — всё        | ✓     | ✗    | ✗      |
| **webadmin** — всё         | ✓     | ✗    | ✗      |
| **auth** — смена пароля    | ✓     | ✓    | ✓      |
| **auth** — CRUD пользователей | ✓  | ✗    | ✗      |
| **auth** — журнал аудита   | ✓     | ✗    | ✗      |

---

## 15. Добавить auth в скрипты запуска

### `start_all_services.py` — добавить в список сервисов:

```python
services = [
    "auth",        # ← ДОБАВИТЬ ПЕРВЫМ (остальные зависят от него)
    "chunker",
    "converter",
    "embedder",
    "indexer",
    "manager",
    "searcher",
    "websearch",
    "webconfig",
    "web_ui"
]
```

### `start_all_services.sh` — добавить:

```bash
SERVICES_PORTS["auth"]="5050"

# Добавить auth первым в массив SERVICES:
SERVICES=("auth" "chunker" "converter" "embedder" "indexer" "manager" "searcher" "websearch" "webconfig" "web_ui" "generator")
```

### `check_status.sh` — добавить:

```bash
SERVICES=("auth" "chunker" "converter" "embedder" "indexer" "manager" "searcher" "websearch" "webconfig" "web_ui" "generator")
```

---

## 16. Порядок внедрения (пошагово)

### Шаг 1: Общие модули
```bash
mkdir -p services/common
# Создать __init__.py, tokens.py, auth_middleware.py, auth_middleware_fastapi.py
```

### Шаг 2: Auth-сервис
```bash
mkdir -p services/auth
# Создать auth.py, config.json
# Добавить AUTH_SECRET_KEY в .env
```

### Шаг 3: Создать первого администратора
```bash
cd services/auth
python auth.py --create-user admin --password ваш_пароль --role admin
python auth.py --list-users  # проверка
```

### Шаг 4: Запустить auth-сервис
```bash
python auth.py
# Проверить: curl http://localhost:5050/health
```

### Шаг 5: Проверить логин через API
```bash
curl -X POST http://localhost:5050/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "ваш_пароль"}'
# Должен вернуть access_token
```

### Шаг 6: Интегрировать в websearch
Внести изменения из раздела 10. Перезапустить websearch.

### Шаг 7: Интегрировать в остальные сервисы
web_ui (раздел 11), webconfig (раздел 12), manager (раздел 13).

### Шаг 8: Обновить скрипты запуска
Раздел 15.

### Шаг 9: Создать пользователей
```bash
cd services/auth
python auth.py --create-user ivanov --password пароль1 --role user --display-name "Иван Иванов"
python auth.py --create-user petrov --password пароль2 --role viewer --display-name "Пётр Петров"
```

---

## 17. Шаблон страницы логина (для всех веб-сервисов)

Создать файл `services/common/templates/login.html` и скопировать в templates каждого веб-сервиса:

```html
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход — RAG Copilot</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f5f5f5;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh;
        }
        .login-card {
            background: white; border-radius: 8px; padding: 40px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 100%; max-width: 400px;
        }
        .login-card h1 { text-align: center; margin-bottom: 8px; color: #333; font-size: 24px; }
        .login-card .subtitle { text-align: center; margin-bottom: 24px; color: #666; font-size: 14px; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 4px; color: #555; font-size: 14px; }
        .form-group input {
            width: 100%; padding: 10px 12px; border: 1px solid #ddd;
            border-radius: 4px; font-size: 16px;
        }
        .form-group input:focus { outline: none; border-color: #4a90d9; }
        .btn-login {
            width: 100%; padding: 12px; background: #4a90d9; color: white;
            border: none; border-radius: 4px; font-size: 16px; cursor: pointer;
            margin-top: 8px;
        }
        .btn-login:hover { background: #357abd; }
        .btn-login:disabled { background: #aaa; cursor: not-allowed; }
        .error-msg { color: #d32f2f; font-size: 14px; margin-top: 12px; text-align: center; display: none; }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>RAG Copilot</h1>
        <p class="subtitle">Введите учётные данные для входа</p>
        <div class="form-group">
            <label for="username">Имя пользователя</label>
            <input type="text" id="username" autocomplete="username" autofocus>
        </div>
        <div class="form-group">
            <label for="password">Пароль</label>
            <input type="password" id="password" autocomplete="current-password">
        </div>
        <button class="btn-login" id="loginBtn" onclick="doLogin()">Войти</button>
        <div class="error-msg" id="errorMsg"></div>
    </div>

    <script>
        // Адрес auth-сервиса (в локальной сети)
        const AUTH_URL = window.location.protocol + '//' + window.location.hostname + ':5050';

        // Вход по нажатию Enter
        document.getElementById('password').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') doLogin();
        });
        document.getElementById('username').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') document.getElementById('password').focus();
        });

        async function doLogin() {
            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;
            const errorEl = document.getElementById('errorMsg');
            const btn = document.getElementById('loginBtn');

            errorEl.style.display = 'none';

            if (!username || !password) {
                errorEl.textContent = 'Введите имя пользователя и пароль';
                errorEl.style.display = 'block';
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Вход...';

            try {
                const response = await fetch(AUTH_URL + '/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password }),
                    credentials: 'include'
                });

                const data = await response.json();

                if (response.ok) {
                    // Cookie устанавливается auth-сервисом,
                    // но для кросс-доменных запросов дублируем через JS
                    if (data.access_token) {
                        document.cookie = `access_token=${data.access_token}; path=/; SameSite=Lax; max-age=1800`;
                    }
                    window.location.href = '/';
                } else {
                    errorEl.textContent = data.error || 'Ошибка входа';
                    errorEl.style.display = 'block';
                }
            } catch (err) {
                errorEl.textContent = 'Сервис авторизации недоступен';
                errorEl.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Войти';
            }
        }
    </script>
</body>
</html>
```

---

## 18. gRPC-сервисы (chunker, embedder, indexer, searcher, generator)

Изменения **не требуются**. Эти сервисы не доступны пользователям напрямую — они вызываются только другими сервисами внутри системы через gRPC. Авторизация для них избыточна в локальной сети.

---

## 19. Сводка всех файлов

| Файл | Действие | Описание |
|------|----------|----------|
| `.env` | Изменить | Добавить `AUTH_SECRET_KEY` |
| `services/common/__init__.py` | Создать | Пустой файл |
| `services/common/tokens.py` | Создать | Модуль токенов (30 строк, 0 зависимостей) |
| `services/common/auth_middleware.py` | Создать | Декораторы для Flask |
| `services/common/auth_middleware_fastapi.py` | Создать | Декораторы для FastAPI |
| `services/auth/auth.py` | Создать | Основной сервис (~500 строк) |
| `services/auth/config.json` | Создать | Конфигурация |
| `services/common/templates/login.html` | Создать | Шаблон страницы логина |
| `services/websearch/websearch.py` | Изменить | + авторизация + логирование пользователя |
| `services/web_ui/web_ui_service.py` | Изменить | Замена Basic Auth на токены |
| `services/webconfig/webconfig.py` | Изменить | Замена Basic Auth на токены |
| `services/manager/manager.py` | Изменить | Замена Basic Auth на токены |
| `start_all_services.py` | Изменить | Добавить auth |
| `start_all_services.sh` | Изменить | Добавить auth |
| `check_status.sh` | Изменить | Добавить auth |
