"""
Микросервис индексации документов в векторной базе данных Qdrant.

Сервис принимает документы через gRPC streaming, разбивает их на чанки с помощью
внешнего сервиса chunker, генерирует эмбеддинги для чанков с помощью сервиса
embedder, и сохраняет результаты в векторной базе данных Qdrant.

Безопасность:
- Проверка размера содержимого документа (max_content_size)
- Проверка длины имени файла (max_filename_length)
- Проверка количества метаданных (max_metadata_count)
- Таймауты для внешних вызовов (embedder_timeout)
- Ограничение на количество чанков на документ (max_chunks_per_doc)
"""

import grpc
import grpc.aio
import logging
import sys
import os
import json
import asyncio
import uuid
import time
import threading
import math
import re
import hashlib
import signal
from collections import defaultdict, Counter

# Загрузка переменных окружения из .env файла
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Если python-dotenv не установлен, продолжаем без него
    pass

# Импорт сгенерированных gRPC-классов
import chunker_pb2
import chunker_pb2_grpc
import embedder_pb2
import embedder_pb2_grpc
import indexer_pb2
import indexer_pb2_grpc

# Импорт Qdrant для хранения векторов
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

# Простая настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Загрузка конфигурации из файла
def load_config():
    """Загружает и валидирует конфигурацию из файла config.json

    Загружает конфигурацию из файла config.json и выполняет валидацию всех
    обязательных и опциональных параметров. Поддерживает валидацию параметров
    безопасности, обработки и подключения к внешним сервисам.

    Конфигурация включает:
    - chunker_service: настройки подключения к сервису чанкинга
    - embedder_service: настройки подключения к сервису эмбеддинга
    - qdrant: настройки подключения к векторной базе данных
    - server: настройки gRPC сервера
    - chunk_size: размер чанка при разбиении документов
    - chunk_overlap: размер перекрытия между чанками
    - security: параметры безопасности (ограничения на размеры и количества)
    - processing: параметры обработки (таймауты, ограничения)

    Returns:
        dict: Словарь с параметрами конфигурации

    Raises:
        FileNotFoundError: Если файл config.json не найден
        ValueError: Если конфигурация содержит недопустимые значения
    """
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError("Файл конфигурации config.json не найден")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # Валидация обязательных полей
    required_fields = ['chunker_service', 'embedder_service', 'qdrant', 'server', 'chunk_size', 'chunk_overlap']
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Отсутствует обязательное поле конфигурации: {field}")

    # Проверка вложенных обязательных полей
    required_subfields = {
        'chunker_service': ['host', 'port'],
        'embedder_service': ['host', 'port'],
        'qdrant': ['host', 'port', 'collection_name', 'vector_size'],
        'server': ['host', 'port', 'max_workers']
    }

    for section, fields in required_subfields.items():
        if section in config:
            for field in fields:
                if field not in config[section]:
                    raise ValueError(f"Отсутствует обязательное поле '{field}' в секции '{section}'")

    # Валидация опциональных полей безопасности
    security_config = config.get('security', {})
    if not isinstance(security_config, dict):
        raise ValueError("Поле 'security' должно быть объектом")

    # Проверка числовых значений безопасности
    if 'max_content_size' in security_config and not isinstance(security_config['max_content_size'], int):
        raise ValueError("Поле 'security.max_content_size' должно быть целым числом")
    if 'max_filename_length' in security_config and not isinstance(security_config['max_filename_length'], int):
        raise ValueError("Поле 'security.max_filename_length' должно быть целым числом")
    if 'max_metadata_count' in security_config and not isinstance(security_config['max_metadata_count'], int):
        raise ValueError("Поле 'security.max_metadata_count' должно быть целым числом")

    # Валидация опциональных полей обработки
    processing_config = config.get('processing', {})
    if not isinstance(processing_config, dict):
        raise ValueError("Поле 'processing' должно быть объектом")

    if 'embedder_timeout' in processing_config and not isinstance(processing_config['embedder_timeout'], (int, float)):
        raise ValueError("Поле 'processing.embedder_timeout' должно быть числом")
    if 'max_chunks_per_doc' in processing_config and not isinstance(processing_config['max_chunks_per_doc'], int):
        raise ValueError("Поле 'processing.max_chunks_per_doc' должно быть целым числом")

    return config

# Загрузка конфигурации будет выполнена при первом обращении
# Потокобезопасный синглтон с двойной проверкой блокировки
_config_lock = threading.Lock()
_config_instance = None

def get_config():
    """Возвращает экземпляр конфигурации, загружая его при необходимости.

    Потокобезопасная реализация с использованием двойной проверки блокировки
    (double-checked locking pattern) для предотвращения гонки при max_workers > 1.
    """
    global _config_instance
    if _config_instance is None:
        with _config_lock:
            if _config_instance is None:
                _config_instance = load_config()
    return _config_instance


# ─── BM25 Sparse Vector Encoder ───────────────────────────────

_STOP_WORDS = {
    'и', 'в', 'на', 'с', 'по', 'для', 'из', 'к', 'от', 'за', 'о', 'об',
    'до', 'не', 'но', 'а', 'что', 'как', 'это', 'все', 'он', 'она', 'они',
    'мы', 'вы', 'я', 'ты', 'при', 'так', 'же', 'бы', 'ли', 'уже', 'был',
    'была', 'было', 'были', 'быть', 'если', 'его', 'её', 'их', 'или',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'have',
    'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
    'of', 'in', 'to', 'for', 'with', 'on', 'at', 'from', 'by', 'as',
    'or', 'and', 'but', 'if', 'not', 'no', 'this', 'that', 'it',
}
_TOKEN_RE = re.compile(r'[a-zA-Zа-яА-ЯёЁ0-9]+', re.UNICODE)


def _stable_hash(term: str) -> int:
    """Детерминистичный хеш терма, одинаковый между процессами.

    Использует MD5 для получения стабильного хеша, который не зависит
    от PYTHONHASHSEED и одинаков в разных процессах Python.
    """
    return int(hashlib.md5(term.encode('utf-8')).hexdigest(), 16)


def tokenize(text: str) -> list:
    """Простая токенизация: lowercase + split + стоп-слова."""
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


class BM25SparseEncoder:
    """Вычисляет BM25 sparse vectors для хранения в Qdrant.

    Термы кодируются в числовые индексы через стабильный хеш (hash(term) % vocab_size).
    Значения — BM25-подобные TF-веса. IDF применяется при поиске на стороне Searcher'а
    или приближённо при индексации.

    При индексации используется упрощённая формула без IDF:
        weight = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

    Это корректный TF-компонент BM25. IDF-компонент не нужен для sparse vector search,
    потому что Qdrant при поиске по sparse vectors считает dot product между
    query sparse vector и stored sparse vector — IDF можно закодировать в query vector.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, vocab_size: int = 50000,
                 stats_path: str = 'bm25_stats.json'):
        self.k1 = k1
        self.b = b
        self.vocab_size = vocab_size
        self.stats_path = stats_path

        # Статистика корпуса для IDF (накапливается при индексации)
        self.doc_count = 0
        self.doc_freqs = defaultdict(int)
        self.total_length = 0
        
        # Загружаем сохранённую статистику
        self._load_stats()

    def _load_stats(self):
        """Загружает статистику BM25 из файла при старте."""
        if os.path.exists(self.stats_path):
            try:
                with open(self.stats_path, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
                self.doc_freqs = defaultdict(int, stats.get('doc_freqs', {}))
                self.doc_count = stats.get('doc_count', 0)
                self.total_length = stats.get('total_length', 0)
                logger.info(f"Загружена статистика BM25: {self.doc_count} документов, "
                           f"{len(self.doc_freqs)} уникальных терминов")
            except Exception as e:
                logger.warning(f"Не удалось загрузить статистику BM25: {e}")

    def save_stats(self):
        """Сохраняет статистику BM25 в файл."""
        try:
            with open(self.stats_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'doc_freqs': dict(self.doc_freqs),
                    'doc_count': self.doc_count,
                    'total_length': self.total_length
                }, f, ensure_ascii=False)
            logger.info(f"Сохранена статистика BM25: {self.doc_count} документов")
        except Exception as e:
            logger.error(f"Не удалось сохранить статистику BM25: {e}")

    def _term_to_index(self, term: str) -> int:
        """Стабильное отображение терма в числовой индекс."""
        return _stable_hash(term) % self.vocab_size

    @property
    def avg_doc_length(self) -> float:
        if self.doc_count == 0:
            return 1.0
        return self.total_length / self.doc_count

    def encode_document(self, text: str) -> dict:
        """Кодирует текст документа в sparse vector для Qdrant.

        Returns:
            dict с ключами 'indices' (list[int]) и 'values' (list[float])
            или None, если текст пуст после токенизации.
        """
        tokens = tokenize(text)
        if not tokens:
            return None

        self.doc_count += 1
        self.total_length += len(tokens)

        tf_counts = Counter(tokens)
        dl = len(tokens)
        avgdl = self.avg_doc_length

        # Используем dict для агрегации весов по уникальным индексам
        # (на случай коллизий хеша разных терминов в один индекс)
        index_weights = {}

        for term, tf in tf_counts.items():
            term_idx = self._term_to_index(term)

            # Вычисляем BM25 вес для этого термина
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * dl / avgdl)
            weight = numerator / denominator

            # Если индекс уже существует (коллизия), суммируем веса
            if term_idx in index_weights:
                index_weights[term_idx] += weight
            else:
                index_weights[term_idx] = weight
                # Обновляем doc_freq только для новых уникальных индексов
                self.doc_freqs[term_idx] += 1

        # Преобразуем в списки indices/values
        indices = list(index_weights.keys())
        values = list(index_weights.values())

        return {"indices": indices, "values": values}


class IndexerService(indexer_pb2_grpc.IndexerServiceServicer):
    """gRPC сервис для индексации документов в векторной базе данных Qdrant.

    Сервис принимает документы через gRPC streaming, разбивает их на чанки с помощью
    внешнего сервиса chunker, генерирует эмбеддинги для чанков с помощью сервиса
    embedder, и сохраняет результаты в векторной базе данных Qdrant.
    """

    def __init__(self):
        """Инициализирует сервис индексации с подключением к зависимым сервисам.

        Создает gRPC-клиенты для сервисов chunker и embedder, а также асинхронный
        клиент для взаимодействия с векторной базой данных Qdrant.
        """
        config = get_config()
        self.chunker_channel = self._make_channel(
            config['chunker_service']['host'],
            config['chunker_service']['port']
        )
        self.chunker_client = chunker_pb2_grpc.ChunkerServiceStub(self.chunker_channel)

        self.embedder_channel = self._make_channel(
            config['embedder_service']['host'],
            config['embedder_service']['port']
        )
        self.embedder_client = embedder_pb2_grpc.EmbedderServiceStub(self.embedder_channel)

        self.qdrant_client = AsyncQdrantClient(
            host=config['qdrant']['host'],
            port=config['qdrant']['port']
        )
        self.collection_name = config['qdrant']['collection_name']

        # ── BM25 Sparse Encoder ──
        bm25_cfg = config.get('bm25', {})
        self.enable_sparse = config['qdrant'].get('enable_sparse', False)
        self.sparse_vector_name = config['qdrant'].get('sparse_vector_name', 'bm25_sparse')
        if self.enable_sparse:
            self.sparse_encoder = BM25SparseEncoder(
                k1=bm25_cfg.get('k1', 1.5),
                b=bm25_cfg.get('b', 0.75),
            )
            logger.info("BM25 sparse encoder инициализирован")
        else:
            self.sparse_encoder = None

    def _make_channel(self, host, port):
        """Создает асинхронный gRPC канал с поддержкой больших сообщений.

        Настройки канала позволяют передавать большие объемы данных между
        микросервисами, что необходимо для обработки крупных документов.

        Args:
            host (str): Хост для подключения
            port (int): Порт для подключения

        Returns:
            grpc.aio.Channel: Асинхронный gRPC канал с настройками для больших сообщений
        """
        addr = f"{host}:{port}"
        return grpc.aio.insecure_channel(addr, options=[
            ('grpc.max_send_message_length', 100 * 1024 * 1024),
            ('grpc.max_receive_message_length', 100 * 1024 * 1024),
        ])

    async def init_db(self):
        """Асинхронно проверяет существование коллекции и создает её при необходимости.

        Метод проверяет, существует ли коллекция в Qdrant, и если нет - создает новую
        с указанными в конфигурации параметрами. Коллекция использует косинусное расстояние
        для вычисления схожести векторов.
        """
        config = get_config()
        vector_size = config['qdrant']['vector_size']
        sparse_name = config['qdrant'].get('sparse_vector_name', 'bm25_sparse')
        enable_sparse = config['qdrant'].get('enable_sparse', False)

        if not await self.qdrant_client.collection_exists(self.collection_name):
            # Конфигурация sparse vectors (если включено)
            sparse_config = None
            if enable_sparse:
                sparse_config = {
                    sparse_name: models.SparseVectorParams(
                        modifier=models.Modifier.IDF
                    )
                }

            await self.qdrant_client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
                sparse_vectors_config=sparse_config,
            )

            msg = f"Создана коллекция {self.collection_name} (dense={vector_size}"
            if enable_sparse:
                msg += f", sparse={sparse_name}"
            msg += ")"
            logger.info(msg)
        else:
            logger.info(f"Коллекция {self.collection_name} уже существует")

    async def collect_document_data(self, request_iterator):
        """Асинхронно собирает данные документа из потока запросов с валидацией.

        Метод обрабатывает gRPC streaming запросы, извлекая заголовок с метаданными
        и чанки содержимого документа. Выполняет проверки безопасности на основе
        параметров из конфигурации.

        Args:
            request_iterator: Асинхронный итератор запросов gRPC

        Returns:
            tuple: Кортеж из (содержимое_документа, метаданные, имя_файла)

        Raises:
            ValueError: Если документ превышает ограничения безопасности
        """
        content_parts = []
        metadata = {}
        filename = "unknown"

        # Ограничения для безопасности из конфигурации
        config = get_config()
        max_content_size = config.get('security', {}).get('max_content_size', 10 * 1024 * 1024)
        max_filename_length = config.get('security', {}).get('max_filename_length', 255)
        max_metadata_count = config.get('security', {}).get('max_metadata_count', 50)

        total_size = 0

        async for request in request_iterator:
            if request.HasField('header'):
                # Извлечь метаданные из заголовка
                header = request.header

                # Проверка длины имени файла для предотвращения атак с длинными именами
                if len(header.filename) > max_filename_length:
                    raise ValueError("Слишком длинное имя файла")

                # Извлечение метаданных из заголовка
                metadata = dict(header.metadata) if header.metadata else {}

                # Проверка количества метаданных для предотвращения атак с избыточными метаданными
                if len(metadata) > max_metadata_count:
                    raise ValueError("Слишком много метаданных")

                filename = header.filename
            elif request.HasField('chunk'):
                # Собрать чанки содержимого
                chunk_data = request.chunk

                # Проверка общего размера документа для предотвращения атак с большими документами
                total_size += len(chunk_data)
                if total_size > max_content_size:
                    raise ValueError("Содержимое документа слишком велико")

                content_parts.append(chunk_data)

        # Объединить содержимое
        content = b''.join(content_parts)

        # Декодировать байты в строку UTF-8
        content_str = content.decode('utf-8')

        return content_str, metadata, filename


    async def IndexDocument(self, request_iterator, context):
        """Асинхронный gRPC метод для индексации документа через streaming.

        Метод принимает документ в виде потока запросов, разбивает его на чанки,
        генерирует эмбеддинги для каждого чанка и сохраняет их в векторной базе данных Qdrant.
        Реализует комплексную обработку документа с проверками безопасности и обработкой ошибок.

        Args:
            request_iterator: Асинхронный итератор запросов с данными документа
            context: Контекст gRPC вызова

        Returns:
            indexer_pb2.IndexResponse: Результат индексации с информацией об успехе/неудаче
                                       и количестве обработанных чанков
        """
        filename = "unknown"
        try:
            # Собрать данные документа
            content_str, metadata, filename = await self.collect_document_data(request_iterator)

            # Создать идентификатор документа
            doc_identifier = metadata.get('doc_id') or filename or 'unknown_doc'

            # Отправить документ в сервис чанкинга
            logger.info(f"Отправка документа '{filename}' в сервис чанкинга")

            # Создать запрос к сервису чанкинга
            config = get_config()
            chunk_request = chunker_pb2.ChunkRequest(
                content=content_str,
                chunk_size=config['chunk_size'],  # Размер чанка из конфигурации
                overlap=config['chunk_overlap']    # Перекрытие из конфигурации
            )

            # Получить таймаут из конфигурации заранее, до цикла обработки чанков
            embedder_timeout = config.get('processing', {}).get('embedder_timeout', 30.0)

            # Получить чанки из сервиса и обработать их
            try:
                chunk_stream = self.chunker_client.ChunkIt(chunk_request)
            except grpc.aio.AioRpcError as e:
                logger.error(f"Ошибка при получении чанков: {e.code()}: {e.details()}")
                return indexer_pb2.IndexResponse(success=False, message=f"Ошибка сервиса чанкинга: {e.details()}")

            total_chunks_processed = 0

            # Обработать чанки последовательно
            async for chunk in chunk_stream:
                # Получить эмбеддинг для чанка с таймаутом
                text_chunks = embedder_pb2.TextChunks(texts=[chunk.text])

                try:
                    response = await asyncio.wait_for(
                        self.embedder_client.GetEmbeddings(text_chunks),
                        timeout=embedder_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут при получении эмбеддинга для чанка: {chunk.text[:100]}...")
                    continue
                except grpc.aio.AioRpcError as e:
                    logger.error(f"Ошибка RPC при получении эмбеддинга: {e.code()}: {e.details()}")
                    continue

                # Проверить, были ли возвращены векторы
                if not response.vectors or len(response.vectors) == 0:
                    logger.warning(f"Не удалось получить векторы для чанка: {chunk.text[:100]}...")
                    continue

                # Извлечь вектор эмбеддинга
                embedding = list(response.vectors[0].values)
                if not embedding:
                    logger.warning(f"Вектор эмбеддинга пуст для чанка: {chunk.text[:100]}...")
                    continue

                # Создать уникальный ID для чанка
                # Используется UUID5 с пространством имен DNS для детерминированного генерирования ID
                # на основе идентификатора документа и позиций начала/конца чанка
                chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_identifier}_{chunk.start}_{chunk.end}"))

                # Создать метаданные для чанка
                # Метаданные от chunker (исключаем source, чтобы не затереть путь к файлу)
                chunker_meta = {k: v for k, v in dict(chunk.metadata).items() if k != "source"}

                chunk_metadata = {
                    "text": chunk.text,
                    "doc_id": doc_identifier,
                    **metadata,
                    **chunker_meta
                }

                # Создать точку для Qdrant
                # Точка содержит уникальный ID, вектор эмбеддинга и метаданные
                vectors = embedding  # dense vector (по умолчанию)

                # Если включён sparse — добавляем BM25 sparse vector
                if self.enable_sparse and self.sparse_encoder:
                    sparse_data = self.sparse_encoder.encode_document(chunk.text)
                    if sparse_data:
                        vectors = {
                            "": embedding,  # dense vector (безымянный = основной)
                            self.sparse_vector_name: models.SparseVector(
                                indices=sparse_data["indices"],
                                values=sparse_data["values"],
                            )
                        }

                point = models.PointStruct(
                    id=chunk_id,
                    vector=vectors,
                    payload=chunk_metadata
                )

                # Добавить в Qdrant с помощью upsert операции
                # Upsert обновляет точку если она существует, или создает новую если не существует
                await self.qdrant_client.upsert(
                    collection_name=self.collection_name,
                    points=[point]
                )

                total_chunks_processed += 1

            logger.info(f"Документ {filename} успешно проиндексирован, обработано {total_chunks_processed} чанков")
            return indexer_pb2.IndexResponse(
                success=True,
                message=f"Документ {filename} успешно проиндексирован",
                chunks_processed=total_chunks_processed
            )

        except grpc.RpcError as e:
            logger.error(f"gRPC ошибка: {e.code()}: {e.details()}")
            return indexer_pb2.IndexResponse(success=False, message=f"gRPC ошибка: {e.details()}")
        except ValueError as e:
            logger.error(f"Ошибка валидации: {str(e)}")
            return indexer_pb2.IndexResponse(success=False, message=f"Ошибка валидации: {str(e)}")
        except Exception as e:
            logger.exception("Неожиданная ошибка")
            return indexer_pb2.IndexResponse(success=False, message=f"Неожиданная ошибка: {e}")

    async def close(self):
        """Асинхронно закрывает все активные соединения с внешними сервисами.

        Метод обеспечивает корректное завершение работы всех gRPC каналов и
        клиента Qdrant, освобождая сетевые ресурсы.
        """
        # Сохраняем статистику BM25 перед закрытием
        if self.sparse_encoder:
            self.sparse_encoder.save_stats()

        try:
            await self.chunker_channel.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии канала chunker: {e}")

        try:
            await self.embedder_channel.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии канала embedder: {e}")

        try:
            await self.qdrant_client.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии клиента Qdrant: {e}")


async def serve():
    """Асинхронно запускает gRPC сервер индексации с инициализацией базы данных.

    Функция создает асинхронный gRPC сервер, регистрирует сервис индексации,
    инициализирует векторную базу данных Qdrant и начинает прослушивание
    входящих запросов. При завершении корректно закрывает все соединения.
    """
    # Создать асинхронный gRPC сервер
    config = get_config()
    server = grpc.aio.server(options=[
        ('grpc.max_workers', config['server']['max_workers'])
    ])

    # Создать и добавить асинхронный сервис индексации к серверу
    indexer_service = IndexerService()

    # Инициализировать БД до начала приема запросов
    await indexer_service.init_db()

    indexer_pb2_grpc.add_IndexerServiceServicer_to_server(indexer_service, server)

    # Адрес сервера
    host = config['server']['host']
    port = config['server']['port']
    server_address = f"{host}:{port}"

    # Добавить небезопасный порт (без TLS)
    server.add_insecure_port(server_address)

    logger.info(f"Запуск асинхронного сервиса индексации на {server_address}")
    await server.start()
    logger.info("Сервер индексации запущен. Ожидание запросов...")

    # Обработчик сигналов для сохранения статистики при Ctrl+C
    def signal_handler(sig, frame):
        logger.info(f"Получен сигнал {sig}, сохранение статистики...")
        if indexer_service.sparse_encoder:
            indexer_service.sparse_encoder.save_stats()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await server.wait_for_termination()
    finally:
        # Гарантированное закрытие соединений и сохранение статистики
        await indexer_service.close()
        await server.stop(grace=5)

if __name__ == '__main__':
    # Запускаем асинхронный сервер
    asyncio.run(serve())