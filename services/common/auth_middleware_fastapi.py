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
