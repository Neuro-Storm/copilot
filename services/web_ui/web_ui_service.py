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
- Базовую аутентификацию по протоколу HTTP Basic Authentication
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
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
import logging
from functools import wraps
import hashlib

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

# Загрузка переменных окружения из .env файла
from dotenv import load_dotenv
load_dotenv()

# Аутентификация
# SECURITY: Используется Basic Authentication для защиты веб-интерфейса
# Учетные данные должны быть установлены через переменные окружения
AUTH_REQUIRED = os.getenv("WEB_UI_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no")
AUTH_USERNAME = os.getenv("WEB_UI_USERNAME", "")
AUTH_PASSWORD = os.getenv("WEB_UI_PASSWORD", "")

# Адрес основного сервиса
MAIN_SERVICE_URL = os.getenv('MAIN_SERVICE_URL', 'http://localhost:5001')

# Добавление поддержки CORS (Cross-Origin Resource Sharing)
# Позволяет JavaScript-коду на веб-страницах делать запросы к этому сервису
# из других доменов, что необходимо для работы AJAX-запросов
CORS(app)

def _auth_required_response():
    """Возвращает HTTP 401 ответ с заголовком WWW-Authenticate для аутентификации по протоколу Basic Auth.

    Эта функция формирует стандартный HTTP 401 ответ, который указывает клиенту,
    что требуется аутентификация с использованием схемы Basic Authentication.

    Returns:
        Response: HTTP 401 ответ с заголовком аутентификации, указывающим на необходимость
                 предоставить учетные данные для доступа к защищенному ресурсу
    """
    return app.response_class(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="WebUI"'}
    )

def _is_auth_valid():
    """Проверяет действительность аутентификации пользователя по протоколу Basic Auth.

    Функция проверяет, включена ли аутентификация в конфигурации, затем извлекает
    учетные данные из заголовка Authorization и сравнивает их с сохраненными
    значениями имени пользователя и пароля из переменных окружения.

    Returns:
        bool: True если аутентификация включена и учетные данные действительны,
              иначе False
    """
    if not AUTH_REQUIRED:
        return True
    auth = request.authorization
    if not auth:
        return False
    # SECURITY: Сравнение учетных данных пользователя с сохраненными значениями
    # Используется простое сравнение строк, так как Basic Auth передает учетные данные в открытом виде
    return auth.username == AUTH_USERNAME and auth.password == AUTH_PASSWORD

@app.before_request
def enforce_auth():
    """Middleware для проверки аутентификации перед обработкой каждого запроса.

    Функция проверяет, включена ли аутентификация и установлены ли учетные данные.
    Если аутентификация включена, но учетные данные не установлены, возвращается
    ошибка 500. Если учетные данные предоставлены, но неверны, возвращается
    ответ с требованием аутентификации (HTTP 401).

    SECURITY: Защита от атак типа "brute force" не реализована в текущей версии.
    Рекомендуется использовать дополнительные меры безопасности в продакшене.
    """
    if AUTH_REQUIRED and (not AUTH_USERNAME or not AUTH_PASSWORD):
        # SECURITY: Проверка наличия учетных данных для аутентификации
        return app.response_class(
            "Auth is required. Set WEB_UI_USERNAME and WEB_UI_PASSWORD.",
            500
        )
    if not _is_auth_valid():
        # SECURITY: Возвращаем HTTP 401 при неудачной аутентификации
        return _auth_required_response()

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
                    'json': lambda: cached_response.get('json_data', {})
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
        stats = stats_response.json() if stats_response.status_code == 200 else {'total_files': 0, 'status_counts': {}, 'last_update': None}

        # Получаем последние файлы из основного сервиса
        files_response = make_request('GET', '/api/files?limit=10')
        files = files_response.json()['files'] if files_response.status_code == 200 else []

        # Получаем статус сервиса
        status_response = make_request('GET', '/api/status')
        is_running = status_response.json().get('running', False) if status_response.status_code == 200 else False

        # Получаем конфигурацию (без чувствительных данных)
        config_response = make_request('GET', '/api/config')
        cfg = config_response.json() if config_response.status_code == 200 else {}

        return render_template('index.html',
                               stats=stats,
                               files=files,
                               is_running=is_running,
                               cfg=cfg,
                               year=datetime.now().year)
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}")
        return f"Ошибка: {e}", 500

@app.route('/files')
def files_page():
    """Страница со списком всех файлов с возможностью фильтрации и пагинации.

    Отображает таблицу файлов с фильтрами по статусу, пагинацией и возможностью
    повторной обработки или удаления файлов. Поддерживает фильтрацию по статусу
    и настраиваемое количество элементов на странице.

    Query Parameters:
        page (int): Номер страницы для пагинации (по умолчанию 1)
        limit (int): Количество файлов на странице (по умолчанию 50)
        status (str): Фильтр по статусу файла (необязательный)

    Returns:
        HTML-страница files.html со списком файлов
    """
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        status_filter = request.args.get('status', '')

        params = {
            'page': page,
            'limit': limit,
            'status': status_filter
        }

        files_response = make_request('GET', '/api/files', params=params)
        response_data = files_response.json() if files_response.status_code == 200 else {'files': [], 'pagination': {}}

        files = response_data.get('files', [])
        pagination = response_data.get('pagination', {})
        total_pages = pagination.get('total_pages', 1)
        total_count = pagination.get('total_count', 0)

        # Получаем уникальные статусы для фильтра
        stats_response = make_request('GET', '/api/stats')
        statuses = []
        if stats_response.status_code == 200:
            stats_data = stats_response.json()
            statuses = list(stats_data.get('status_counts', {}).keys())

        return render_template('files.html',
                             files=files,
                             page=page,
                             total_pages=total_pages,
                             total_count=total_count,
                             statuses=statuses)
    except Exception as e:
        logger.error(f"Ошибка на странице файлов: {e}")
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
def start_manager():
    """Запуск менеджера обработки файлов в основном сервисе.

    Отправляет команду основному сервису на запуск процесса обработки файлов.
    После успешного запуска перенаправляет пользователя на главную страницу.

    Returns:
        Редирект на главную страницу или JSON-ошибка при неудаче
    """
    try:
        response = make_request('POST', '/api/control/start')
        if response.status_code == 200:
            return redirect(url_for('index'))
        else:
            return jsonify({'error': 'Failed to start manager'}), 500
    except Exception as e:
        logger.error(f"Ошибка при запуске менеджера: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/control/stop', methods=['POST'])
def stop_manager():
    """Остановка менеджера обработки файлов в основном сервисе.

    Отправляет команду основному сервису на остановку процесса обработки файлов.
    После успешной остановки перенаправляет пользователя на главную страницу.

    Returns:
        Редирект на главную страницу или JSON-ошибка при неудаче
    """
    try:
        response = make_request('POST', '/api/control/stop')
        if response.status_code == 200:
            return redirect(url_for('index'))
        else:
            return jsonify({'error': 'Failed to stop manager'}), 500
    except Exception as e:
        logger.error(f"Ошибка при остановке менеджера: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/file/<int:file_id>/retry', methods=['POST'])
def retry_file_processing(file_id):
    """Повторная попытка обработки файла с указанным ID.

    Отправляет команду основному сервису на повторную обработку файла,
    который ранее завершился с ошибкой. Файл помечается как ожидающий
    повторной обработки.

    Args:
        file_id (int): ID файла для повторной обработки

    Returns:
        JSON-ответ с результатом операции
    """
    try:
        response = make_request('POST', f'/api/file/{file_id}/retry')
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'success': False, 'error': 'Failed to retry file processing'}), 500
    except Exception as e:
        logger.error(f"Ошибка при повторной обработке файла {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/file/<int:file_id>/delete', methods=['POST'])
def delete_file(file_id):
    """Удаление файла из системы (помечает файл как удаленный).

    Отправляет команду основному сервису на удаление файла из базы данных.
    Файл помечается как удаленный, но физически может остаться в системе
    в зависимости от настроек основного сервиса.

    Args:
        file_id (int): ID файла для удаления

    Returns:
        JSON-ответ с результатом операции
    """
    try:
        response = make_request('DELETE', f'/api/file/{file_id}')
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'success': False, 'error': 'Failed to delete file'}), 500
    except Exception as e:
        logger.error(f"Ошибка при удалении файла {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/manual_scan', methods=['POST'])
def manual_scan():
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