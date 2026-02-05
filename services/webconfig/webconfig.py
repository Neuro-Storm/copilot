import os
import json
import logging
import subprocess
import signal
import psutil
import re
import sys
import urllib.parse
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
from dotenv import load_dotenv, dotenv_values
import html
from pathlib import Path
import threading
import time
import hmac

# Import for CORS
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)

# Define services directory
SERVICES_DIR = Path("../").resolve()
BASE_SERVICES_DIR = SERVICES_DIR.resolve()
CONFIG_FILE_NAME = "config.json"

SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

AUTH_REQUIRED = os.getenv("WEB_CONFIG_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no")
AUTH_USERNAME = os.getenv("WEB_CONFIG_USERNAME", "")
AUTH_PASSWORD = os.getenv("WEB_CONFIG_PASSWORD", "")
DEBUG_MODE = os.getenv("WEB_CONFIG_DEBUG", "0").lower() in ("1", "true", "yes")
ALLOWED_ORIGINS = os.getenv("WEB_CONFIG_CORS_ORIGINS", "")

# Global variables for process management
SERVICE_PROCESSES = {}
SERVICE_PROCESSES_LOCK = threading.RLock()

# Global variables for gRPC connection management
GRPC_CONNECTIONS = {}
GRPC_CONNECTIONS_LOCK = threading.RLock()

from datetime import datetime

# Add CORS support only when explicit origin list is provided
if ALLOWED_ORIGINS:
    CORS(app, origins=[origin.strip() for origin in ALLOWED_ORIGINS.split(",") if origin.strip()],
         supports_credentials=True)


# Add Jinja2 filter for timestamp to datetime conversion
@app.template_filter('timestamp_to_datetime')
def timestamp_to_datetime(timestamp):
    """Convert timestamp to readable datetime format"""
    try:
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return 'Invalid timestamp'


class GrpcConnectionManager:
    """Класс для управления gRPC соединениями между микросервисами"""

    def __init__(self):
        self.connections = {}
        self.lock = threading.RLock()

    def register_connection(self, service_name, target_service, channel):
        """Регистрирует новое gRPC соединение"""
        with self.lock:
            connection_key = f"{service_name}_to_{target_service}"
            self.connections[connection_key] = {
                'service_name': service_name,
                'target_service': target_service,
                'channel': channel,
                'connected_at': time.time(),
                'status': 'active'
            }

    def unregister_connection(self, service_name, target_service):
        """Удаляет gRPC соединение из реестра"""
        with self.lock:
            connection_key = f"{service_name}_to_{target_service}"
            if connection_key in self.connections:
                del self.connections[connection_key]

    def get_connections(self):
        """Возвращает все зарегистрированные соединения"""
        with self.lock:
            return self.connections.copy()

    def get_service_connections(self, service_name):
        """Возвращает соединения для конкретного сервиса"""
        with self.lock:
            result = {}
            for key, conn in self.connections.items():
                if conn['service_name'] == service_name or conn['target_service'] == service_name:
                    result[key] = conn
            return result

    def check_connection_health(self, service_name, target_service):
        """Проверяет здоровье соединения"""
        with self.lock:
            connection_key = f"{service_name}_to_{target_service}"
            if connection_key in self.connections:
                channel = self.connections[connection_key]['channel']
                try:
                    # Проверяем состояние канала
                    import grpc
                    state = channel.get_state(try_to_connect=False)
                    state_name = state.name.lower()

                    # Возвращаем состояние в понятном формате
                    if state_name == 'idle':
                        return 'idle'
                    elif state_name == 'connecting':
                        return 'connecting'
                    elif state_name == 'ready':
                        return 'active'
                    elif state_name == 'transient_failure':
                        return 'transient_failure'
                    elif state_name == 'shutdown':
                        return 'shutdown'
                    else:
                        return 'unknown'
                except:
                    return 'unknown'
            return 'not_found'

    def close_connection(self, service_name, target_service):
        """Закрывает gRPC соединение"""
        with self.lock:
            connection_key = f"{service_name}_to_{target_service}"
            if connection_key in self.connections:
                try:
                    channel = self.connections[connection_key]['channel']
                    channel.close()
                    self.connections[connection_key]['status'] = 'closed'
                    return True
                except Exception as e:
                    logging.error(f"Ошибка при закрытии соединения {connection_key}: {e}")
                    return False
            return False


# Initialize the gRPC connection manager
grpc_conn_manager = GrpcConnectionManager()


class ProtoFileManager:
    """Класс для управления proto файлами"""

    def __init__(self, services_dir):
        self.services_dir = services_dir
        self.proto_extensions = ['.proto']

    def find_all_proto_files(self):
        """Находит все proto файлы в директориях сервисов"""
        proto_files = {}

        for service_dir in self.services_dir.iterdir():
            if service_dir.is_dir() and not service_dir.is_symlink():
                # Ищем proto файлы в директории сервиса
                service_protos = []
                for proto_file in service_dir.rglob('*.proto'):
                    service_protos.append({
                        'name': proto_file.name,
                        'path': str(proto_file.relative_to(self.services_dir)),
                        'size': proto_file.stat().st_size,
                        'modified': proto_file.stat().st_mtime
                    })

                if service_protos:
                    proto_files[service_dir.name] = service_protos

        return proto_files

    def read_proto_file(self, service_name, proto_filename):
        """Читает содержимое proto файла"""
        try:
            service_dir = PathValidator.resolve_service_dir(self.services_dir, service_name)
            proto_path = service_dir / proto_filename

            # Дополнительная проверка, что файл имеет расширение .proto
            if proto_path.suffix.lower() != '.proto':
                raise ValueError("Файл должен иметь расширение .proto")

            # Проверяем, что путь находится внутри директории сервиса
            proto_path.relative_to(service_dir)

            with open(proto_path, 'r', encoding='utf-8') as f:
                content = f.read()

            return content
        except Exception as e:
            raise e

    def write_proto_file(self, service_name, proto_filename, content):
        """Записывает содержимое в proto файл"""
        try:
            service_dir = PathValidator.resolve_service_dir(self.services_dir, service_name)
            proto_path = service_dir / proto_filename

            # Проверяем, что файл имеет расширение .proto
            if proto_path.suffix.lower() != '.proto':
                raise ValueError("Файл должен иметь расширение .proto")

            # Проверяем, что путь находится внутри директории сервиса
            proto_path.relative_to(service_dir)

            # Создаем резервную копию, если файл существует
            if proto_path.exists():
                backup_path = proto_path.with_suffix(proto_path.suffix + '.backup')
                import shutil
                shutil.copy2(proto_path, backup_path)

            with open(proto_path, 'w', encoding='utf-8') as f:
                f.write(content)

            return True
        except Exception as e:
            raise e


# Initialize the proto file manager
proto_manager = ProtoFileManager(SERVICES_DIR)


def detect_grpc_connections():
    """
    Обнаруживает gRPC соединения между сервисами на основе их конфигураций
    """
    configs = get_all_configs()
    detected_connections = {}

    # Определение типичных gRPC портов для сервисов
    for service_name, config_info in configs.items():
        config_data = config_info.get('data', {})

        # Определение порта сервиса на основе его типа
        if service_name == 'searcher':
            embedder_host = config_data.get('embedder_host', 'localhost')
            embedder_port = config_data.get('embedder_port', 50051)
            detected_connections[f"{service_name}_to_embedder"] = {
                'service_name': service_name,
                'target_service': 'embedder',
                'target_address': f"{embedder_host}:{embedder_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }
        elif service_name == 'indexer':
            chunker_host = config_data.get('CHUNKER_HOST', 'localhost')
            chunker_port = config_data.get('CHUNKER_PORT', 50052)
            embedder_host = config_data.get('EMBEDDER_HOST', 'localhost')
            embedder_port = config_data.get('EMBEDDER_PORT', 50051)

            detected_connections[f"{service_name}_to_chunker"] = {
                'service_name': service_name,
                'target_service': 'chunker',
                'target_address': f"{chunker_host}:{chunker_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }

            detected_connections[f"{service_name}_to_embedder"] = {
                'service_name': service_name,
                'target_service': 'embedder',
                'target_address': f"{embedder_host}:{embedder_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }
        elif service_name == 'websearch':
            searcher_host = config_data.get('SEARCHER_HOST', 'localhost')
            searcher_port = config_data.get('SEARCHER_PORT', 50055)
            detected_connections[f"{service_name}_to_searcher"] = {
                'service_name': service_name,
                'target_service': 'searcher',
                'target_address': f"{searcher_host}:{searcher_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }

            # Проверяем, включена ли генерация
            if config_data.get('ENABLE_GENERATION', False):
                generator_host = config_data.get('GENERATOR_HOST', 'localhost')
                generator_port = config_data.get('GENERATOR_PORT', 50056)
                detected_connections[f"{service_name}_to_generator"] = {
                    'service_name': service_name,
                    'target_service': 'generator',
                    'target_address': f"{generator_host}:{generator_port}",
                    'detected_at': time.time(),
                    'status': 'configured'
                }
        elif service_name == 'manager':
            # Manager подключается к converter, chunker, embedder, indexer
            converter_host = config_data.get('converter_address', 'localhost:50053').split(':')[0]
            converter_port = config_data.get('converter_address', 'localhost:50053').split(':')[1]
            indexer_host = config_data.get('indexer_address', 'localhost:50054').split(':')[0]
            indexer_port = config_data.get('indexer_address', 'localhost:50054').split(':')[1]

            detected_connections[f"{service_name}_to_converter"] = {
                'service_name': service_name,
                'target_service': 'converter',
                'target_address': f"{converter_host}:{converter_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }

            detected_connections[f"{service_name}_to_indexer"] = {
                'service_name': service_name,
                'target_service': 'indexer',
                'target_address': f"{indexer_host}:{indexer_port}",
                'detected_at': time.time(),
                'status': 'configured'
            }
        elif service_name == 'converter':
            # Converter может иметь зависимости, указанные в конфигурации
            # В текущей реализации converter обычно не подключается к другим gRPC сервисам напрямую
            pass
        elif service_name == 'chunker':
            # Chunker может иметь зависимости, но обычно работает самостоятельно
            pass
        elif service_name == 'embedder':
            # Embedder может иметь зависимости, но обычно работает самостоятельно
            pass
        elif service_name == 'generator':
            # Generator может подключаться к другим сервисам при необходимости
            pass
        elif service_name == 'web_ui':
            # Web UI подключается к Manager для получения статуса
            main_service_url = config_data.get('MAIN_SERVICE_URL', 'http://localhost:5001')
            # Web UI использует HTTP API, а не gRPC, но для полноты картины можно отметить
            pass

    # Также определяем связи на основе типичной архитектуры RAG-системы
    # Проверяем, какие сервисы могут быть связаны
    service_names = list(configs.keys())

    # Manager обычно взаимодействует с converter, chunker, embedder, indexer
    if 'manager' in service_names:
        for target_service in ['converter', 'chunker', 'embedder', 'indexer']:
            if target_service in service_names:
                # Эти соединения устанавливаются во время обработки файлов
                connection_key = f"manager_to_{target_service}"
                if connection_key not in detected_connections:
                    # Устанавливаем правильные порты для каждого сервиса
                    port_map = {
                        'converter': '50053',
                        'chunker': '50052',
                        'embedder': '50051',
                        'indexer': '50054'
                    }
                    detected_connections[connection_key] = {
                        'service_name': 'manager',
                        'target_service': target_service,
                        'target_address': f"localhost:{port_map[target_service]}",
                        'detected_at': time.time(),
                        'status': 'potential'
                    }

    # Indexer взаимодействует с chunker и embedder
    if 'indexer' in service_names and 'chunker' in service_names:
        connection_key = "indexer_to_chunker"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'indexer',
                'target_service': 'chunker',
                'target_address': 'localhost:50052',
                'detected_at': time.time(),
                'status': 'potential'
            }

    if 'indexer' in service_names and 'embedder' in service_names:
        connection_key = "indexer_to_embedder"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'indexer',
                'target_service': 'embedder',
                'target_address': 'localhost:50051',
                'detected_at': time.time(),
                'status': 'potential'
            }

    # Searcher взаимодействует с embedder
    if 'searcher' in service_names and 'embedder' in service_names:
        connection_key = "searcher_to_embedder"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'searcher',
                'target_service': 'embedder',
                'target_address': 'localhost:50051',
                'detected_at': time.time(),
                'status': 'potential'
            }

    # Websearch взаимодействует с searcher и generator
    if 'websearch' in service_names and 'searcher' in service_names:
        connection_key = "websearch_to_searcher"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'websearch',
                'target_service': 'searcher',
                'target_address': 'localhost:50055',
                'detected_at': time.time(),
                'status': 'potential'
            }

    if 'websearch' in service_names and 'generator' in service_names:
        connection_key = "websearch_to_generator"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'websearch',
                'target_service': 'generator',
                'target_address': 'localhost:50056',
                'detected_at': time.time(),
                'status': 'potential'
            }

    # Web UI взаимодействует с Manager через HTTP, но для полноты картины добавим
    if 'web_ui' in service_names and 'manager' in service_names:
        main_service_url = configs['web_ui']['data'].get('MAIN_SERVICE_URL', 'http://localhost:5001')
        # Извлекаем хост и порт из URL
        parsed_url = urllib.parse.urlparse(main_service_url)
        host = parsed_url.hostname or 'localhost'
        port = parsed_url.port or 5001

        connection_key = "web_ui_to_manager"
        if connection_key not in detected_connections:
            detected_connections[connection_key] = {
                'service_name': 'web_ui',
                'target_service': 'manager',
                'target_address': f"{host}:{port}",
                'detected_at': time.time(),
                'status': 'potential_http'
            }

    # Добавляем возможные соединения для других сервисов
    # Generator может взаимодействовать с другими сервисами
    if 'generator' in service_names:
        # Generator может подключаться к другим сервисам при необходимости
        pass

    # Converter может взаимодействовать с другими сервисами
    if 'converter' in service_names:
        # Converter обычно работает независимо, но может быть частью цепочки
        pass

    # Chunker может взаимодействовать с другими сервисами
    if 'chunker' in service_names:
        # Chunker обычно работает независимо, но может быть частью цепочки
        pass

    # Embedder может взаимодействовать с другими сервисами
    if 'embedder' in service_names:
        # Embedder обычно работает независимо, но может быть частью цепочки
        pass

    return detected_connections

class PathValidator:
    """Утилитарный класс для проверки безопасности путей"""

    @staticmethod
    def validate_service_name(service_name):
        """Проверяет имя сервиса на безопасность"""
        if '..' in service_name or service_name.startswith('/') or '../' in service_name:
            raise ValueError("Неверное имя сервиса")

        if not SERVICE_NAME_RE.match(service_name):
            raise ValueError("Неверное имя сервиса")

    @staticmethod
    def resolve_service_dir(base_dir, service_name):
        """Разрешает директорию сервиса, проверяя безопасность пути."""
        PathValidator.validate_service_name(service_name)
        service_dir = (base_dir / service_name).resolve()

        try:
            service_dir.relative_to(base_dir)
        except ValueError:
            raise ValueError("Директория сервиса находится вне базового пути")

        return service_dir


def _auth_required_response():
    """Возвращает ответ, требующий аутентификации."""
    return Response("Требуется аутентификация", 401, {"WWW-Authenticate": 'Basic realm="WebConfig"'})


def _is_auth_valid():
    """Проверяет действительность аутентификации пользователя."""
    if not AUTH_REQUIRED:
        return True
    auth = request.authorization
    if not auth:
        return False
    # Используем hmac.compare_digest для предотвращения атак по времени
    username_valid = hmac.compare_digest(auth.username, AUTH_USERNAME)
    password_valid = hmac.compare_digest(auth.password, AUTH_PASSWORD)

    return username_valid and password_valid


@app.before_request
def enforce_auth():
    if AUTH_REQUIRED and (not AUTH_USERNAME or not AUTH_PASSWORD):
        return Response("Auth is required. Set WEB_CONFIG_USERNAME and WEB_CONFIG_PASSWORD.", 500)
    if not _is_auth_valid():
        return _auth_required_response()


def load_service_env(service_dir, base_env):
    """Загружает переменные окружения из .env файла сервиса."""
    env = base_env.copy()
    dotenv_path = service_dir / ".env"
    if dotenv_path.exists():
        values = dotenv_values(dotenv_path)
        for key, value in values.items():
            if value is not None:
                env[key] = value
    return env


def find_service_process(service_name):
    """Находит процесс для заданного сервиса, проверяя его Python файл"""
    try:
        service_dir = PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError:
        return None

    # Находим главный Python файл в директории сервиса
    main_file_paths = _find_main_files(service_dir, service_name)
    if not main_file_paths:
        return None

    # Находим запущенные процессы, соответствующие этому сервису
    return _search_matching_process(service_name, service_dir, main_file_paths)


def _find_main_files(service_dir, service_name):
    """Находит возможные главные файлы для сервиса"""
    possible_main_files = [
        f"{service_name}.py",
        "main.py",
        "app.py",
        "server.py"
    ]

    # Специальный случай для web_ui, который использует web_ui_service.py
    if service_name == "web_ui":
        possible_main_files.insert(1, "web_ui_service.py")

    main_file_paths = []
    for main_file in possible_main_files:
        main_file_path = service_dir / main_file
        if main_file_path.exists():
            main_file_paths.append(main_file_path.name)

    return main_file_paths


def _search_matching_process(service_name, service_dir, main_file_paths):
    """Поиск процесса, соответствующего критериям сервиса"""
    service_dir_lower = str(service_dir).lower()
    service_name_lower = service_name.lower()

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
        try:
            # Проверяем командную строку
            if proc.info['cmdline']:
                cmdline = ' '.join(proc.info['cmdline']).lower()

                # Если имя сервиса и путь к директории находятся в командной строке
                if service_name_lower in cmdline and service_dir_lower in cmdline:
                    # Проверяем, есть ли какой-либо главный файл в командной строке
                    for main_file in main_file_paths:
                        if main_file.lower() in cmdline:
                            return proc

            # Проверяем рабочую директорию
            proc_cwd = proc.info.get('cwd')
            if proc_cwd and service_dir_lower in proc_cwd.lower():
                return proc

            # Дополнительная проверка: для Flask-приложений (включая web_ui) может потребоваться
            # более гибкий подход, так как процесс может быть запущен через разные механизмы
            if service_name == 'web_ui':
                # Проверяем, содержит ли имя процесса или командная строка ключевые слова
                proc_name = proc.info.get('name', '').lower()
                if 'python' in proc_name or 'flask' in proc_name:
                    # Проверяем, возможно, процесс запущен из директории сервиса
                    if service_dir_lower in cmdline or service_name_lower in cmdline:
                        return proc

        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue

    return None


def _ensure_service_exists(service_name):
    """Проверяет существование сервиса и возвращает результат проверки"""
    try:
        service_dir = PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return False, str(e)

    configs = get_all_configs()
    if service_name not in configs and not service_dir.exists():
        return False, f'Сервис {service_name} не найден'

    return True, None


def _find_main_file(service_dir, service_name):
    """Находит главный файл для сервиса"""
    possible_main_files = [
        f"{service_name}.py",
        "main.py",
        "app.py",
        "server.py"
    ]

    # Специальный случай для web_ui, который использует web_ui_service.py
    if service_name == "web_ui":
        possible_main_files.insert(1, "web_ui_service.py")

    for main_file in possible_main_files:
        main_file_path = service_dir / main_file
        # Дополнительная проверка, что файл действительно находится в директории сервиса
        try:
            main_file_path.relative_to(service_dir)
        except ValueError:
            continue

        if main_file_path.exists():
            return main_file_path

    return None


def _setup_python_path(env, service_dir):
    """Настройка PYTHONPATH для подпроцесса"""
    python_path = env.get('PYTHONPATH', '')
    if python_path:
        env['PYTHONPATH'] = f"{str(service_dir)}{os.pathsep}{python_path}"
    else:
        env['PYTHONPATH'] = str(service_dir)
    return env


def _execute_service(service_name, main_file_path, service_dir):
    """Выполняет процесс сервиса"""
    cmd = [sys.executable, str(main_file_path)]

    # Настройка переменных окружения для подпроцесса
    env = os.environ.copy()
    env = load_service_env(service_dir, env)
    env = _setup_python_path(env, service_dir)

    # Открытие файла лога
    log_path = service_dir / f"{service_name}.webconfig.log"
    try:
        log_file = open(log_path, "a", encoding="utf-8")
    except OSError:
        log_file = open(os.devnull, "w")

    try:
        # Используем CREATE_NO_WINDOW вместо CREATE_NEW_CONSOLE, чтобы избежать открытия терминала
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        process = subprocess.Popen(
            cmd,
            cwd=str(service_dir),
            env=env,
            stdout=log_file,
            stderr=log_file,
            shell=False,  # ВАЖНО: Не использовать shell=True
            creationflags=creationflags
        )
    finally:
        try:
            log_file.close()
        except Exception:
            pass

    # Краткая пауза для проверки немедленного выхода
    time.sleep(0.5)
    if process.poll() is not None:
        return False, f"Сервис {service_name} завершился сразу после запуска. Проверьте лог: {log_path}"

    # Сохранение ссылки на процесс
    with SERVICE_PROCESSES_LOCK:
        SERVICE_PROCESSES[service_name] = process

    return True, f"Сервис {service_name} успешно запущен с PID {process.pid}"


def start_service(service_name):
    """Запускает сервис, выполняя его главный Python файл"""
    # Проверка безопасности имени сервиса
    if not SERVICE_NAME_RE.match(service_name) or '..' in service_name:
        return False, f"Неверное имя сервиса: {service_name}"

    # Проверка, что имя сервиса не содержит потенциально опасных символов
    if not re.match(r'^[A-Za-z0-9_-]+$', service_name):
        return False, f"Неверное имя сервиса: {service_name}"

    try:
        service_dir = PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return False, str(e)

    # Проверка, запущен ли уже сервис
    existing_proc = find_service_process(service_name)
    if existing_proc:
        return False, f"Сервис {service_name} уже запущен с PID {existing_proc.pid}"

    # Поиск главного Python файла в директории сервиса
    main_file_path = _find_main_file(service_dir, service_name)
    if not main_file_path:
        return False, f"Главный Python файл для сервиса {service_name} не найден"

    try:
        return _execute_service(service_name, main_file_path, service_dir)
    except Exception as e:
        return False, f"Не удалось запустить сервис {service_name}: {str(e)}"


def stop_service(service_name):
    """Останавливает сервис, завершая его процесс"""
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return False, str(e)

    # Полная синхронизация доступа к процессам
    with SERVICE_PROCESSES_LOCK:
        process = SERVICE_PROCESSES.get(service_name)

        if not process:
            proc = find_service_process(service_name)
            if proc:
                SERVICE_PROCESSES[service_name] = proc
                process = proc

        if process:
            try:
                # Попытка правильно завершить процесс
                return _terminate_process(process, service_name)
            except Exception as e:
                return False, f"Не удалось остановить сервис {service_name}: {str(e)}"
        else:
            # Попытка найти и остановить процесс напрямую
            proc = find_service_process(service_name)
            if proc:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except psutil.TimeoutExpired:
                        proc.kill()
                    return True, f"Сервис {service_name} успешно остановлен"
                except Exception as e:
                    return False, f"Не удалось остановить сервис {service_name}: {str(e)}"
            else:
                return False, f"Сервис {service_name} не запущен или не найден"


def _terminate_process(process, service_name):
    """Завершает процесс и обрабатывает результат"""
    process.terminate()
    try:
        # Ожидание завершения процесса (с таймаутом)
        if hasattr(process, 'communicate'):
            _, stderr = process.communicate(timeout=10)
            if stderr:
                logging.warning(f"Сервис {service_name} завершен с выводом ошибки: {stderr.decode()}")
    except subprocess.TimeoutExpired:
        # Принудительное уничтожение, если процесс не завершается должным образом
        process.kill()
        if hasattr(process, 'communicate'):
            _, stderr = process.communicate()  # Очистка оставшегося вывода
            if stderr:
                logging.warning(f"Сервис {service_name} уничтожен с выводом ошибки: {stderr.decode()}")

    # Удаление из сохраненных процессов
    with SERVICE_PROCESSES_LOCK:
        if SERVICE_PROCESSES.get(service_name) is process:
            del SERVICE_PROCESSES[service_name]

    return True, f"Сервис {service_name} успешно остановлен"


def restart_service(service_name):
    """Перезапускает сервис, останавливая и запуская его снова"""
    # Сначала останавливаем сервис
    success, message = stop_service(service_name)
    if not success:
        # Если сервис не был запущен, это нормально для перезапуска
        if "not running" not in message.lower():
            logging.warning(f"Ошибка остановки сервиса {service_name} для перезапуска: {message}")
    else:
        # Если остановка прошла успешно, ждем немного перед запуском
        time.sleep(1)

    # Небольшая задержка, чтобы процесс полностью остановился
    time.sleep(2)

    # Запускаем сервис
    return start_service(service_name)


def cleanup_terminated_processes():
    """Непрерывная очистка словаря SERVICE_PROCESSES, удаление завершенных процессов"""
    global SERVICE_PROCESSES
    while True:
        try:
            # Создание копии ключей, чтобы избежать изменения словаря во время итерации
            with SERVICE_PROCESSES_LOCK:
                service_names = list(SERVICE_PROCESSES.keys())

            for service_name in service_names:
                with SERVICE_PROCESSES_LOCK:
                    process = SERVICE_PROCESSES.get(service_name)
                if not process:
                    continue
                if hasattr(process, 'poll'):
                    # Для объектов subprocess.Popen
                    if process.poll() is not None:  # Процесс завершен
                        with SERVICE_PROCESSES_LOCK:
                            if SERVICE_PROCESSES.get(service_name) is process:
                                del SERVICE_PROCESSES[service_name]
                elif hasattr(process, 'is_running'):
                    # Для объектов psutil.Process
                    try:
                        if not process.is_running():
                            with SERVICE_PROCESSES_LOCK:
                                if SERVICE_PROCESSES.get(service_name) is process:
                                    del SERVICE_PROCESSES[service_name]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        with SERVICE_PROCESSES_LOCK:
                            if SERVICE_PROCESSES.get(service_name) is process:
                                del SERVICE_PROCESSES[service_name]

        except Exception as e:
            logging.error(f"Ошибка во время очистки процессов: {str(e)}")

        # Пауза в 30 секунд перед следующей очисткой
        time.sleep(30)


def get_all_configs():
    """Получает конфигурации из всех микросервисов, у которых есть файлы config.json"""
    configs = {}

    # Итерация по всем директориям в директории сервисов
    for service_dir in SERVICES_DIR.iterdir():
        if service_dir.is_dir() and not service_dir.is_symlink():
            config_path = service_dir / CONFIG_FILE_NAME

            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                        configs[service_dir.name] = {
                            'path': str(config_path),
                            'data': config_data
                        }
                except json.JSONDecodeError as e:
                    logging.error(f"Ошибка парсинга JSON в конфигурации для {service_dir.name}: {str(e)}")
                except PermissionError:
                    logging.error(f"Нет доступа к файлу конфигурации для {service_dir.name}")
                except OSError as e:
                    logging.error(f"Ошибка чтения файла конфигурации для {service_dir.name}: {str(e)}")
                except Exception as e:
                    logging.error(f"Неожиданная ошибка чтения конфигурации для {service_dir.name}: {str(e)}")

    return configs


def update_config(service_name, new_config_data):
    """Обновляет конфигурацию для указанного сервиса"""
    try:
        service_dir = PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return False, str(e)
    config_path = service_dir / CONFIG_FILE_NAME

    if not config_path.exists():
        return False, f"Файл конфигурации для {service_name} не существует"

    # Проверка формата новых данных конфигурации
    if not isinstance(new_config_data, dict):
        return False, f"Неверный формат данных конфигурации для {service_name}. Ожидается словарь."

    backup_path = config_path.with_suffix(config_path.suffix + '.backup')
    temp_path = config_path.with_suffix(config_path.suffix + '.tmp')

    # Создание резервной копии текущей конфигурации
    try:
        if config_path.exists():
            import shutil
            shutil.copy2(config_path, backup_path)
    except Exception as e:
        logging.warning(f"Не удалось создать резервную копию конфигурации для {service_name}: {str(e)}")

    try:
        # Проверка, что новая конфигурация является допустимым JSON путем сериализации
        json.dumps(new_config_data, indent=2, ensure_ascii=False)

        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(new_config_data, f, indent=2, ensure_ascii=False)

        os.replace(temp_path, config_path)

        logging.info(f"Конфигурация успешно обновлена для {service_name}")
        return True, "Конфигурация успешно обновлена"
    except TypeError as e:
        # Восстановление из резервной копии, если проверка достоверности не пройдена
        if backup_path.exists():
            import shutil
            shutil.move(backup_path, config_path)
        logging.error(f"Неверный тип данных в конфигурации для {service_name}: {str(e)}")
        return False, f"Неверный тип данных в конфигурации: {str(e)}"
    except Exception as e:
        # Восстановление из резервной копии, если обновление не удалось
        if backup_path.exists():
            import shutil
            shutil.move(backup_path, config_path)
        logging.error(f"Ошибка обновления конфигурации для {service_name}: {str(e)}")
        return False, f"Ошибка обновления конфигурации: {str(e)}"
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def get_log_file_path(service_name):
    """Получает путь к файлу лога для сервиса"""
    try:
        service_dir = PathValidator.resolve_service_dir(SERVICES_DIR, service_name)
    except ValueError:
        return None

    # Определение возможных имен файлов лога для сервиса ТОЛЬКО в директории сервиса
    log_filenames = [
        f"{service_name}.log",
        f"{service_name}.webconfig.log",
        "service.log",
        "app.log",
        "main.log",
        "server.log"
    ]

    # Попытка найти файл лога ТОЛЬКО в директории сервиса
    for log_filename in log_filenames:
        log_path = service_dir / log_filename
        if log_path.exists():
            # Дополнительная проверка, что файл находится в директории сервиса
            try:
                log_path.relative_to(service_dir)
                return log_path
            except ValueError:
                continue  # Файл вне директории сервиса

    return None


def get_log_content(service_name, lines=100):
    """Получает содержимое лога сервиса"""
    log_path = get_log_file_path(service_name)

    if not log_path or not log_path.exists():
        return f"Файл лога для сервиса {service_name} не найден"

    try:
        # Чтение последних N строк из файла лога
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()

        # Возврат последних строк
        last_lines = all_lines[-lines:] if len(all_lines) >= lines else all_lines

        # Объединение строк в одну строку
        content = ''.join(last_lines)

        return content
    except Exception as e:
        return f"Ошибка чтения лога для сервиса {service_name}: {str(e)}"


def get_all_service_logs(lines_per_service=50):
    """Get logs of all services"""
    configs = get_all_configs()
    all_logs = {}

    for service_name in configs.keys():
        log_content = get_log_content(service_name, lines_per_service)
        all_logs[service_name] = log_content

    return all_logs


def render_config_field(key, value, full_key, depth=0):
    """
    Преобразует поле конфигурации в HTML для формы
    """
    indent = depth * 20  # Отступ для вложенных полей

    if isinstance(value, dict):
        return _render_dict_field(key, value, full_key, depth, indent)
    elif isinstance(value, list):
        return _render_list_field(key, value, full_key, indent)
    else:
        return _render_primitive_field(key, value, full_key, indent)


def _render_dict_field(key, value, full_key, depth, indent):
    """Отображает поле словаря"""
    html_output = f'<div class="config-section" style="margin-left: {indent}px;">'
    html_output += f'<div class="section-title">{key.replace("_", " ").upper()}</div>'

    for sub_key, sub_value in value.items():
        new_full_key = f"{full_key}.{sub_key}"
        html_output += render_config_field(sub_key, sub_value, new_full_key, depth + 1)

    html_output += '</div>'
    return html_output


def _render_list_field(key, value, full_key, indent):
    """Отображает поле списка"""
    label = _format_label(key)
    json_value = json.dumps(value, indent=2, ensure_ascii=False)

    return (
        f'<div class="form-group" style="margin-left: {indent}px;">'
        f'<label for="{html.escape(full_key)}">{label}:</label>'
        f'<textarea name="{html.escape(full_key)}" id="{html.escape(full_key)}" rows="6">'
        f'{html.escape(json_value)}</textarea>'
        f'<div class="field-description">Массив JSON</div>'
        f'</div>'
    )


def _render_primitive_field(key, value, full_key, indent):
    """Отображает примитивное поле (строка, число, логическое значение)"""
    label = _format_label(key)

    html_output = f'<div class="form-group" style="margin-left: {indent}px;">'
    html_output += f'<label for="{html.escape(full_key)}">{label}:</label>'

    # Генерация поля ввода на основе типа значения
    html_output += _generate_input_field(value, full_key)
    html_output += f'<div class="field-description">Текущее значение: {html.escape(str(value))}</div>'
    html_output += '</div>'

    return html_output


def _format_label(key):
    """Форматирует метку для отображения"""
    label_words = key.replace('_', ' ').split()
    return ' '.join(word.capitalize() for word in label_words)


def _generate_input_field(value, full_key):
    """Генерирует подходящее поле ввода на основе типа значения"""
    if isinstance(value, bool):
        selected_true = 'selected' if value else ''
        selected_false = '' if value else 'selected'
        return (
            f'<select name="{html.escape(full_key)}" id="{html.escape(full_key)}">'
            f'<option value="true" {selected_true}>True</option>'
            f'<option value="false" {selected_false}>False</option>'
            f'</select>'
        )
    elif isinstance(value, (int, float)):
        step_attr = 'step="any"' if isinstance(value, float) else ''
        return f'<input type="number" {step_attr} name="{html.escape(full_key)}" id="{html.escape(full_key)}" value="{value}" />'
    else:
        return f'<input type="text" name="{html.escape(full_key)}" id="{html.escape(full_key)}" value="{html.escape(str(value))}" />'


# Register custom function as template filter
@app.template_global()
def render_config_field_template(key, value, full_key, depth):
    return render_config_field(key, value, full_key, depth)


def flatten_dict(d, parent_key='', sep='.'):
    """Преобразует вложенный словарь в плоскую структуру для обработки форм"""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def unflatten_dict(d, sep='.'):
    """Преобразует плоский словарь обратно во вложенную структуру"""
    result = {}
    for key, value in d.items():
        keys = key.split(sep)
        current = result
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]
        # Преобразование строковых значений обратно в соответствующие типы
        converted_value = convert_string_to_type(value)
        current[keys[-1]] = converted_value
    return result


def convert_string_to_type(value):
    """Преобразует строковое значение в соответствующий тип (bool, int, float или str)"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()

        # Обработка логических значений
        if lowered == 'true':
            return True
        elif lowered == 'false':
            return False
        elif lowered in ('null', 'none'):
            return None

        # Попытка разбора JSON для списков/объектов
        if stripped.startswith('[') or stripped.startswith('{'):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass

    # Обработка числовых значений
    try:
        # Проверка, является ли число целым
        if '.' not in str(value) and str(value).lstrip('-').isdigit():
            return int(value)
        # Проверка, является ли число вещественным
        elif str(value).replace('.', '').replace('-', '').isdigit():
            return float(value)
    except (ValueError, AttributeError):
        pass

    # Возврат в виде строки, если невозможно преобразовать
    return value


def coerce_to_schema(value, schema):
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return value
        result = {}
        for key, schema_value in schema.items():
            if key in value:
                result[key] = coerce_to_schema(value[key], schema_value)
            else:
                result[key] = schema_value
        for key, val in value.items():
            if key not in result:
                result[key] = val
        return result
    if isinstance(schema, list):
        if not schema:
            return value
        return [coerce_to_schema(item, schema[0]) for item in value]
    if isinstance(schema, bool):
        return bool(value)
    if isinstance(schema, int) and not isinstance(schema, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if isinstance(schema, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if isinstance(schema, str):
        return str(value)
    return value


def is_float(value):
    """Проверяет, представляет ли строка число с плавающей точкой"""
    try:
        float(value)
        return True
    except ValueError:
        return False


def service_exists_required(f):
    """Декоратор для проверки существования сервиса"""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        service_name = kwargs.get('service_name')
        if service_name:
            try:
                service_dir = PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400

            configs = get_all_configs()
            if service_name not in configs and not service_dir.exists():
                return jsonify({'success': False, 'error': f'Сервис {service_name} не найден'}), 404

        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
def index():
    """
    Главная страница, отображающая все конфигурации
    """
    configs = get_all_configs()
    return render_template('index.html', configs=configs)


@app.route('/service/<service_name>')
def service_config(service_name):
    """
    Страница, отображающая конфигурацию для конкретного сервиса
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError:
        return "Сервис не найден", 404

    configs = get_all_configs()

    if service_name not in configs:
        return "Сервис не найден", 404

    service_config = configs[service_name]
    return render_template('service_config.html',
                          service_name=service_name,
                          config=service_config['data'])


@app.route('/update/<service_name>', methods=['POST'])
def update_service_config(service_name):
    """
    Обновление конфигурации для конкретного сервиса
    """
    try:
        try:
            PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        # Проверка существования сервиса
        configs = get_all_configs()
        if service_name not in configs:
            return jsonify({'success': False, 'error': f'Сервис {service_name} не найден'}), 404

        existing_config = configs[service_name]['data']

        # Обработка как прямого JSON, так и данных формы
        if request.is_json:
            new_config = request.get_json()
        else:
            # Обработка данных формы с плоскими ключами
            form_data = {}
            for key, value in request.form.items():
                if key != 'csrf_token':  # Пропуск токена CSRF, если присутствует
                    form_data[key] = value
            # Преобразование данных формы обратно во вложенную структуру
            new_config = unflatten_dict(form_data)

        if isinstance(new_config, dict):
            new_config = coerce_to_schema(new_config, existing_config)

        if not new_config or not isinstance(new_config, dict):
            return jsonify({'success': False, 'error': 'Не предоставлены допустимые данные конфигурации'}), 400

        success, message = update_config(service_name, new_config)

        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 400

    except json.JSONDecodeError as e:
        return jsonify({'success': False, 'error': f'Неверный формат JSON: {str(e)}'}), 400
    except Exception as e:
        logging.error(f"Неожиданная ошибка в update_service_config для {service_name}: {str(e)}")
        return jsonify({'success': False, 'error': f'Ошибка сервера: {str(e)}'}), 500


@app.route('/reload')
def reload_configs():
    """
    Перезагрузка всех конфигураций (обновление с диска)
    """
    configs = get_all_configs()
    return jsonify({'configs': configs})


@app.route('/status/<service_name>')
def service_status(service_name):
    """
    Получение статуса сервиса (запущен или нет)
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    proc = find_service_process(service_name)
    if proc:
        return jsonify({
            'service': service_name,
            'status': 'running',
            'pid': proc.pid,
            'name': proc.name()
        })
    else:
        return jsonify({
            'service': service_name,
            'status': 'stopped'
        })


@app.route('/start/<service_name>', methods=['POST'])
@service_exists_required
def start_service_endpoint(service_name):
    """
    Запуск конкретного сервиса
    """
    success, message = start_service(service_name)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400


@app.route('/stop/<service_name>', methods=['POST'])
@service_exists_required
def stop_service_endpoint(service_name):
    """
    Остановка конкретного сервиса
    """
    success, message = stop_service(service_name)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400


@app.route('/restart/<service_name>', methods=['POST'])
@service_exists_required
def restart_service_endpoint(service_name):
    """
    Перезапуск конкретного сервиса
    """
    success, message = restart_service(service_name)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400


@app.route('/services/status')
def all_services_status():
    """
    Получение статуса всех сервисов
    """
    configs = get_all_configs()
    statuses = {}

    for service_name in configs.keys():
        proc = find_service_process(service_name)
        if proc:
            statuses[service_name] = {
                'status': 'running',
                'pid': proc.pid,
                'name': proc.name()
            }
        else:
            statuses[service_name] = {
                'status': 'stopped'
            }

    return jsonify({'services': statuses})


@app.route('/logs')
def view_all_logs():
    """
    Просмотр логов всех сервисов
    """
    all_logs = get_all_service_logs(lines_per_service=100)
    return render_template('all_logs.html', all_logs=all_logs)


@app.route('/logs/<service_name>')
def view_service_logs(service_name):
    """
    Просмотр логов конкретного сервиса
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError:
        return "Сервис не найден", 404

    configs = get_all_configs()

    if service_name not in configs:
        return "Сервис не найден", 404

    try:
        lines = int(request.args.get('lines', 200))
    except ValueError:
        lines = 200
    log_content = get_log_content(service_name, lines)
    return render_template('service_logs.html',
                          service_name=service_name,
                          log_content=log_content)


@app.route('/api/logs/<service_name>')
def api_get_service_logs(service_name):
    """
    Конечная точка API для получения логов сервиса в формате JSON
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    configs = get_all_configs()

    if service_name not in configs:
        return jsonify({'error': f'Сервис {service_name} не найден'}), 404

    log_content = get_log_content(service_name, 100)
    return jsonify({'service': service_name, 'log_content': log_content})


@app.route('/api/logs')
def api_get_all_logs():
    """
    Конечная точка API для получения логов всех сервисов
    """
    all_logs = get_all_service_logs(lines_per_service=50)
    return jsonify({'logs': all_logs})


@app.route('/grpc_connections')
def view_grpc_connections():
    """
    Просмотр всех gRPC соединений между сервисами
    """
    # Получаем зарегистрированные соединения
    active_connections = grpc_conn_manager.get_connections()

    # Получаем обнаруженные соединения из конфигураций
    detected_connections = detect_grpc_connections()

    # Объединяем оба списка
    all_connections = {**active_connections, **detected_connections}

    return render_template('grpc_connections.html', connections=all_connections)


@app.route('/api/grpc_connections')
def api_get_all_grpc_connections():
    """
    API endpoint для получения всех gRPC соединений
    """
    # Получаем зарегистрированные соединения
    active_connections = grpc_conn_manager.get_connections()

    # Получаем обнаруженные соединения из конфигураций
    detected_connections = detect_grpc_connections()

    # Объединяем оба списка
    all_connections = {**active_connections, **detected_connections}

    return jsonify({'connections': all_connections})


@app.route('/api/grpc_connections/<service_name>')
def api_get_service_grpc_connections(service_name):
    """
    API endpoint для получения gRPC соединений конкретного сервиса
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    connections = grpc_conn_manager.get_service_connections(service_name)
    return jsonify({'connections': connections})


@app.route('/api/grpc_connections/<service_name>/<target_service>', methods=['DELETE'])
def api_close_grpc_connection(service_name, target_service):
    """
    API endpoint для закрытия gRPC соединения между сервисами
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, target_service)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    success = grpc_conn_manager.close_connection(service_name, target_service)
    if success:
        return jsonify({'success': True, 'message': f'Соединение {service_name} -> {target_service} закрыто'})
    else:
        return jsonify({'success': False, 'error': f'Не удалось закрыть соединение {service_name} -> {target_service}'}), 400


@app.route('/api/grpc_connections/<service_name>/<target_service>/health')
def api_check_grpc_connection_health(service_name, target_service):
    """
    API endpoint для проверки здоровья gRPC соединения
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, target_service)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    health_status = grpc_conn_manager.check_connection_health(service_name, target_service)
    return jsonify({'health': health_status})


# === Proto Management Endpoints ===

@app.route('/protos')
def view_all_protos():
    """
    Просмотр всех proto файлов в системе
    """
    proto_files = proto_manager.find_all_proto_files()
    return render_template('proto_browser.html', proto_files=proto_files)


@app.route('/protos/<service_name>')
def view_service_protos(service_name):
    """
    Просмотр proto файлов для конкретного сервиса
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return "Сервис не найден", 404

    # Получаем все proto файлы
    all_protos = proto_manager.find_all_proto_files()
    service_protos = all_protos.get(service_name, [])

    return render_template('service_protos.html',
                          service_name=service_name,
                          proto_files=service_protos)


@app.route('/protos/edit/<service_name>/<path:proto_filename>')
def edit_proto_file(service_name, proto_filename):
    """
    Редактирование конкретного proto файла
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return "Сервис не найден", 404

    try:
        content = proto_manager.read_proto_file(service_name, proto_filename)
        return render_template('proto_editor.html',
                              service_name=service_name,
                              proto_filename=proto_filename,
                              content=content)
    except Exception as e:
        return f"Ошибка чтения файла: {str(e)}", 500


@app.route('/api/protos')
def api_get_all_protos():
    """
    API endpoint для получения всех proto файлов
    """
    try:
        proto_files = proto_manager.find_all_proto_files()
        return jsonify({'proto_files': proto_files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/protos/<service_name>')
def api_get_service_protos(service_name):
    """
    API endpoint для получения proto файлов конкретного сервиса
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        all_protos = proto_manager.find_all_proto_files()
        service_protos = all_protos.get(service_name, [])
        return jsonify({'proto_files': service_protos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/protos/<service_name>/<path:proto_filename>')
def api_get_proto_file(service_name, proto_filename):
    """
    API endpoint для получения содержимого proto файла
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        content = proto_manager.read_proto_file(service_name, proto_filename)
        return jsonify({'content': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/protos/<service_name>/<path:proto_filename>', methods=['PUT'])
def api_update_proto_file(service_name, proto_filename):
    """
    API endpoint для обновления содержимого proto файла
    """
    try:
        PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        data = request.get_json()
        if not data or 'content' not in data:
            return jsonify({'error': 'Content is required'}), 400

        success = proto_manager.write_proto_file(service_name, proto_filename, data['content'])
        if success:
            return jsonify({'success': True, 'message': 'Proto файл успешно обновлен'})
        else:
            return jsonify({'success': False, 'error': 'Не удалось обновить файл'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


class ProtoCompiler:
    """Класс для компиляции proto файлов в pb2 файлы"""

    def __init__(self, services_dir):
        self.services_dir = services_dir
        self.compiler_path = self._find_compiler()

    def _find_compiler(self):
        """Находит protoc компилятор в системе"""
        import shutil
        compiler_path = shutil.which('protoc')
        if not compiler_path:
            # Попробуем найти в стандартных местах
            possible_paths = [
                './protoc/bin/protoc',
                '/usr/local/bin/protoc',
                '/opt/protoc/bin/protoc',
                'C:/Program Files/protoc/bin/protoc.exe',
                'C:/protoc/bin/protoc.exe'
            ]
            for path in possible_paths:
                if Path(path).exists():
                    compiler_path = path
                    break

        return compiler_path

    def _sanitize_proto_content(self, proto_content):
        """Sanitizes proto content to prevent malicious code injection"""
        # Check for potentially dangerous patterns in proto content
        dangerous_patterns = [
            'import os',
            'import sys',
            'import subprocess',
            'import shutil',
            'exec(',
            'eval(',
            '__import__(',
            'open(',
            'file(',
            'getattr(',
            'setattr(',
            'delattr(',
            'compile(',
            'execfile(',
        ]

        lower_content = proto_content.lower()
        for pattern in dangerous_patterns:
            if pattern in lower_content:
                raise ValueError(f"Обнаружено потенциально опасное выражение в proto файле: {pattern}")

        return proto_content

    def validate_proto_syntax(self, proto_content):
        """Проверяет синтаксис proto файла"""
        import tempfile
        import subprocess

        if not self.compiler_path:
            return False, "protoc компилятор не найден в системе"

        try:
            # Sanitize the proto content first
            sanitized_content = self._sanitize_proto_content(proto_content)

            # Создаем временный файл для проверки синтаксиса
            with tempfile.NamedTemporaryFile(mode='w', suffix='.proto', delete=False) as temp_file:
                temp_file.write(sanitized_content)
                temp_proto_path = temp_file.name

            # Проверяем синтаксис с помощью protoc
            cmd = [
                self.compiler_path,
                '--error_format=json',
                f"--proto_path={os.path.dirname(temp_proto_path)}",
                temp_proto_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

            # Удаляем временный файл
            os.unlink(temp_proto_path)

            if result.returncode == 0:
                return True, "Синтаксис корректен"
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                return False, f"Ошибка синтаксиса: {error_msg}"

        except ValueError as e:
            # This is raised by our sanitization function
            return False, str(e)
        except Exception as e:
            return False, f"Ошибка проверки синтаксиса: {str(e)}"

    def compile_proto(self, service_name, proto_filename):
        """Компилирует proto файл в pb2 файлы"""
        import subprocess
        import tempfile

        if not self.compiler_path:
            return False, "protoc компилятор не найден в системе"

        try:
            service_dir = PathValidator.resolve_service_dir(self.services_dir, service_name)
            proto_path = service_dir / proto_filename

            if not proto_path.exists():
                return False, f"Proto файл не найден: {proto_path}"

            # Проверяем, что это действительно proto файл
            if proto_path.suffix.lower() != '.proto':
                return False, "Файл должен иметь расширение .proto"

            # Читаем и sanitize содержимое proto файла перед компиляцией
            with open(proto_path, 'r', encoding='utf-8') as f:
                proto_content = f.read()

            # Sanitize the content
            self._sanitize_proto_content(proto_content)

            # Компиляция proto в pb2 файлы
            cmd = [
                self.compiler_path,
                f"--proto_path={service_dir}",
                f"--python_out={service_dir}",
                f"--grpc_python_out={service_dir}",
                str(proto_path)
            ]

            # Ограничиваем время выполнения команды
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                # Успешная компиляция
                pb2_file = service_dir / f"{proto_path.stem}_pb2.py"
                grpc_pb2_file = service_dir / f"{proto_path.stem}_pb2_grpc.py"

                generated_files = []
                if pb2_file.exists():
                    generated_files.append(str(pb2_file.name))
                if grpc_pb2_file.exists():
                    generated_files.append(str(grpc_pb2_file.name))

                return True, f"Успешно скомпилировано. Созданы файлы: {', '.join(generated_files)}"
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                return False, f"Ошибка компиляции: {error_msg}"

        except ValueError as e:
            return False, str(e)
        except subprocess.TimeoutExpired:
            return False, "Таймаут выполнения компиляции. Команда заняла слишком много времени."
        except Exception as e:
            return False, f"Ошибка компиляции: {str(e)}"


# Initialize the proto compiler
proto_compiler = ProtoCompiler(SERVICES_DIR)


@app.route('/api/proto/compile', methods=['POST'])
def api_compile_proto():
    """
    API endpoint для компиляции proto файла в pb2
    """
    try:
        data = request.get_json()
        if not data or 'service_name' not in data or 'proto_filename' not in data:
            return jsonify({'error': 'Service name and proto filename are required'}), 400

        service_name = data['service_name']
        proto_filename = data['proto_filename']

        # Проверяем имя сервиса
        try:
            PathValidator.resolve_service_dir(BASE_SERVICES_DIR, service_name)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        success, message = proto_compiler.compile_proto(service_name, proto_filename)

        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/proto/validate', methods=['POST'])
def api_validate_proto():
    """
    API endpoint для проверки синтаксиса proto файла
    """
    try:
        data = request.get_json()
        if not data or 'content' not in data:
            return jsonify({'error': 'Proto content is required'}), 400

        proto_content = data['content']

        is_valid, message = proto_compiler.validate_proto_syntax(proto_content)

        return jsonify({
            'valid': is_valid,
            'message': message
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    # Запуск потока очистки процессов
    cleanup_thread = threading.Thread(target=cleanup_terminated_processes, daemon=True)
    cleanup_thread.start()

    if AUTH_REQUIRED and (not AUTH_USERNAME or not AUTH_PASSWORD):
        raise RuntimeError("WEB_CONFIG_USERNAME и WEB_CONFIG_PASSWORD должны быть установлены, когда требуется аутентификация.")

    # Загрузка настроек из config.json
    config_path = Path("config.json")
    if config_path.exists():
        import json
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        host = config.get("server", {}).get("host", "0.0.0.0")
        port = config.get("server", {}).get("port", 50055)
        debug = config.get("server", {}).get("debug", DEBUG_MODE)
    else:
        host = "0.0.0.0"
        port = 50055
        debug = DEBUG_MODE

    app.run(host=host, port=port, debug=debug)