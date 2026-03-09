# Common Modules — Общие модули авторизации

Общие библиотеки и утилиты для системы авторизации RAG Copilot.

## Структура

```
services/common/
├── __init__.py              # Пакет Python
├── tokens.py                # Создание и проверка токенов
├── auth_middleware.py       # Middleware для Flask
├── auth_middleware_fastapi.py  # Middleware для FastAPI
└── templates/
    └── login.html           # Шаблон страницы входа
```

## Модули

### tokens.py

Модуль создания и проверки подписанных токенов на основе HMAC-SHA256.

**Функции:**
- `create_token(payload, secret, ttl)` — создаёт токен с полезной нагрузкой
- `verify_token(token, secret)` — проверяет подпись и срок действия

**Пример использования:**
```python
from tokens import create_token, verify_token

# Создание токена
token = create_token(
    payload={'user_id': 1, 'username': 'admin', 'role': 'admin'},
    secret='my-secret-key',
    ttl=1800  # 30 минут
)

# Проверка токена
try:
    payload = verify_token(token, 'my-secret-key')
    print(f"User: {payload['username']}")
except ValueError as e:
    print(f"Ошибка: {e}")
```

### auth_middleware.py

Декораторы авторизации для Flask-приложений.

**Декораторы:**
- `require_auth(secret_key)` — требует авторизации (любая роль)
- `require_role(*roles, secret_key)` — требует определённую роль

**Пример использования:**
```python
from flask import Flask
from auth_middleware import require_auth, require_role

app = Flask(__name__)
SECRET_KEY = os.getenv('AUTH_SECRET_KEY')

@app.route('/api/data')
@require_auth(SECRET_KEY)
def get_data(current_user=None):
    return {'user': current_user['username']}

@app.route('/api/admin')
@require_role('admin', secret_key=SECRET_KEY)
def admin_panel(current_user=None):
    return {'message': 'Admin access granted'}
```

### auth_middleware_fastapi.py

Middleware авторизации для FastAPI-приложений.

**Функции:**
- `get_current_user(request, secret_key)` — получает текущего пользователя
- `get_current_user_optional(request, secret_key)` — опциональный пользователь

**Пример использования:**
```python
from fastapi import FastAPI, Request
from auth_middleware_fastapi import get_current_user

app = FastAPI()
SECRET_KEY = os.getenv('AUTH_SECRET_KEY')

@app.get("/api/profile")
async def profile(request: Request):
    user = await get_current_user(request, SECRET_KEY)
    return {'username': user['username']}
```

## Формат токена

Токен имеет структуру, аналогичную JWT:
```
base64url(payload).hmac_signature
```

**Payload содержит:**
- `user_id` — ID пользователя
- `username` — имя пользователя
- `display_name` — отображаемое имя
- `role` — роль (admin, user, viewer)
- `is_active` — активен ли пользователь
- `iat` — время создания токена
- `exp` — время истечения токена

## Безопасность

- Подпись токенов: HMAC-SHA256
- Защита от timing-атак: `hmac.compare_digest()`
- URL-safe base64 кодирование без паддинга
- Проверка срока действия токена

## Зависимости

Только стандартная библиотека Python:
- `hmac`, `hashlib` — криптография
- `base64` — кодирование
- `json` — сериализация
- `time` — временные метки

## Интеграция

Для использования в сервисе:

1. Добавьте импорт:
```python
sys.path.insert(0, str(Path(__file__).parent.parent / 'common'))
from auth_middleware import require_auth
```

2. Загрузите секретный ключ:
```python
from dotenv import load_dotenv
load_dotenv()
AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY')
```

3. Используйте декораторы на маршрутах.
