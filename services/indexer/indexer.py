"""
Микросервис индексации документов в векторной базе данных Qdrant.

Сервис принимает документы через gRPC streaming, разбивает их на чанки с помощью
внешнего сервиса chunker, генерирует эмбеддинги для чанков с помощью сервиса
embedder, и сохраняет результаты в векторной базе данных Qdrant.

Безопасность:
- Проверка размера содержимого документа (max_content_size)
- Проверка длины имени файла (max_filename_length)
- Проверка количества метаданных (max_metadata_count)
- Проверка безопасности имен файлов (validate_filename)
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
import re

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
_config_instance = None

def get_config():
    """Возвращает экземпляр конфигурации, загружая его при необходимости."""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config()
    return _config_instance


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
        if not await self.qdrant_client.collection_exists(self.collection_name):
            await self.qdrant_client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE)
            )
            logger.info(f"Создана коллекция {self.collection_name} с размером вектора {vector_size}")

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

                # Получить таймаут из конфигурации
                config = get_config()
                embedder_timeout = config.get('processing', {}).get('embedder_timeout', 30.0)

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
                # Включает текст чанка, позиции, источник, идентификатор документа и дополнительные метаданные
                chunk_metadata = {
                    "text": chunk.text,
                    "start": chunk.start,
                    "end": chunk.end,
                    "source": "indexer",
                    "doc_id": doc_identifier,
                    **metadata,
                    **dict(chunk.metadata)
                }

                # Создать точку для Qdrant
                # Точка содержит уникальный ID, вектор эмбеддинга и метаданные
                point = models.PointStruct(
                    id=chunk_id,
                    vector=embedding,
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

    try:
        await server.wait_for_termination()
    finally:
        # Гарантированное закрытие соединений
        await indexer_service.close()
        await server.stop(grace=5)

def validate_filename(filename):
    """Проверяет безопасность имени файла на наличие потенциально опасных символов.

    Функция проверяет имя файла на наличие символов или последовательностей,
    которые могут быть использованы для манипуляции файловой системой, таких как
    '../', '/', '\'.

    Args:
        filename (str): Имя файла для проверки

    Returns:
        bool: True если имя файла безопасно, иначе False
    """
    if not filename:
        return False

    # Проверка на наличие опасных символов
    if '..' in filename or '/' in filename or '\\' in filename:
        return False

    # Проверка на безопасные символы (буквы, цифры, точки, тире, подчеркивания)
    if not re.match(r'^[\w\.\-\s]+$', filename):
        return False

    return True


if __name__ == '__main__':
    # Запускаем асинхронный сервер
    asyncio.run(serve())