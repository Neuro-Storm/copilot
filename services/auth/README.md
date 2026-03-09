# Документация по микросервису авторизации (auth)

## Обзор

Микросервис `auth` — централизованная система авторизации для RAG Copilot. Хранит пользователей в SQLite, выдаёт подписанные токены (HMAC-SHA256), предоставляет API для логина и управления пользователями.

## Принцип работы

1. Пользователь логинится → получает access-токен (30 мин) и refresh-токен (7 дней)
2. Access-токен передаётся в каждом запросе
3. Любой сервис проверяет его локально по общему секретному ключу, без обращения к auth-сервису

## Запуск

```bash
# Создание первого администратора
cd services/auth
python auth.py --create-user admin --password ваш_пароль --role admin

# Запуск сервиса
python auth.py

# Проверка
curl http://localhost:5050/health
```

## API эндпоинты

### Аутентификация
- `POST /api/auth/login` — вход (username, password)
- `POST /api/auth/refresh` — обновление access-токена
- `POST /api/auth/logout` — выход
- `GET /api/auth/me` — информация о текущем пользователе
- `PUT /api/auth/change-password` — смена пароля

### Управление пользователями (только admin)
- `GET /api/users` — список пользователей
- `POST /api/users` — создание пользователя
- `PUT /api/users/<id>` — обновление пользователя
- `DELETE /api/users/<id>` — удаление пользователя
- `PUT /api/users/<id>/password` — сброс пароля

### Журнал аудита (только admin)
- `GET /api/audit` — просмотр журнала аудита

## CLI команды

```bash
# Создать пользователя
python auth.py --create-user username --password secret --role admin

# Сбросить пароль
python auth.py --reset-password username --new-password newsecret

# Список пользователей
python auth.py --list-users
```

## Роли

| Роль | Описание |
|------|----------|
| `admin` | Полный доступ ко всем функциям |
| `user` | Стандартный пользователь |
| `viewer` | Только просмотр |

## Конфигурация

Файл `config.json`:
- `host` — хост сервера (по умолчанию: 0.0.0.0)
- `port` — порт сервера (по умолчанию: 5050)
- `access_token_ttl` — время жизни access-токена в секундах (1800 = 30 минут)
- `refresh_token_ttl` — время жизни refresh-токена (604800 = 7 дней)
- `min_password_length` — минимальная длина пароля (6)
- `max_login_attempts` — максимум неудачных попыток входа (10)
- `lockout_duration` — длительность блокировки в секундах (300 = 5 минут)

## Безопасность

- Пароли хешируются через PBKDF2-SHA256
- Токены подписываются через HMAC-SHA256
- Защита от timing-атак через `compare_digest`
- Блокировка после множественных неудачных попыток входа
- Журнал аудита всех действий
