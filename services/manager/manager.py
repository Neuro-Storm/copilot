"""
Модуль менеджера файлов для RAG-системы.

Этот модуль реализует компонент, который управляет жизненным циклом файлов в RAG-системе:
- Сканирование директории на наличие новых файлов
- Управление очередью обработки файлов
- Интеграция с сервисами конвертации и индексации
- Предоставление HTTP API для мониторинга и управления
- Обеспечение безопасности через валидацию путей и токенную авторизацию

Поддерживаемые форматы файлов: PDF, DOCX, PPTX, TXT, HTML, HTM, TIF, TIFF, MD
"""

import os
import sys
import time
import json
import sqlite3
import grpc
import logging
import shutil
import re
from pathlib import Path
from datetime import datetime
from functools import wraps
import threading
from queue import Queue, Empty, Full

# Добавляем Flask для HTTP API
from flask import Flask, jsonify, request, Response

# Загрузка секретного ключа для авторизации
from dotenv import load_dotenv
load_dotenv()
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / '.env')

# Добавляем common в путь для импорта middleware
_common_path = str(Path(__file__).parent.parent / 'common')
if _common_path not in sys.path:
    sys.path.append(_common_path)
from auth_middleware import require_role

AUTH_SECRET_KEY = os.getenv('AUTH_SECRET_KEY', 'change-me-in-production')

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Декоратор для обработки общих исключений в API
def handle_api_errors(f):
    """Декоратор для обработки общих исключений в API"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка в API: {e}")
            return jsonify({'error': str(e)}), 500
    return decorated_function

# Пытаемся импортировать сгенерированные gRPC файлы
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    import converter_pb2
    import converter_pb2_grpc
    import indexer_pb2
    import indexer_pb2_grpc
except ImportError:
    logger.error("ОШИБКА: Не найдены gRPC модули.")
    logger.error("Запустите: python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. *.proto")
    sys.exit(1)

# --- Конфигурация и Утилиты ---

def safe_filename(filename):
    """
    Очистка имени файла от опасных символов.
    Убирает путевые разделители, спецсимволы, ведущие точки.
    Использует только re из стандартной библиотеки.
    """
    # Берём только имя файла (убираем путь)
    filename = filename.replace('\\', '/').split('/')[-1]
    # Убираем всё кроме букв, цифр, пробелов, точек, дефисов, подчёркиваний
    filename = re.sub(r'[^\w\s\-.]', '', filename).strip()
    # Пробелы → подчёркивания
    filename = re.sub(r'\s+', '_', filename)
    # Убираем ведущие точки (скрытые файлы в Linux)
    filename = filename.lstrip('.')
    return filename or 'unnamed'


class Config:
    """
    Класс для управления конфигурацией приложения.

    Загружает настройки из JSON-файла, при отсутствии файла использует значения по умолчанию.
    Поддерживает следующие параметры:
    - files_dir: директория для хранения файлов
    - db_path: путь к файлу базы данных
    - converter_address: адрес gRPC-сервиса конвертации
    - indexer_address: адрес gRPC-сервиса индексации
    - scan_interval: интервал сканирования директории (в секундах)
    - api_port: порт для HTTP API
    - max_workers: максимальное количество рабочих потоков
    """
    def __init__(self, config_path="config.json"):
        """
        Инициализирует объект конфигурации.

        Args:
            config_path (str): путь к файлу конфигурации (по умолчанию "config.json")
        """
        self.defaults = {
            "files_dir": "./files",
            "db_path": "files.db",
            "converter_address": "localhost:50053",
            "indexer_address": "localhost:50054",
            "scan_interval": 5,
            "api_port": 5001,
            "max_workers": 4,
            "converter_timeout": 60,
            "indexer_timeout": 120,
            "worker_shutdown_timeout": 5.0,
            "queue_operation_timeout": 1,
            "max_upload_size_mb": 100,
            "allowed_extensions": [".pdf", ".docx", ".pptx", ".txt", ".html", ".htm", ".tif", ".tiff"],
            "converted_md_base": None
        }
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"Конфиг {config_path} не найден или некорректен, используем по умолчанию.")
            self.data = self.defaults

    def get(self, key):
        """
        Возвращает значение параметра конфигурации.

        Сначала ищет в загруженной конфигурации, затем в значениях по умолчанию.

        Args:
            key (str): ключ параметра конфигурации

        Returns:
            значение параметра или None, если параметр не найден
        """
        return self.data.get(key, self.defaults.get(key))

# --- Работа с БД ---

class FileManager:
    """
    Класс для управления файлами в базе данных.

    Отвечает за:
    - Хранение информации о файлах
    - Сканирование директории на наличие новых/измененных файлов
    - Управление статусами файлов
    - Обеспечение безопасности путей к файлам
    - Кэширование статистики
    - Потокобезопасную работу с базой данных SQLite
    - Проверку и нормализацию путей к файлам
    - Обновление статусов файлов во время обработки
    """
    def __init__(self, db_path, files_dir):
        """
        Инициализирует FileManager.

        Args:
            db_path (str): путь к файлу базы данных SQLite
            files_dir (str): директория для сканирования файлов
        """
        self.db_path = db_path
        self.files_dir = Path(files_dir)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        # Блокировка для кэша статистики
        self.stats_cache_lock = threading.Lock()
        self.init_db()
        # Кэш для статистики
        self.stats_cache = {}
        self.cache_timestamp = 0
        self.cache_timeout = 2  # 2 секунды кэширования
        # Список активных соединений для закрытия при необходимости
        self.active_connections = []

    def get_connection(self):
        """
        Создает новое подключение к базе данных.

        Использует параметр check_same_thread=False для обеспечения потокобезопасности.
        Настройки SQLite оптимизированы для многопоточного доступа и производительности.

        Returns:
            sqlite3.Connection: подключение к базе данных
        """
        # Параметр check_same_thread=False позволяет использовать подключение в разных потоках
        conn = sqlite3.connect(self.db_path, check_same_thread=False)

        # Установка настроек для текущего соединения
        conn.execute("PRAGMA journal_mode=WAL;")  # WAL режим для лучшей конкурентности
        conn.execute("PRAGMA synchronous=NORMAL;")  # Баланс между производительностью и безопасностью
        conn.execute("PRAGMA cache_size=1000;")  # Размер кэша в страницах
        conn.execute("PRAGMA temp_store=MEMORY;")  # Хранить временные таблицы в памяти

        return conn

    def close_connections(self):
        """
        Закрывает подключения к базе данных.

        В текущей реализации метод не используется, так как подключения создаются
        для каждой операции и закрываются автоматически при выходе из контекста,
        но сохранен для совместимости.
        """
        # Since we're creating new connections for each operation and closing them automatically,
        # this method is no longer needed for normal operation.
        # But we keep it for compatibility if called
        pass

    def init_db(self):
        """
        Инициализирует таблицы базы данных.

        Создает таблицу files с полями:
        - id: уникальный идентификатор
        - filename: имя файла
        - file_path: путь к файлу
        - md_path: путь к преобразованному MD-файлу
        - status: статус обработки ('pending', 'processing', 'indexed', 'failed', 'deleted', 'conversion_success_only')
        - error_message: сообщение об ошибке при обработке файла
        - updated_at: дата последнего обновления

        Также создает индексы для ускорения поиска и оптимизирует настройки SQLite для многопоточного доступа.
        """
        with self.get_connection() as conn:
            # Настройка SQLite для лучшей совместимости с многопоточным доступом
            conn.execute("PRAGMA journal_mode=WAL;")  # WAL режим для лучшей конкурентности
            conn.execute("PRAGMA synchronous=NORMAL;")  # Баланс между производительностью и безопасностью
            conn.execute("PRAGMA cache_size=1000;")  # Размер кэша в страницах
            conn.execute("PRAGMA temp_store=MEMORY;")  # Хранить временные таблицы в памяти

            conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    md_path TEXT,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Добавляем столбец error_message, если его еще нет
            try:
                conn.execute("ALTER TABLE files ADD COLUMN error_message TEXT")
            except sqlite3.OperationalError:
                # Столбец уже существует
                pass
            # Индексы для быстрого поиска
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_path ON files(file_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
            # Добавляем индекс для улучшения производительности запросов в get_pending_task
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status_updated_at ON files(status, updated_at)")

    def scan_directory(self):
        """
        Сканирует директорию, добавляет новые, обновляет измененные, помечает удаленные.

        Алгоритм:
        1. Загружает все файлы из БД
        2. Проходит по файловой системе и сравнивает с данными из БД
        3. Выполняет пакетные операции для новых, измененных и удаленных файлов
        4. Сбрасывает кэш статистики
        5. Пропускает файлы в директории converted_md и файлы с неподдерживаемыми расширениями

        Поддерживаемые форматы файлов: PDF, DOCX, PPTX, TXT, HTML, HTM

        Returns:
            int: количество добавленных файлов
        """
        added = 0
        current_paths = set()

        # Убираем .md из списка поддерживаемых форматов, чтобы не обрабатывать конвертированные файлы
        supported = {'.pdf', '.docx', '.pptx', '.txt', '.html', '.htm', '.tif', '.tiff'}

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Загружаем все файлы из БД одним запросом
            cursor.execute("SELECT file_path, updated_at, status FROM files")
            db_files = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

            logger.debug(f"Найдено файлов в БД: {len(db_files)}")
            for path, (time, status) in db_files.items():
                logger.debug(f"  Файл в БД: {path}, статус: {status}")

            new_files = []
            updated_files = []

            # 2. Проходим по файловой системе
            for p in self.files_dir.rglob('*'):
                logger.debug(f"Обрабатываем файл: {p}, is_file: {p.is_file()}, suffix: {p.suffix.lower()}")

                if p.is_file() and p.suffix.lower() in supported:
                    # Пропускаем файлы в директории converted_md
                    # Преобразуем пути к одному формату для сравнения
                    try:
                        rel_path = p.relative_to(self.files_dir)
                        if 'converted_md' in str(rel_path).split(os.sep):
                            logger.debug(f"Пропускаем файл в converted_md: {p}")
                            continue
                    except ValueError:
                        # Если путь не внутри self.files_dir, пропускаем
                        logger.debug(f"Путь не внутри files_dir, пропускаем: {p}")
                        continue

                    logger.debug(f"Файл прошел проверку converted_md: {p}")

                    try:
                        # Валидируем путь к файлу
                        path_str = self.validate_file_path(str(p.resolve()))
                        logger.debug(f"Добавляем путь в current_paths: {path_str}")
                        current_paths.add(path_str)

                        if path_str not in db_files:
                            # Новый файл - добавляем в пакет
                            logger.debug(f"Новый файл: {p.name} -> {path_str}")
                            new_files.append((p.name, path_str, 'pending'))
                        else:
                            # Проверяем обновление
                            db_time_str, status = db_files[path_str]
                            file_mtime = datetime.fromtimestamp(p.stat().st_mtime)
                            logger.debug(f"Файл уже в БД: {path_str}, статус: {status}, время БД: {db_time_str}, время файла: {file_mtime}")
                            try:
                                if '.' in db_time_str:
                                    db_time_str = db_time_str.split('.')[0]
                                db_time = datetime.fromisoformat(db_time_str)
                                if file_mtime > db_time and status != 'pending' and status != 'processing':
                                    logger.debug(f"Файл обновлен: {path_str}")
                                    updated_files.append((path_str,))
                            except ValueError:
                                logger.debug(f"Ошибка преобразования времени для файла: {path_str}")
                                pass
                    except ValueError as e:
                        logger.warning(f"Некорректный путь к файлу: {e}")
                        continue

            logger.debug(f"Всего найдено файлов в файловой системе: {len(current_paths)}")
            logger.debug(f"Новых файлов: {len(new_files)}, обновленных: {len(updated_files)}")

            # 3. Пакетные операции
            if new_files:
                cursor.executemany(
                    "INSERT INTO files (filename, file_path, status) VALUES (?, ?, ?)",
                    new_files
                )
                added = len(new_files)
                logger.info(f"Добавлено новых файлов: {added}")

            if updated_files:
                cursor.executemany(
                    "UPDATE files SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE file_path = ?",
                    updated_files
                )
                logger.info(f"Обновлено файлов: {len(updated_files)}")

            # 4. Помечаем удаленные
            logger.debug("Проверяем файлы на удаление...")
            deleted_files = []
            for p in db_files:
                if p not in current_paths and db_files[p][1] != 'deleted':
                    logger.debug(f"Файл будет помечен как удаленный: {p}")
                    deleted_files.append((p,))

            logger.debug(f"Будет помечено как удаленных: {len(deleted_files)}")

            if deleted_files:
                cursor.executemany(
                    "UPDATE files SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE file_path = ?",
                    deleted_files
                )
                logger.info(f"Помечено как удаленных файлов: {len(deleted_files)}")

            conn.commit()

        # Сброс кэша статистики при изменении
        self._invalidate_cache()
        return added

    def get_pending_task(self):
        """
        Возвращает одну задачу (файл) для обработки.

        Выбирает файл со статусом 'pending', переводит его в 'processing' и проверяет валидность пути.
        Использует атомарные операции для предотвращения конфликта при многопоточной обработке.

        Returns:
            tuple or None: кортеж (id, filename, file_path) или None, если нет задач
        """
        try:
            with self.get_connection() as conn:
                # Начинаем транзакцию
                conn.execute("BEGIN IMMEDIATE")

                # Выбираем один файл со статусом 'pending'
                row = conn.execute(
                    "SELECT id, filename, file_path FROM files WHERE status = 'pending' ORDER BY id LIMIT 1"
                ).fetchone()

                if row:
                    # Атомарно обновляем статус файла на 'processing'
                    updated = conn.execute(
                        "UPDATE files SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
                        (row[0],)
                    ).rowcount

                    # Фиксируем транзакцию
                    conn.commit()

                    # Если обновление не произошло (файл уже был захвачен другим процессом), возвращаем None
                    if updated != 1:
                        logger.debug(f"Файл с ID {row[0]} уже был захвачен другим процессом")
                        return None

                    # Проверяем валидность пути к файлу
                    try:
                        validated_path = self.validate_file_path(row[2])
                        # Возвращаем обновленный кортеж с проверенным путем
                        logger.debug(f"Выбран файл для обработки: ID={row[0]}, Name={row[1]}, Path={validated_path}")
                        return (row[0], row[1], validated_path)
                    except ValueError as e:
                        logger.warning(f"Некорректный путь к файлу в БД: {e}")
                        return None
                else:
                    # Если нет подходящих файлов, просто фиксируем транзакцию
                    conn.commit()

            logger.debug("Нет файлов со статусом 'pending' для обработки")
            return None
        except sqlite3.OperationalError as e:
            logger.error(f"Ошибка базы данных при получении задачи: {e}")
            # Возвращаем None, чтобы не прерывать основной цикл
            return None
        except Exception as e:
            logger.error(f"Неожиданная ошибка при получении задачи: {e}")
            # Возвращаем None, чтобы не прерывать основной цикл
            return None

    def update_status(self, file_id, status, md_path=None, error_message=None):
        """
        Обновляет статус файла в базе данных.

        Args:
            file_id (int): идентификатор файла
            status (str): новый статус файла
            md_path (str, optional): путь к MD-файлу после конвертации
            error_message (str, optional): сообщение об ошибке
        Returns:
            bool: True, если обновление прошло успешно, иначе False
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Build the update dynamically but in a simple way
                fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
                values = [status]

                if md_path is not None:
                    fields.append("md_path = ?")
                    values.append(md_path)

                if error_message is not None:
                    fields.append("error_message = ?")
                    values.append(error_message)

                values.append(file_id)

                query = f"UPDATE files SET {', '.join(fields)} WHERE id = ?"
                cursor.execute(query, values)

                rows_affected = cursor.rowcount
                conn.commit()

                if rows_affected == 0:
                    logger.warning(f"Предупреждение: файл с ID {file_id} не найден в базе данных")
                    return False

                logger.debug(f"Статус файла с ID {file_id} успешно обновлен на '{status}'")
                return True

        except sqlite3.Error as e:
            logger.error(f"Ошибка при обновлении статуса файла с ID {file_id}: {e}")
            return False
        finally:
            # Сброс кэша статистики при изменении
            self._invalidate_cache()

    def _invalidate_cache(self):
        """Сбрасывает кэш статистики при изменении данных."""
        self.stats_cache = {}
        self.cache_timestamp = 0

    def get_files(self, limit=50, offset=0, status_filter=''):
        """
        Получить список файлов с пагинацией и фильтрацией.

        Args:
            limit (int): максимальное количество возвращаемых файлов
            offset (int): смещение для пагинации
            status_filter (str): фильтр по статусу (если пустой, то без фильтрации)

        Returns:
            list: список словарей с информацией о файлах
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            if status_filter:
                cursor.execute("""
                    SELECT id, filename, file_path, md_path, status, error_message, updated_at
                    FROM files
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                """, (status_filter, limit, offset))
            else:
                cursor.execute("""
                    SELECT id, filename, file_path, md_path, status, error_message, updated_at
                    FROM files
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset))

            columns = ['id', 'filename', 'file_path', 'md_path', 'status', 'error_message', 'updated_at']
            files = []
            for row in cursor.fetchall():
                file_dict = dict(zip(columns, row))

                # Проверяем валидность пути к файлу
                try:
                    if file_dict['file_path']:
                        file_dict['file_path'] = self.validate_file_path(file_dict['file_path'])
                except ValueError as e:
                    logger.warning(f"Некорректный путь к файлу в БД: {e}")
                    file_dict['file_path'] = None  # или можно пропустить этот файл

                files.append(file_dict)

            return files

    def get_file_stats(self):
        """
        Получить статистику по файлам с кэшированием.

        Включает общее количество файлов, количество по статусам и время последнего обновления.
        Использует кэширование для улучшения производительности.

        Returns:
            dict: словарь с информацией о статистике файлов
        """
        import time
        current_time = time.time()

        # Проверяем, нужно ли обновить кэш для избежания частых обращений к БД
        # Кэш обновляется каждые 2 секунды (cache_timeout)
        with self.stats_cache_lock:
            if current_time - self.cache_timestamp < self.cache_timeout and self.stats_cache:
                return self.stats_cache.copy()  # Возвращаем копию для безопасности

            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Общее количество файлов для статистики
                cursor.execute("SELECT COUNT(*) FROM files")
                total_files = cursor.fetchone()[0]

                # Количество файлов по статусам для анализа прогресса обработки
                cursor.execute("SELECT status, COUNT(*) FROM files GROUP BY status")
                status_counts = dict(cursor.fetchall())

                # Время последнего обновления для отслеживания активности
                cursor.execute("SELECT MAX(updated_at) FROM files")
                last_update = cursor.fetchone()[0]

                stats = {
                    'total_files': total_files,
                    'status_counts': status_counts,
                    'last_update': last_update
                }

                # Обновляем кэш под блокировкой для обеспечения потокобезопасности
                self.stats_cache = stats
                self.cache_timestamp = current_time

                return stats.copy()  # Возвращаем копию для безопасности

    def get_total_files_count(self, status_filter=''):
        """
        Получить общее количество файлов.

        Args:
            status_filter (str): фильтр по статусу (если пустой, то без фильтрации)

        Returns:
            int: общее количество файлов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            if status_filter:
                cursor.execute("SELECT COUNT(*) FROM files WHERE status = ?", (status_filter,))
            else:
                cursor.execute("SELECT COUNT(*) FROM files")

            return cursor.fetchone()[0]

    def get_unique_statuses(self):
        """
        Получить список уникальных статусов файлов.

        Returns:
            list: список уникальных статусов
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT status FROM files ORDER BY status")
            return [row[0] for row in cursor.fetchall()]

    def retry_file_processing(self, file_id):
        """
        Пометить файл для повторной обработки.

        Изменяет статус файла на 'pending', чтобы он снова был обработан.

        Args:
            file_id (int): идентификатор файла

        Returns:
            bool: True, если файл был найден и обновлен, иначе False
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE files SET status = 'pending', error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (file_id,)
            )
            conn.commit()

            # Сброс кэша статистики при изменении
            self._invalidate_cache()

            return cursor.rowcount > 0

    def validate_file_path(self, file_path):
        """
        Проверка, что путь к файлу находится в разрешенном каталоге.

        Защита от атак с использованием относительных путей (path traversal).
        Нормализует путь и проверяет, что он находится внутри разрешённой директории.

        Args:
            file_path (str): путь к файлу для проверки

        Returns:
            str: проверенный и нормализованный путь к файлу

        Raises:
            ValueError: если путь содержит нулевой байт или находится вне разрешенной директории
        """
        try:
            # Проверка на нулевые байты
            if '\0' in file_path:
                raise ValueError(f"Путь содержит нулевой байт: {file_path}")

            # Разрешаем и нормализуем путь, чтобы убедиться, что он находится внутри разрешенной директории
            # resolve() разрешает все ../ и символические ссылки, is_relative_to() полностью защищает от path traversal
            resolved_path = Path(file_path).resolve()
            allowed_dir = Path(self.files_dir).resolve()
            if not resolved_path.is_relative_to(allowed_dir):
                raise ValueError(f"Путь к файлу {file_path} находится вне разрешенного каталога")
            return str(resolved_path)
        except (OSError, ValueError) as e:
            # Конкретные исключения для лучшей диагностики
            raise ValueError(f"Некорректный путь к файлу: {file_path} — {e}")

    def delete_file(self, file_id):
        """
        Пометить файл как удаленный.

        Фактически не удаляет файл из базы данных, а изменяет его статус на 'deleted'.

        Args:
            file_id (int): идентификатор файла

        Returns:
            bool: True, если файл был найден и обновлен, иначе False
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE files SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (file_id,)
            )
            conn.commit()

            # Сброс кэша статистики при изменении
            self._invalidate_cache()

            return cursor.rowcount > 0

    def list_subfolders(self):
        """Список подпапок в files_dir для отображения в UI."""
        subfolders = []
        excluded = {'converted_md', '__pycache__', '.git'}

        for dirpath, dirnames, _filenames in os.walk(self.files_dir):
            # Исключаем служебные директории из дальнейшего обхода
            dirnames[:] = [d for d in dirnames if d not in excluded]
            rel_path = os.path.relpath(dirpath, self.files_dir)
            if rel_path != '.':
                subfolders.append(rel_path)

        return sorted(subfolders)

    def register_uploaded_file(self, filename, file_path):
        """Регистрация загруженного файла в БД со статусом pending."""
        validated_path = self.validate_file_path(file_path)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Проверяем дубликат — файл с таким путём уже может быть в БД
            cursor.execute("SELECT id FROM files WHERE file_path = ?", (validated_path,))
            if cursor.fetchone():
                return False

            cursor.execute(
                "INSERT INTO files (filename, file_path, status, updated_at) "
                "VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)",
                (filename, validated_path)
            )
            conn.commit()
            self._invalidate_cache()
            return True

    def import_from_folder(self, source_dir, target_subfolder=""):
        """
        Рекурсивный импорт файлов из серверной папки в files_dir.
        Сохраняет структуру подпапок источника.
        """
        supported = set(self._get_supported_extensions())
        source_path = Path(source_dir).resolve()

        if not source_path.exists():
            raise ValueError(f"Папка не найдена: {source_dir}")
        if not source_path.is_dir():
            raise ValueError(f"Путь не является папкой: {source_dir}")

        # Определяем целевую директорию
        if target_subfolder:
            clean_subfolder = Path(target_subfolder)
            if '..' in clean_subfolder.parts:
                raise ValueError("Недопустимый путь подпапки")
            target_dir = Path(self.files_dir).resolve() / clean_subfolder
        else:
            target_dir = Path(self.files_dir).resolve()

        target_dir.mkdir(parents=True, exist_ok=True)

        result = {'copied': 0, 'skipped': 0, 'errors': 0, 'details': []}

        # rglob('*') — рекурсивный обход всех вложенных папок
        for p in source_path.rglob('*'):
            if not p.is_file():
                continue
            if p.suffix.lower() not in supported:
                result['skipped'] += 1
                continue

            try:
                # relative_to — вычисляем путь относительно корня источника
                rel_source = p.relative_to(source_path)
                dest_file = target_dir / rel_source
                dest_file.parent.mkdir(parents=True, exist_ok=True)

                # Если файл уже существует — добавляем timestamp
                if dest_file.exists():
                    stem = dest_file.stem
                    suffix = dest_file.suffix
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    dest_file = dest_file.parent / f"{stem}_{ts}{suffix}"

                # copy2 сохраняет метаданные (время модификации)
                shutil.copy2(str(p), str(dest_file))

                registered = self.register_uploaded_file(
                    dest_file.name, str(dest_file.resolve())
                )
                result['copied' if registered else 'skipped'] += 1
                result['details'].append({
                    'source': str(p),
                    'dest': str(dest_file),
                    'status': 'copied' if registered else 'already_registered'
                })
            except Exception as e:
                result['errors'] += 1
                result['details'].append({
                    'source': str(p), 'error': str(e), 'status': 'error'
                })

        self._invalidate_cache()
        return result

    def _get_supported_extensions(self):
        """Возвращает множество поддерживаемых расширений из конфигурации."""
        # Берём из config.json, иначе используем список по умолчанию
        try:
            cfg_path = Path("config.json")
            if cfg_path.exists():
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    exts = data.get('allowed_extensions')
                    if exts:
                        return set(exts)
        except Exception:
            pass
        return {'.pdf', '.docx', '.pptx', '.txt', '.html', '.htm', '.tif', '.tiff'}

# --- Основной класс ---

class Manager:
    """
    Основной класс менеджера файлов.

    Отвечает за:
    - Управление жизненным циклом файлов
    - Координацию между конвертером и индексером
    - Запуск и остановка сервиса
    - Управление очередью обработки файлов
    - Мониторинг состояния сервиса
    - Обработку файлов в многопоточном режиме
    - Взаимодействие с gRPC-сервисами конвертации и индексации
    - Обработку ошибок и повторные попытки обработки
    """
    def __init__(self):
        """
        Инициализирует менеджер файлов.

        Создает конфигурацию, файловый менеджер и gRPC-каналы для взаимодействия
        с сервисами конвертации и индексации.
        """
        self.cfg = Config()
        self.fm = FileManager(self.cfg.get("db_path"), self.cfg.get("files_dir"))

        # Каналы gRPC
        self.conv_channel = grpc.insecure_channel(self.cfg.get("converter_address"))
        self.conv_stub = converter_pb2_grpc.DoclingConverterStub(self.conv_channel)

        self.idx_channel = grpc.insecure_channel(self.cfg.get("indexer_address"))
        self.idx_stub = indexer_pb2_grpc.IndexerServiceStub(self.idx_channel)

        # Состояние сервиса
        self.running_event = threading.Event()
        self.processing_queue = Queue(maxsize=100)  # Ограничиваем размер очереди
        self.worker_threads = []

    def is_running(self):
        """
        Проверяет, запущен ли сервис.

        Returns:
            bool: True, если сервис запущен, иначе False
        """
        return self.running_event.is_set()

    def _ensure_channels_connected(self):
        """
        Проверяет состояние каналов gRPC и при необходимости восстанавливает соединение.

        gRPC каналы в Python управляют подключением автоматически: при вызове RPC канал
        устанавливает соединение, если его нет, и переподключается при обрыве.
        Метод оставлен для возможного расширения в будущем.
        """
        pass

    def shutdown(self):
        """
        Закрытие gRPC каналов для предотвращения утечки ресурсов.

        Выполняет корректное завершение работы сервиса:
        - Останавливает прием новых задач
        - Отправляет сигнал остановки рабочим потокам
        - Ждет завершения рабочих потоков
        - Закрывает gRPC-каналы
        - Закрывает подключения к базе данных
        - Освобождает все занятые ресурсы
        """
        logger.info("Initiating graceful shutdown...")

        # Stop accepting new tasks
        self.running_event.clear()

        # Добавляем сигнал остановки для всех рабочих потоков
        num_workers = len(self.worker_threads)
        for i in range(num_workers):
            try:
                self.processing_queue.put_nowait(None)  # Используем put_nowait для избежания блокировки
            except:
                pass  # Очередь может быть полной

        # ВАЖНО: Сначала ждем завершения задач в очереди, ПОКА потоки ещё работают
        # и могут вызывать task_done(). После завершения потоков queue.join() зависнет.
        try:
            self.processing_queue.join()  # Wait for all tasks to be done
        except Exception as e:
            logger.warning(f"Ошибка при ожидании завершения задач: {e}")

        # Ждем завершения рабочих потоков с таймаутом
        for thread in self.worker_threads:
            thread.join(timeout=self.cfg.get("worker_shutdown_timeout"))

        # Close gRPC channels
        try:
            if hasattr(self, 'conv_channel'):
                self.conv_channel.close()
            if hasattr(self, 'idx_channel'):
                self.idx_channel.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии gRPC каналов: {e}")

        # Close database connections
        try:
            self.fm.close_connections()
        except Exception as e:
            logger.error(f"Ошибка при закрытии подключений к базе данных: {e}")

        logger.info("Shutdown completed")

    def convert(self, file_path):
        """
        Вызов converter.proto для конвертации файла.

        Если файл уже является MD-файлом, пропускает конвертацию.
        Сохраняет результат конвертации в единую папку converted_md в корне files_dir,
        повторяя структуру подпапок оригинала.

        Args:
            file_path (str): путь к файлу для конвертации

        Returns:
            tuple: (bool, str) - успех операции и путь к результату или сообщение об ошибке
        """
        # Если это уже MD файл, пропускаем конвертацию
        if str(file_path).lower().endswith('.md'):
            return True, str(file_path)

        # --- ЕДИНАЯ ПАПКА: converted_md в корне files_dir ---
        file_p = Path(file_path).resolve()
        files_root = self.fm.files_dir.resolve()

        # Получаем базовую папку для MD из конфига (по умолчанию files/converted_md)
        custom_md_base = self.cfg.get("converted_md_base")
        if custom_md_base:
            md_base = Path(custom_md_base).resolve()
        else:
            md_base = files_root / "converted_md"

        try:
            # Вычисляем относительный путь файла от корня files_dir
            # Например: files/docs/reports/annual.pdf → docs/reports/annual.pdf
            rel_path = file_p.relative_to(files_root)
        except ValueError:
            # Файл вне files_dir — фолбэк на старое поведение
            logger.warning(f"Файл вне files_dir, используем локальную папку: {file_path}")
            md_dir = Path(file_path).parent / "converted_md"
            md_dir.mkdir(exist_ok=True)
            md_out = md_dir / (Path(file_path).stem + ".md")
        else:
            # Собираем путь: md_base/docs/reports/annual.md
            md_dir = md_base / rel_path.parent
            md_dir.mkdir(parents=True, exist_ok=True)
            md_out = md_dir / (Path(file_path).stem + ".md")

        logger.info("-> Конвертация: %s", file_path)
        req = converter_pb2.ConvertRequest(
            input_path=str(file_path),
            return_content=True
        )
        try:
            # Прямой вызов gRPC с таймаутом
            # gRPC каналы управляют подключением автоматически
            resp = self.conv_stub.ConvertFile(req, timeout=self.cfg.get("converter_timeout"))  # 60 секунд

            if resp.success:
                md_out.write_text(resp.markdown_content, encoding='utf-8')
                logger.info("Сохранено в: %s", md_out)
                return True, str(md_out)
            return False, resp.error_message
        except grpc.RpcError as e:
            status_code = e.code().name if hasattr(e, 'code') else 'UNKNOWN'
            details = e.details() if hasattr(e, 'details') else str(e)
            logger.error(f"ConvertFile gRPC ошибка для файла {file_path}: {status_code} - {details}")

            if e.code() == grpc.StatusCode.UNAVAILABLE:
                return False, "Сервис конвертации недоступен"
            elif e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                return False, "Таймаут при конвертации файла"
            elif e.code() == grpc.StatusCode.INTERNAL:
                return False, f"Внутренняя ошибка конвертации: {details}"
            else:
                return False, f"Ошибка конвертации ({status_code}): {details}"
        except Exception as e:
            logger.error(f"Неожиданная ошибка при конвертации файла {file_path}: {e}")
            return False, f"Unexpected error during conversion: {e}"

    def index(self, md_path, original_filename, original_filepath=None):
        """
        Вызов indexer.proto (streaming) для индексации файла.

        Проверяет размер файла перед индексацией (максимальный размер 100MB).
        Использует streaming gRPC для передачи содержимого файла частями.

        Args:
            md_path (str): путь к MD-файлу для индексации
            original_filename (str): оригинальное имя файла
            original_filepath (str, optional): полный путь к оригинальному файлу

        Returns:
            tuple: (bool, str) - успех операции и сообщение об ошибке или OK
        """
        logger.info("-> Индексация: %s", md_path)

        # Проверка размера файла
        try:
            file_size = os.path.getsize(md_path)
            max_size = 100 * 1024 * 1024  # 100 MB
            if file_size > max_size:
                logger.info(f"Файл слишком большой: {file_size} байт, максимум {max_size}")
                return False, f"Файл слишком большой: {file_size} байт, максимум {max_size}"
        except OSError as e:
            logger.info(f"Ошибка получения размера файла: {e}")
            return False, f"Ошибка получения размера файла: {e}"

        try:
            with open(md_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.info(f"Ошибка чтения MD: {e}")
            return False, f"Ошибка чтения MD: {e}"

        def req_iterator():
            # 1. Заголовок
            yield indexer_pb2.IndexFileRequest(
                header=indexer_pb2.FileHeader(
                    filename=original_filename,
                    metadata={
                        "source": original_filepath or original_filename,
                        "md_path": md_path,
                        "processed_at": datetime.now().isoformat()
                    },
                    total_size=len(content.encode('utf-8'))
                )
            )
            # 2. Чанки
            chunk_size = 1024 * 64
            enc_content = content.encode('utf-8')
            for i in range(0, len(enc_content), chunk_size):
                yield indexer_pb2.IndexFileRequest(
                    chunk=enc_content[i : i + chunk_size]
                )

        logger.info(f"Начинаем вызов IndexDocument для файла: {md_path}")
        try:
            # Прямой вызов gRPC с таймаутом
            resp = self.idx_stub.IndexDocument(req_iterator(), timeout=self.cfg.get("indexer_timeout"))  # 2 минуты

            logger.info(f"IndexDocument завершен для файла: {md_path}, success: {resp.success}, message: {resp.message}")
            if resp.success:
                return True, "OK"
            return False, resp.message
        except grpc.RpcError as e:
            status_code = e.code().name if hasattr(e, 'code') else 'UNKNOWN'
            details = e.details() if hasattr(e, 'details') else str(e)
            logger.error(f"IndexDocument gRPC ошибка для файла {md_path}: {status_code} - {details}")

            if e.code() == grpc.StatusCode.UNAVAILABLE:
                return False, "Сервис индексации недоступен"
            elif e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                return False, "Таймаут при индексации файла"
            elif e.code() == grpc.StatusCode.INTERNAL:
                return False, f"Внутренняя ошибка индексации: {details}"
            else:
                return False, f"Ошибка индексации ({status_code}): {details}"
        except Exception as e:
            logger.error(f"Неожиданная ошибка при индексации файла {md_path}: {e}")
            return False, f"Unexpected error during indexing: {e}"

    def process_file(self, fid, fname, fpath):
        """
        Обработка одного файла.

        Выполняет последовательно:
        1. Конвертацию файла через converter
        2. Индексацию результата через indexer
        3. Обновление статуса файла в базе данных

        Args:
            fid (int): идентификатор файла
            fname (str): имя файла
            fpath (str): путь к файлу

        Returns:
            bool: True, если обработка прошла успешно, иначе False
        """
        logger.info(f"Начинаем обработку файла ID: {fid}, Name: {fname}, Path: {fpath}")
        # Шаг 1: Конвертер
        try:
            ok, res = self.convert(fpath)
        except Exception as e:
            logger.error(f"Критическая ошибка при конвертации файла {fpath}: {e}")
            error_msg = f"Критическая ошибка при конвертации: {str(e)}"
            logger.info(f"Устанавливаем статус 'failed' для файла ID: {fid}")
            update_result = self.fm.update_status(fid, 'failed', error_message=error_msg)
            if not update_result:
                logger.error(f"НЕУДАЧА: Не удалось обновить статус файла ID: {fid} на 'failed'")
            logger.info(f"Завершаем обработку файла ID: {fid} с критической ошибкой конвертации")
            return False

        if not ok:
            logger.error("Ошибка конвертации: %s", res)
            logger.info(f"Устанавливаем статус 'failed' для файла ID: {fid}")
            update_result = self.fm.update_status(fid, 'failed', error_message=res)
            if not update_result:
                logger.error(f"НЕУДАЧА: Не удалось обновить статус файла ID: {fid} на 'failed'")
            logger.info(f"Завершаем обработку файла ID: {fid} с ошибкой конвертации")
            return False

        md_file = res
        logger.info(f"Конвертация успешна, MD файл: {md_file}")

        # Шаг 2: Индексер
        try:
            ok_idx, res_idx = self.index(md_file, fname, fpath)
        except Exception as e:
            logger.error(f"Критическая ошибка при индексации файла {md_file}: {e}")
            error_msg_idx = f"Критическая ошибка при индексации: {str(e)}"
            # Сохраняем, что хотя бы сконвертировали, но добавляем сообщение об ошибке индексации
            logger.info(f"Устанавливаем статус 'conversion_success_only' для файла ID: {fid}")
            update_result = self.fm.update_status(fid, 'conversion_success_only', md_path=md_file, error_message=error_msg_idx)
            if not update_result:
                logger.error(f"НЕУДАЧА: Не удалось обновить статус файла ID: {fid} на 'conversion_success_only'")
            logger.info(f"Завершаем обработку файла ID: {fid} с критической ошибкой индексации")
            return False

        if not ok_idx:
            logger.error("Ошибка индексации: %s", res_idx)
            logger.info(f"Устанавливаем статус 'conversion_success_only' для файла ID: {fid}")
            update_result = self.fm.update_status(fid, 'conversion_success_only', md_path=md_file, error_message=res_idx)
            if not update_result:
                logger.error(f"НЕУДАЧА: Не удалось обновить статус файла ID: {fid} на 'conversion_success_only'")
            logger.info(f"Завершаем обработку файла ID: {fid} с ошибкой индексации")
            return False

        # Успех
        logger.info(f"Индексация успешна, устанавливаем статус 'indexed' для файла ID: {fid}")
        update_result = self.fm.update_status(fid, 'indexed', md_file)
        if not update_result:
            logger.error(f"НЕУДАЧА: Не удалось обновить статус файла ID: {fid} на 'indexed'")
        logger.info("Файл %s успешно обработан", fname)
        logger.info(f"Завершаем обработку файла ID: {fid} успешно")
        return True

    def worker(self):
        """
        Рабочий поток для обработки файлов.

        Получает задачи из очереди и выполняет обработку файлов.
        Обрабатывает различные типы исключений и корректно обновляет статусы файлов.
        Работает до тех пор, пока не получен сигнал остановки сервиса.
        """
        while self.running_event.is_set():
            try:
                # Получаем задачу из очереди
                task = self.processing_queue.get(timeout=self.cfg.get("queue_operation_timeout"))
                if task is None:  # Сигнал остановки
                    break

                fid, fname, fpath = task
                logger.info("\nОбработка: %s (ID: %s)", fname, fid)

                # Обрабатываем файл напрямую
                process_result = self.process_file(fid, fname, fpath)

                # process_file возвращает True при успешной обработке, False при ошибке
                if process_result:
                    logger.debug(f"Файл {fname} (ID: {fid}) успешно обработан")
                else:
                    logger.warning(f"Файл {fname} (ID: {fid}) не был успешно обработан")

                self.processing_queue.task_done()
            except Empty:
                continue  # Продолжаем ожидание задач
            except KeyboardInterrupt:
                logger.info("Worker received interrupt signal")
                break
            except grpc.RpcError as e:
                logger.error(f"gRPC error in worker: {e}")
                # Форматируем сообщение об ошибке gRPC, чтобы избежать длинных внутренних сообщений
                error_msg = str(e)
                if "_InactiveRpcError" in error_msg:
                    # Извлекаем только основную информацию об ошибке
                    if "details =" in error_msg:
                        start = error_msg.find("details =") + len("details =")
                        end = error_msg.find('"', start + 2)
                        if end > start:
                            details = error_msg[start:end].strip().strip('"\'')
                            if details:
                                error_msg = f"gRPC Internal Error - {details}"
                            else:
                                error_msg = "gRPC Internal Error - сервис недоступен"
                        else:
                            error_msg = "gRPC Internal Error - сервис недоступен"
                    else:
                        error_msg = "gRPC Internal Error - сервис недоступен"
                else:
                    error_msg = f'gRPC Error: {str(e)}'

                # Помечаем задачу как неудачную
                if task and isinstance(task, tuple) and len(task) == 3:
                    fid, fname, fpath = task
                    try:
                        update_result = self.fm.update_status(fid, 'failed', error_message=error_msg)
                        if not update_result:
                            logger.error(f"Не удалось обновить статус файла {fname} (ID: {fid}) после gRPC ошибки")
                    except Exception as update_error:
                        logger.error(f"Could not update file status after gRPC error: {update_error}")
                if task:
                    self.processing_queue.task_done()
            except FileNotFoundError as e:
                logger.error(f"File not found error: {e}")
                if task and isinstance(task, tuple) and len(task) == 3:
                    fid, fname, fpath = task
                    try:
                        update_result = self.fm.update_status(fid, 'failed', error_message=f'File not found: {e}')
                        if not update_result:
                            logger.error(f"Не удалось обновить статус файла {fname} (ID: {fid}) после ошибки FileNotFound")
                    except Exception as update_error:
                        logger.error(f"Could not update file status after file not found error: {update_error}")
                if task:
                    self.processing_queue.task_done()
            except Exception as e:
                logger.error(f"Unexpected error in worker: {e}", exc_info=True)
                # Помечаем файл как неудачно обработанный для других исключений
                if task and isinstance(task, tuple) and len(task) == 3:
                    fid, fname, fpath = task
                    try:
                        update_result = self.fm.update_status(fid, 'failed', error_message=f'Processing error: {str(e)}')
                        if not update_result:
                            logger.error(f"Не удалось обновить статус файла {fname} (ID: {fid}) после неожиданной ошибки")
                    except Exception as update_error:
                        logger.error(f"Could not update file status after unexpected error: {update_error}")
                if task:
                    self.processing_queue.task_done()

        logger.info("Worker thread shutting down")

    def run(self):
        """
        Основной цикл работы менеджера.

        Выполняет:
        1. Запуск рабочих потоков
        2. Периодическое сканирование директории
        3. Добавление новых задач в очередь
        4. Обработка сигналов остановки
        5. Управление жизненным циклом обработки файлов
        """
        logger.info("=== Менеджер запущен ===")
        self.running_event.set()

        # Сброс "зомби"-файлов, застрявших в processing с прошлого запуска
        try:
            with self.fm.get_connection() as conn:
                result = conn.execute(
                    "UPDATE files SET status = 'pending', updated_at = CURRENT_TIMESTAMP "
                    "WHERE status = 'processing'"
                )
                conn.commit()
                if result.rowcount > 0:
                    logger.info(
                        f"Сброшено {result.rowcount} файлов из 'processing' в 'pending' "
                        f"(зомби с прошлого запуска)"
                    )
        except Exception as e:
            logger.error(f"Ошибка сброса зомби-файлов: {e}")

        # Запускаем рабочие потоки
        num_workers = self.cfg.get("max_workers")
        for i in range(num_workers):
            t = threading.Thread(target=self.worker, daemon=True)
            t.start()
            self.worker_threads.append(t)

        scan_interval = self.cfg.get("scan_interval")  # Сохраняем значение в переменную

        while self.running_event.is_set():
            logger.debug("Начало итерации основного цикла")

            # 1. Сканируем
            try:
                logger.debug("Вызов scan_directory")
                self.fm.scan_directory()
                logger.debug("scan_directory завершен успешно")
            except Exception as e:
                logger.error("Ошибка сканирования: %s", e, exc_info=True)

            # 2. Добавляем задачи в очередь
            tasks_added = 0
            logger.debug("Начало добавления задач в очередь")
            
            # Не добавляем больше, чем есть свободных мест в очереди
            queue_free = self.processing_queue.maxsize - self.processing_queue.qsize()
            added_this_cycle = 0
            
            while self.running_event.is_set() and added_this_cycle < queue_free:
                try:
                    task = self.fm.get_pending_task()
                except Exception as e:
                    logger.error("Ошибка при получении задачи из базы данных: %s", e, exc_info=True)
                    # Делаем паузу перед следующей итерацией, чтобы не перегружать систему
                    time.sleep(1)
                    break  # Прерываем внутренний цикл и переходим к следующему сканированию

                if not task:
                    logger.debug("Нет задач для обработки")
                    break

                logger.debug(f"Получена задача: {task[0]} - {task[1]}")

                # Добавляем задачу в очередь
                try:
                    self.processing_queue.put(task, timeout=self.cfg.get("queue_operation_timeout"))  # Добавляем таймаут для избежания блокировки
                    tasks_added += 1
                    added_this_cycle += 1
                    logger.debug(f"Задача добавлена в очередь: {task[0]}")
                except Full:
                    # КРИТИЧНО: возвращаем файл обратно в pending, иначе он навсегда застрянет в processing
                    fid, fname, fpath = task
                    logger.warning(
                        f"Очередь переполнена, возвращаем файл ID:{fid} '{fname}' в pending. "
                        f"Уже добавлено задач: {tasks_added}"
                    )
                    try:
                        self.fm.update_status(fid, 'pending')
                    except Exception as e:
                        logger.error(f"Не удалось вернуть файл ID:{fid} в pending: {e}")
                    break

            if tasks_added > 0:
                logger.info(f"Добавлено {tasks_added} задач в очередь обработки")

            logger.debug("Конец итерации основного цикла")
            time.sleep(scan_interval)

        # Останавливаем рабочие потоки
        for i in range(num_workers):
            self.processing_queue.put(None)

# --- HTTP API для взаимодействия с веб-интерфейсом ---

# Создаем экземпляр менеджера
manager_instance = Manager()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

@app.route('/api/stats')
@handle_api_errors
def api_stats():
    """
    API endpoint для получения статистики.

    Возвращает информацию о количестве файлов, их статусах и времени последнего обновления.

    Returns:
        JSON: статистика по файлам
    """
    stats = manager_instance.fm.get_file_stats()
    return jsonify(stats)

@app.route('/api/files')
def api_files():
    """
    API endpoint для получения списка файлов в JSON формате.

    Поддерживает пагинацию и фильтрацию по статусу.
    Валидирует входные параметры и ограничивает максимальное смещение для предотвращения проблем с производительностью.

    Query Parameters:
        page (int): номер страницы (по умолчанию 1)
        limit (int): количество файлов на странице (по умолчанию 10, максимум 100)
        offset (int): смещение для пагинации
        status (str): фильтр по статусу файла

    Returns:
        JSON: список файлов с информацией и метаданными пагинации
    """
    try:
        # Валидация параметров
        try:
            page = int(request.args.get('page', 1))
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        try:
            limit = int(request.args.get('limit', 10))
            if limit < 1 or limit > 100:  # Устанавливаем разумное ограничение
                limit = 10
        except (ValueError, TypeError):
            limit = 10

        try:
            offset = int(request.args.get('offset', (page - 1) * limit))
            if offset < 0:
                offset = 0
            # Ограничиваем максимальное значение смещения для предотвращения проблем с производительностью
            if offset > 10000:
                offset = 10000
        except (ValueError, TypeError):
            offset = max(0, min((page - 1) * limit, 10000))

        # Валидация статуса
        status_filter = request.args.get('status', '')
        if status_filter and status_filter not in ['pending', 'indexed', 'failed', 'deleted', 'conversion_success_only']:
            status_filter = ''  # Сбросить фильтр если статус недопустимый

        files = manager_instance.fm.get_files(limit=limit, offset=offset, status_filter=status_filter)
        total_count = manager_instance.fm.get_total_files_count(status_filter)
        total_pages = (total_count + limit - 1) // limit

        return jsonify({
            'files': files,
            'pagination': {
                'page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'limit': limit,
                'offset': offset
            }
        })
    except Exception as e:
        logger.error(f"Ошибка в API файлов: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/status')
def api_status():
    """
    API endpoint для получения статуса сервиса.

    Возвращает информацию о том, запущен ли сервис в данный момент.

    Returns:
        JSON: статус сервиса
    """
    try:
        return jsonify({
            'running': manager_instance.is_running() if manager_instance else False
        })
    except Exception as e:
        logger.error(f"Ошибка в API статуса: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/config')
@handle_api_errors
def api_config():
    """
    API endpoint для получения конфигурации (без чувствительных данных).

    Возвращает только безопасные параметры конфигурации.

    Returns:
        JSON: безопасные параметры конфигурации
    """
    # Возвращаем только безопасные параметры конфигурации
    safe_config = {
        'files_dir': manager_instance.cfg.get('files_dir'),
        'scan_interval': manager_instance.cfg.get('scan_interval'),
        'api_port': manager_instance.cfg.get('api_port')
    }
    return jsonify(safe_config)

def api_start_manager():
    """
    API endpoint для запуска менеджера.

    Запускает основной цикл обработки файлов в отдельном потоке.

    Returns:
        JSON: результат операции
    """
    try:
        if not manager_instance.is_running():
            # Запускаем менеджер в отдельном потоке
            import threading
            thread = threading.Thread(target=manager_instance.run, daemon=True)
            thread.start()
            manager_instance.running_event.set()
            return jsonify({'success': True, 'message': 'Manager started'})
        else:
            return jsonify({'success': False, 'message': 'Manager already running'}), 400
    except Exception as e:
        logger.error(f"Ошибка при запуске менеджера: {e}")
        return jsonify({'error': str(e)}), 500

def api_stop_manager():
    """
    API endpoint для остановки менеджера.

    Останавливает основной цикл обработки файлов.

    Returns:
        JSON: результат операции
    """
    try:
        if manager_instance.is_running():
            manager_instance.running_event.clear()
            return jsonify({'success': True, 'message': 'Manager stopped'})
        else:
            return jsonify({'success': False, 'message': 'Manager already stopped'}), 400
    except Exception as e:
        logger.error(f"Ошибка при остановке менеджера: {e}")
        return jsonify({'error': str(e)}), 500

def api_retry_file_processing(file_id):
    """
    API endpoint для повторной попытки обработки файла.

    Args:
        file_id (int): идентификатор файла для повторной обработки

    Returns:
        JSON: результат операции
    """
    try:
        # Валидация file_id
        if file_id <= 0:
            return jsonify({'success': False, 'message': 'Invalid file ID'}), 400

        success = manager_instance.fm.retry_file_processing(file_id)
        if success:
            return jsonify({'success': True, 'message': f'File {file_id} marked for retry'})
        else:
            return jsonify({'success': False, 'message': f'File {file_id} not found'}), 404
    except Exception as e:
        logger.error(f"Ошибка при повторной обработке файла {file_id}: {e}")
        return jsonify({'error': str(e)}), 500

def api_delete_file(file_id):
    """
    API endpoint для удаления файла из базы данных.

    Фактически не удаляет файл, а помечает его как удаленный.

    Args:
        file_id (int): идентификатор файла для удаления

    Returns:
        JSON: результат операции
    """
    try:
        # Валидация file_id
        if file_id <= 0:
            return jsonify({'success': False, 'message': 'Invalid file ID'}), 400

        success = manager_instance.fm.delete_file(file_id)
        if success:
            return jsonify({'success': True, 'message': f'File {file_id} marked as deleted'})
        else:
            return jsonify({'success': False, 'message': f'File {file_id} not found'}), 404
    except Exception as e:
        logger.error(f"Ошибка при удалении файла {file_id}: {e}")
        return jsonify({'error': str(e)}), 500

def api_manual_scan():
    """
    API endpoint для ручного сканирования директории.

    Выполняет однократное сканирование директории на наличие новых файлов.

    Returns:
        JSON: результат операции и количество добавленных файлов
    """
    try:
        added_count = manager_instance.fm.scan_directory()
        return jsonify({'success': True, 'added_count': added_count})
    except Exception as e:
        logger.error(f"Ошибка при ручном сканировании: {e}")
        return jsonify({'error': str(e)}), 500

# Применяем аутентификацию к чувствительным эндпоинтам, используя токенную авторизацию
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
def protected_api_retry_file_processing(file_id, current_user=None):
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

# Для остальных эндпоинтов можно добавить опциональную аутентификацию или оставить без нее
# в зависимости от требований безопасности


@app.route('/api/subfolders')
@handle_api_errors
def api_subfolders():
    """Список подпапок в files_dir."""
    subfolders = manager_instance.fm.list_subfolders()
    return jsonify({
        'subfolders': subfolders,
        'files_dir': str(manager_instance.fm.files_dir)
    })


@app.route('/api/create_subfolder', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def api_create_subfolder(current_user=None):
    """Создание подпапки в files_dir."""
    try:
        data = request.get_json()
        if not data or not data.get('subfolder', '').strip():
            return jsonify({'error': 'Не указано имя подпапки'}), 400

        subfolder_name = data['subfolder'].strip()
        clean_path = Path(subfolder_name)
        if '..' in clean_path.parts:
            return jsonify({'error': 'Недопустимый путь'}), 400

        target = Path(manager_instance.fm.files_dir).resolve() / clean_path
        # Двойная проверка: и parts, и startswith
        if not str(target).startswith(str(Path(manager_instance.fm.files_dir).resolve())):
            return jsonify({'error': 'Путь выходит за пределы разрешённой директории'}), 400

        target.mkdir(parents=True, exist_ok=True)
        logger.info(f"[{current_user['username']}] Создана подпапка: {subfolder_name}")
        return jsonify({'success': True, 'message': f'Подпапка создана: {subfolder_name}'})
    except Exception as e:
        logger.error(f"Ошибка при создании подпапки: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_batch', methods=['POST'])
@require_role('admin', 'user', secret_key=AUTH_SECRET_KEY)
def api_upload_batch(current_user=None):
    """
    Пакетная загрузка файлов (папками).
    Принимает multipart с files[] и JSON-массивом relative_paths.
    """
    files = request.files.getlist('files[]')
    if not files:
        return jsonify({'error': 'Файлы не переданы'}), 400

    # relative_paths — массив путей из webkitRelativePath
    relative_paths_json = request.form.get('relative_paths', '[]')
    try:
        relative_paths = json.loads(relative_paths_json)
    except (json.JSONDecodeError, TypeError):
        relative_paths = []

    subfolder = request.form.get('subfolder', '').strip()
    if subfolder in ('null', 'undefined', 'None'):
        subfolder = ''

    allowed_extensions = set(
        manager_instance.cfg.get('allowed_extensions') or
        ['.pdf', '.docx', '.pptx', '.txt', '.html', '.htm', '.tif', '.tiff']
    )
    max_size = (manager_instance.cfg.get('max_upload_size_mb') or 100) * 1024 * 1024
    files_dir = Path(manager_instance.fm.files_dir).resolve()

    # Базовая целевая директория
    if subfolder:
        clean_sub = Path(subfolder)
        if '..' in clean_sub.parts:
            return jsonify({'error': 'Недопустимый путь подпапки'}), 400
        base_target = files_dir / clean_sub
    else:
        base_target = files_dir

    base_target.mkdir(parents=True, exist_ok=True)

    result = {'success': True, 'uploaded': 0, 'skipped': 0, 'errors': 0, 'details': []}

    for idx, file in enumerate(files):
        original_name = file.filename
        if not original_name:
            result['skipped'] += 1
            continue

        ext = os.path.splitext(original_name)[1].lower()
        if ext not in allowed_extensions:
            result['skipped'] += 1
            result['details'].append({
                'filename': original_name,
                'status': 'skipped',
                'reason': f'Неподдерживаемый формат: {ext}'
            })
            continue

        # Проверка размера
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        if file_size > max_size:
            result['skipped'] += 1
            result['details'].append({
                'filename': original_name,
                'status': 'skipped',
                'reason': f'Файл слишком большой: {file_size // (1024*1024)} МБ'
            })
            continue

        try:
            # Воссоздаём структуру подпапок из relative_paths
            if idx < len(relative_paths) and relative_paths[idx]:
                rel_parts = Path(relative_paths[idx])
                if '..' in rel_parts.parts:
                    raise ValueError('Недопустимый путь')
                # relative_paths[idx] = "FolderName/sub/file.pdf"
                # parts[:-1] = путь без имени файла
                if len(rel_parts.parts) > 1:
                    sub_structure = Path(*rel_parts.parts[:-1])
                    target_dir = base_target / sub_structure
                else:
                    target_dir = base_target
            else:
                target_dir = base_target

            target_dir.mkdir(parents=True, exist_ok=True)

            clean_name = safe_filename(original_name)
            if not clean_name or clean_name == 'unnamed':
                ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                clean_name = f"file_{ts}{ext}"

            dest_path = target_dir / clean_name

            # Конфликт имён — добавляем timestamp с микросекундами
            if dest_path.exists():
                stem = dest_path.stem
                suffix = dest_path.suffix
                ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                clean_name = f"{stem}_{ts}{suffix}"
                dest_path = target_dir / clean_name

            # Финальная проверка что путь внутри files_dir
            if not str(dest_path.resolve()).startswith(str(files_dir)):
                raise ValueError('Путь выходит за пределы разрешённой директории')

            file.save(str(dest_path))

            registered = manager_instance.fm.register_uploaded_file(
                clean_name, str(dest_path.resolve())
            )
            result['uploaded'] += 1
            result['details'].append({
                'filename': clean_name,
                'path': str(dest_path),
                'status': 'uploaded',
                'registered': registered
            })
        except Exception as e:
            result['errors'] += 1
            result['details'].append({
                'filename': original_name,
                'status': 'error',
                'reason': str(e)
            })

    logger.info(
        f"[{current_user['username']}] Пакетная загрузка: "
        f"загружено={result['uploaded']}, пропущено={result['skipped']}, ошибок={result['errors']}"
    )
    return jsonify(result)


@app.route('/api/import_folder', methods=['POST'])
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def api_import_folder(current_user=None):
    """Импорт файлов из папки на сервере (рекурсивно)."""
    data = request.get_json()
    if not data or not data.get('source_dir', '').strip():
        return jsonify({'error': 'Не указан путь к папке'}), 400

    source_dir = data['source_dir'].strip()
    target_subfolder = data.get('target_subfolder', '').strip()
    if target_subfolder in ('null', 'undefined', 'None'):
        target_subfolder = ''

    try:
        result = manager_instance.fm.import_from_folder(source_dir, target_subfolder)
        logger.info(
            f"[{current_user['username']}] Импорт из {source_dir}: "
            f"скопировано={result['copied']}, пропущено={result['skipped']}, ошибок={result['errors']}"
        )
        return jsonify({
            'success': True,
            'result': result,
            'message': f"Скопировано: {result['copied']}, пропущено: {result['skipped']}, ошибок: {result['errors']}"
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/browse_server_folder')
@require_role('admin', secret_key=AUTH_SECRET_KEY)
def api_browse_server_folder(current_user=None):
    """
    Просмотр серверной папки для выбора источника импорта.
    Только для admin — позволяет видеть файловую систему сервера.
    """
    folder_path = request.args.get('path', '/').strip()
    supported = {'.pdf', '.docx', '.pptx', '.txt', '.html', '.htm', '.tif', '.tiff'}

    try:
        target = Path(folder_path).resolve()
        if not target.exists() or not target.is_dir():
            return jsonify({'error': 'Папка не найдена'}), 404

        items = {
            'current_path': str(target),
            'parent_path': str(target.parent) if str(target) != '/' else None,
            'folders': [],
            'files': [],
            'supported_count': 0
        }

        try:
            for item in sorted(target.iterdir()):
                if item.name.startswith('.'):
                    continue

                if item.is_dir():
                    # Считаем файлы рекурсивно (rglob), не только первый уровень
                    try:
                        file_count = sum(
                            1 for f in item.rglob('*')
                            if f.is_file() and f.suffix.lower() in supported
                        )
                    except PermissionError:
                        file_count = -1

                    items['folders'].append({
                        'name': item.name,
                        'path': str(item),
                        'file_count': file_count
                    })
                elif item.is_file() and item.suffix.lower() in supported:
                    items['files'].append({
                        'name': item.name,
                        'size': item.stat().st_size,
                        'modified': item.stat().st_mtime
                    })
                    items['supported_count'] += 1
        except PermissionError:
            return jsonify({'error': f'Нет доступа к папке: {folder_path}'}), 403

        return jsonify(items)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Добавляем эндпоинт проверки работоспособности
@app.route('/health')
@handle_api_errors
def health_check():
    """
    Health check endpoint.

    Проверяет работоспособность сервиса, включая подключение к базе данных.
    Используется для мониторинга и проверки готовности сервиса к работе.

    Returns:
        JSON: статус работоспособности сервиса
    """
    # Проверяем подключение к базе данных
    with manager_instance.fm.get_connection() as conn:
        conn.execute("SELECT 1").fetchone()

    return jsonify({
        'status': 'healthy',
        'running': manager_instance.is_running(),
        'timestamp': datetime.now().isoformat()
    })


@app.route('/metrics')
@handle_api_errors
def metrics():
    """
    Metrics endpoint for monitoring.

    Возвращает метрики для мониторинга производительности и состояния сервиса.
    Используется для сбора данных о работе сервиса и диагностики проблем.

    Returns:
        JSON: метрики сервиса
    """
    stats = manager_instance.fm.get_file_stats()
    return jsonify({
        'queue_size': manager_instance.processing_queue.qsize(),
        'worker_threads': len(manager_instance.worker_threads),
        'is_running': manager_instance.is_running(),
        'file_stats': stats
    })


if __name__ == "__main__":
    # Присваиваем глобальный экземпляр менеджера в приложение
    app.manager_instance = manager_instance

    # Запускаем основной цикл менеджера в отдельном потоке
    def run_manager():
        logger.info("Запуск основного цикла менеджера в фоновом потоке")
        try:
            manager_instance.run()
            logger.info("Основной цикл менеджера завершен")
        except Exception as e:
            logger.error(f"Ошибка в основном цикле менеджера: {e}", exc_info=True)

    manager_thread = threading.Thread(target=run_manager, daemon=True)
    manager_thread.start()

    print("Основной цикл менеджера запущен в фоновом режиме")

    # Запускаем HTTP API сервер
    api_port = manager_instance.cfg.get("api_port")
    logger.info(f"=== Запуск HTTP API на порту {api_port} ===")
    app.run(host='0.0.0.0', port=api_port, debug=False, use_reloader=False, threaded=True)