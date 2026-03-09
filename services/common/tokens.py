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
