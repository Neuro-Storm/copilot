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
