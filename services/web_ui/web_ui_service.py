#!/usr/bin/env python3
"""
Микросервис веб-интерфейса для RAG-системы (Retrieval-Augmented Generation).

Этот микросервис предоставляет веб-интерфейс для мониторинга и управления
процессом обработки файлов в RAG-системе. Он взаимодействует с основным
сервисом через HTTP API, обеспечивая удобный способ просмотра статуса файлов,
управления процессом обработки и диагностики проблем.

Основные функции:
- Отображение статуса системы и статистики файлов
- Просмотр списка файлов с фильтрацией и пагинацией
- Управление процессом обработки (запуск/остановка)
- Повторная обработка файлов с ошибками
- Удаление файлов из системы
- Ручное сканирование директории

Микросервис включает в себя:
- Механизм кэширования GET-запросов для повышения производительности
- Токенную аутентификацию через auth-сервис
- Обработку ошибок и логирование событий
- Интеграцию с системой конфигурации через файлы и переменные окружения
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, render_template, jsonify, request, redirect, url_for, g
from flask_cors import CORS
import logging
from functools import wraps
import hashlib

# Загрузка переменных окружения из .env файла
from dotenv import load_dotenv
load_dotenv()

# Загрузка секретного ключа для авторизации
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')

# Добавляем common в путь для импорта middleware
_common_path = str(Path(__file__).parent.parent / 'common')
if _common_path not in sys.path:
    sys.path.append(_common_path)
from auth_middleware import require_auth, require_role

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

# Адрес основного сервиса
MAIN_SERVICE_URL = os.getenv('MAIN_SERVICE_URL', 'http://localhost:5001')

# Добавление поддержки CORS (Cross-Origin Resource Sharing)
# Позволяет JavaScript-коду на веб-страницах делать запросы к этому сервису
# из других доменов, что необходимо для работы AJAX-запросов
CORS(app)


@app.before_request
def enforce_auth():
    """Проверка авторизации перед каждым запросом."""
    # Пропускаем публичные маршруты
    if request.path in ('/login', '/health'):
        return None
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
        from tokens import verify_token
        payload = verify_token(token, AUTH_SECRET_KEY)
    except ValueError:
        if 'text/html' in request.headers.get('Accept', ''):
            return redirect('/login')
        return jsonify({'error': 'Токен недействителен'}), 401

    if payload.get('role') not in ('admin', 'user', 'viewer'):
        return jsonify({'error': 'Недостаточно прав'}), 403


def get_api_url(endpoint):
    """Формирует полный URL для вызова API основного сервиса.

    Функция объединяет базовый URL основного сервиса с указанным эндпоинтом,
    создавая полный URL для выполнения HTTP-запроса.

    Args:
        endpoint (str): Относительный путь к эндпоинту API (например, '/api/files')

    Returns:
        str: Полный URL для обращения к API основного сервиса
    """
    return f"{MAIN_SERVICE_URL}{endpoint}"


# Простой кэш в памяти для уменьшения количества запросов к основному сервису
# Кэширование применяется только к GET-запросам для повышения производительности
# и уменьшения нагрузки на основной сервис
cache = {}
CACHE_TIMEOUT = 5  # Время жизни кэша в секундах (5 секунд)

def get_cache_key(method, endpoint, params=None, json_data=None):
    """Генерирует уникальный ключ для кэша на основе параметров HTTP-запроса.

    Функция создает уникальный ключ, который используется для хранения и извлечения
    закэшированных ответов HTTP-запросов. Ключ включает в себя метод запроса,
    эндпоинт и дополнительные параметры, чтобы гарантировать, что каждый уникальный
    запрос будет иметь свой собственный кэш.

    Args:
        method (str): HTTP-метод запроса (GET, POST, PUT и т.д.)
        endpoint (str): Конечная точка API
        params (dict, optional): Параметры запроса (для GET-запросов)
        json_data (dict, optional): Данные запроса в формате JSON (для POST/PUT-запросов)

    Returns:
        str: Уникальный строковый ключ для кэширования ответа запроса
    """
    key_str = f"{method}:{endpoint}"
    if params:
        key_str += f":params:{sorted(params.items())}"
    if json_data:
        # Используем MD5 для хеширования JSON-данных, чтобы избежать слишком длинных ключей
        # при передаче больших объемов данных в теле запроса
        key_str += f":json:{hashlib.md5(str(json_data).encode()).hexdigest()}"
    return key_str

def make_request(method, endpoint, use_cache=True, **kwargs):
    """Выполняет HTTP-запрос к основному сервису с поддержкой кэширования, аутентификации и таймаута.

    Функция реализует механизм кэширования для GET-запросов, чтобы уменьшить количество
    обращений к основному сервису и повысить производительность. Для других методов
    запросы выполняются напрямую без кэширования. Функция также автоматически устанавливает
    таймаут по умолчанию и обрабатывает возможные сетевые исключения.

    Args:
        method (str): HTTP метод запроса ('GET', 'POST', 'PUT', 'DELETE', 'PATCH')
        endpoint (str): Конечная точка API основного сервиса
        use_cache (bool): Использовать кэширование для GET-запросов (по умолчанию True)
        **kwargs: Дополнительные параметры для передачи в requests (params, json, headers и т.д.)

    Returns:
        requests.Response: Объект ответа requests с результатами запроса

    Raises:
        requests.exceptions.Timeout: При превышении времени ожидания запроса
        requests.exceptions.ConnectionError: При ошибке подключения к сервису
        requests.exceptions.RequestException: При других ошибках HTTP-запроса

    SECURITY: Все запросы к основному сервису проходят через эту функцию, что позволяет
    контролировать доступ и добавлять дополнительные меры безопасности при необходимости.
    """
    url = get_api_url(endpoint)

    # Пробрасываем токен авторизации в Manager API
    if 'headers' not in kwargs:
        kwargs['headers'] = {}
    try:
        token = request.cookies.get('access_token')
        if token:
            kwargs['headers']['Authorization'] = f'Bearer {token}'
    except RuntimeError:
        pass  # Вне контекста запроса (например, при старте)

    # Генерируем ключ кэша для GET-запросов
    cache_key = None
    if method.upper() == 'GET' and use_cache:
        cache_key = get_cache_key(method, endpoint, kwargs.get('params'), kwargs.get('json'))
        current_time = time.time()

        # Проверяем, есть ли актуальный кэш
        if cache_key in cache:
            cached_response, timestamp = cache[cache_key]
            if current_time - timestamp < CACHE_TIMEOUT:
                # Возвращаем кэшированный ответ, имитируя объект Response
                # Это позволяет использовать кэшированные данные так же,
                # как и настоящие ответы requests, обеспечивая совместимость
                # с остальной частью кода
                import io
                import json as json_module
                response = type('obj', (object,), {
                    'status_code': cached_response.get('status_code', 200),
                    'content': cached_response.get('content', b''),
                    'headers': cached_response.get('headers', {}),
                    'json': lambda _=None: cached_response.get('json_data', {})
                })()
                return response

    # SECURITY: Устанавливаем таймаут по умолчанию для предотвращения долгих блокировок
    # Это также помогает избежать DoS-атак, когда внешний сервис не отвечает
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30  # 30 секунд таймаут

    # Выполняем запрос с обработкой возможных исключений
    try:
        if method.upper() == 'GET':
            response = requests.get(url, **kwargs)
        elif method.upper() == 'POST':
            response = requests.post(url, **kwargs)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, **kwargs)
        elif method.upper() == 'PUT':
            response = requests.put(url, **kwargs)
        elif method.upper() == 'PATCH':
            response = requests.patch(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Кэшируем GET-ответ только если кэширование включено и это GET-запрос
        # Сохраняем не только содержимое ответа, но и метаданные (статус, заголовки)
        # вместе с временной меткой для проверки актуальности кэша
        if method.upper() == 'GET' and use_cache and cache_key:
            cache[cache_key] = ({
                'status_code': response.status_code,
                'content': response.content,
                'headers': dict(response.headers),
                'json_data': response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
            }, time.time())

        return response
    except requests.exceptions.Timeout:
        logger.error(f"Request timeout for {method} {url}")
        raise
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection error for {method} {url}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error for {method} {url}: {e}")
        raise

@app.route('/')
def index():
    """Главная страница с обзором состояния системы и статистикой файлов.

    Отображает сводную информацию о состоянии системы, включая общее количество файлов,
    распределение по статусам, последние обработанные файлы и текущий статус сервиса.
    Страница обновляется динамически с помощью JavaScript каждые 30 секунд.

    Returns:
        HTML-страница index.html с информацией о состоянии системы
    """
    try:
        # Получаем статистику по файлам из основного сервиса
        stats_response = make_request('GET', '/api/stats')
        stats_data = stats_response.json() if stats_response.status_code == 200 else None
        stats = stats_data if isinstance(stats_data, dict) else {'total_files': 0, 'status_counts': {}, 'last_update': None}

        # Получаем последние файлы из основного сервиса
        files_response = make_request('GET', '/api/files?limit=10')
        files_data = files_response.json() if files_response.status_code == 200 else None
        files = files_data.get('files', []) if isinstance(files_data, dict) else []

        # Получаем статус сервиса
        status_response = make_request('GET', '/api/status')
        status_data = status_response.json() if status_response.status_code == 200 else None
        is_running = status_data.get('running', False) if isinstance(status_data, dict) else False

        # Получаем конфигурацию (без чувствительных данных)
        config_response = make_request('GET', '/api/config')
        config_data = config_response.json() if config_response.status_code == 200 else None
        cfg = config_data if isinstance(config_data, dict) else {}

        return render_template('index.html',
                               stats=stats,
                               files=files,
                               is_running=is_running,
                               cfg=cfg,
                               year=datetime.now().year)
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}", exc_info=True)
        return f"Ошибка: {e}", 500

@app.route('/login')
def login():
    """Страница входа."""
    return render_template('login.html')

@app.route('/files')
def files_page():
    """Страница со списком файлов."""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        status_filter = request.args.get('status', '')

        params = {
            'page': page,
            'limit': limit,
            'status': status_filter
        }

        # Получаем файлы — защита от None на каждом шагу
        files = []
        pagination = {}
        total_pages = 1
        total_count = 0

        try:
            files_response = make_request('GET', '/api/files', params=params)
            if files_response and files_response.status_code == 200:
                response_data = files_response.json()
                if isinstance(response_data, dict):
                    files = response_data.get('files', []) or []
                    pagination = response_data.get('pagination', {}) or {}
                    total_pages = pagination.get('total_pages', 1) or 1
                    total_count = pagination.get('total_count', 0) or 0
        except Exception as e:
            logger.error(f"Ошибка получения файлов: {e}")

        # Получаем статусы для фильтра
        statuses = []
        try:
            stats_response = make_request('GET', '/api/stats')
            if stats_response and stats_response.status_code == 200:
                stats_data = stats_response.json()
                if isinstance(stats_data, dict):
                    status_counts = stats_data.get('status_counts', {})
                    if isinstance(status_counts, dict):
                        statuses = list(status_counts.keys())
        except Exception as e:
            logger.error(f"Ошибка получения статусов: {e}")

        return render_template('files.html',
                             files=files,
                             page=page,
                             total_pages=total_pages,
                             total_count=total_count,
                             statuses=statuses)
    except Exception as e:
        logger.error(f"Ошибка на странице файлов: {e}", exc_info=True)
        return f"Ошибка: {e}", 500

@app.route('/api/files')
def api_files():
    """API endpoint для получения списка файлов в JSON формате.

    Этот эндпоинт используется для получения списка файлов с пагинацией и фильтрацией
    для динамического обновления данных на веб-страницах.

    Query Parameters:
        page (int): Номер страницы для пагинации (по умолчанию 1)
        limit (int): Количество файлов на странице (по умолчанию 10)
        status (str): Фильтр по статусу файла (необязательный)

    Returns:
        JSON-ответ с информацией о файлах и метаданными пагинации
    """
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 10))
        status_filter = request.args.get('status', '')

        params = {
            'page': page,
            'limit': limit,
            'status': status_filter
        }

        response = make_request('GET', '/api/files', params=params)
        if response.status_code == 200:
            return response.content, 200, {'Content-Type': 'application/json'}
        else:
            return jsonify({'error': 'Failed to fetch files'}), 500
    except Exception as e:
        logger.error(f"Ошибка в API файлов: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats')
def api_stats():
    """API endpoint для получения статистики по файлам.

    Возвращает общую статистику по файлам в системе, включая общее количество файлов,
    распределение по статусам и время последнего обновления.

    Returns:
        JSON-ответ с информацией о статистике файлов
    """
    try:
        response = make_request('GET', '/api/stats')
        if response.status_code == 200:
            return response.content, 200, {'Content-Type': 'application/json'}
        else:
            return jsonify({'error': 'Failed to fetch stats'}), 500
    except Exception as e:
        logger.error(f"Ошибка в API статистики: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/control/start', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def start_manager(current_user=None):
    """Запуск менеджера обработки файлов в основном сервисе.

    Отправляет команду основному сервису на запуск процесса обработки файлов.
    После успешного запуска перенаправляет пользователя на главную страницу.

    Args:
        current_user: Данные текущего пользователя из токена

    Returns:
        Редирект на главную страницу или JSON-ошибка при неудаче
    """
    try:
        logger.info(f"[{current_user['username']}] Запуск обработки")
        response = make_request('POST', '/api/control/start')
        if response.status_code == 200:
            return redirect(url_for('index'))
        else:
            return jsonify({'error': 'Failed to start manager'}), 500
    except Exception as e:
        logger.error(f"Ошибка при запуске менеджера: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/control/stop', methods=['POST'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def stop_manager(current_user=None):
    """Остановка менеджера обработки файлов в основном сервисе.

    Отправляет команду основному сервису на остановку процесса обработки файлов.
    После успешной остановки перенаправляет пользователя на главную страницу.

    Args:
        current_user: Данные текущего пользователя из токена

    Returns:
        Редирект на главную страницу или JSON-ошибка при неудаче
    """
    try:
        logger.info(f"[{current_user['username']}] Остановка обработки")
        response = make_request('POST', '/api/control/stop')
        if response.status_code == 200:
            return redirect(url_for('index'))
        else:
            return jsonify({'error': 'Failed to stop manager'}), 500
    except Exception as e:
        logger.error(f"Ошибка при остановке менеджера: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/file/<int:file_id>/retry', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def retry_file_processing(file_id, current_user=None):
    """Повторная попытка обработки файла с указанным ID.

    Отправляет команду основному сервису на повторную обработку файла,
    который ранее завершился с ошибкой. Файл помечается как ожидающий
    повторной обработки.

    Args:
        file_id (int): ID файла для повторной обработки
        current_user: Данные текущего пользователя из токена

    Returns:
        JSON-ответ с результатом операции
    """
    try:
        logger.info(f"[{current_user['username']}] Повторная обработка файла {file_id}")
        response = make_request('POST', f'/api/file/{file_id}/retry')
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'success': False, 'error': 'Failed to retry file processing'}), 500
    except Exception as e:
        logger.error(f"Ошибка при повторной обработке файла {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/file/<int:file_id>/delete', methods=['POST'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def delete_file(file_id, current_user=None):
    """Удаление файла из системы (помечает файл как удаленный).

    Отправляет команду основному сервису на удаление файла из базы данных.
    Файл помечается как удаленный, но физически может остаться в системе
    в зависимости от настроек основного сервиса.

    Args:
        file_id (int): ID файла для удаления
        current_user: Данные текущего пользователя из токена

    Returns:
        JSON-ответ с результатом операции
    """
    try:
        logger.info(f"[{current_user['username']}] Удаление файла {file_id}")
        response = make_request('DELETE', f'/api/file/{file_id}')
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'success': False, 'error': 'Failed to delete file'}), 500
    except Exception as e:
        logger.error(f"Ошибка при удалении файла {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/manual_scan', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def manual_scan(current_user=None):
    """Инициирует ручное сканирование директории файлов.

    Отправляет команду основному сервису на немедленное сканирование
    директории файлов для поиска новых или измененных файлов.
    Возвращает количество найденных новых файлов.

    Returns:
        JSON-ответ с результатом операции и количеством добавленных файлов
    """
    try:
        response = make_request('POST', '/api/manual_scan')
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'success': False, 'error': 'Failed to manual scan'}), 500
    except Exception as e:
        logger.error(f"Ошибка при ручном сканировании: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/upload_batch', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def upload_batch(current_user=None):
    """Проксирование пакетной загрузки файлов в Manager API."""
    try:
        files = request.files.getlist('files[]')
        if not files:
            return jsonify({'error': 'Файлы не переданы'}), 400

        relative_paths = request.form.get('relative_paths', '[]')
        subfolder = request.form.get('subfolder', '')

        logger.info(
            f"[{current_user['username']}] Пакетная загрузка: "
            f"{len(files)} файлов, подпапка: {subfolder}"
        )

        # Формируем multipart для проксирования
        proxy_files = []
        for f in files:
            proxy_files.append(('files[]', (f.filename, f.stream, f.content_type)))

        proxy_data = {
            'relative_paths': relative_paths,
            'subfolder': subfolder
        }

        url = get_api_url('/api/upload_batch')

        # Передаём авторизацию
        headers = {}
        token = request.cookies.get('access_token')
        if token:
            headers['Authorization'] = f'Bearer {token}'

        logger.info(f"Отправка в Manager: {url}, токен: {bool(token)}")
        response = requests.post(
            url,
            files=proxy_files,
            data=proxy_data,
            headers=headers,
            timeout=300  # 5 минут для больших пакетов
        )
        logger.info(f"Ответ Manager: {response.status_code} {response.text[:200]}")
        return response.json(), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Таймаут загрузки. Попробуйте меньше файлов.'}), 504
    except Exception as e:
        logger.error(f"Ошибка пакетной загрузки: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/import_folder', methods=['POST'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def import_folder(current_user=None):
    """Проксирование импорта из серверной папки."""
    try:
        data = request.get_json()
        if not data or not data.get('source_dir', '').strip():
            return jsonify({'error': 'Не указан путь к папке'}), 400

        logger.info(
            f"[{current_user['username']}] Импорт из папки: {data['source_dir']}"
        )
        response = make_request('POST', '/api/import_folder', use_cache=False, json=data)
        return response.json(), response.status_code
    except Exception as e:
        logger.error(f"Ошибка импорта из папки: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/subfolders')
def api_subfolders():
    """Проксирование списка подпапок."""
    try:
        response = make_request('GET', '/api/subfolders')
        if response.status_code == 200:
            return response.content, 200, {'Content-Type': 'application/json'}
        return jsonify({'error': 'Не удалось получить список подпапок'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/create_subfolder', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def create_subfolder(current_user=None):
    """Проксирование создания подпапки."""
    try:
        data = request.get_json()
        logger.info(f"[{current_user['username']}] Создание подпапки: {data.get('subfolder', '')}")
        response = make_request('POST', '/api/create_subfolder', use_cache=False, json=data)
        return response.json(), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/browse_server_folder')
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def browse_server_folder(current_user=None):
    """Проксирование просмотра серверных папок. Только admin."""
    try:
        folder_path = request.args.get('path', '/')
        response = make_request('GET', '/api/browse_server_folder', params={'path': folder_path})
        if response.status_code == 200:
            return response.content, 200, {'Content-Type': 'application/json'}
        return response.json(), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # Создаем директории, если их нет
    Path('templates').mkdir(exist_ok=True)
    Path('static').mkdir(exist_ok=True)

    # Загрузка настроек из config.json
    config_path = Path("config.json")
    if config_path.exists():
        import json
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        host = config.get("server", {}).get("host", "0.0.0.0")
        port = config.get("server", {}).get("port", 5000)
        debug = config.get("server", {}).get("debug", False)
    else:
        host = "0.0.0.0"
        port = 5000
        debug = False

    app.run(debug=debug, host=host, port=port)