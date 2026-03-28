import logging
import json
import grpc
import sys
from typing import Dict, List, Any
from concurrent import futures
import time
import math
import re
import hashlib
from collections import Counter

from qdrant_client import QdrantClient
from qdrant_client.http import models

# Импорт сгенерированных protobuf модулей
import embedder_pb2
import embedder_pb2_grpc

# Импорт searcher proto модулей
import searcher_pb2
import searcher_pb2_grpc

# ─── Токенизация для BM25 query encoding ──────────────────────
_STOP_WORDS = {
    # Русские
    'и', 'в', 'на', 'с', 'по', 'для', 'из', 'к', 'от', 'за', 'о', 'об',
    'до', 'не', 'но', 'а', 'что', 'как', 'это', 'все', 'он', 'она', 'они',
    'мы', 'вы', 'я', 'ты', 'при', 'так', 'же', 'бы', 'ли', 'уже', 'был',
    'была', 'было', 'были', 'быть', 'если', 'его', 'её', 'их', 'или',
    # Английские
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'have',
    'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
    'of', 'in', 'to', 'for', 'with', 'on', 'at', 'from', 'by', 'as',
    'or', 'and', 'but', 'if', 'not', 'no', 'this', 'that', 'it',
}
_TOKEN_RE = re.compile(r'[a-zA-Zа-яА-ЯёЁ0-9]+', re.UNICODE)
_VOCAB_SIZE = 50000  # Должен совпадать с indexer'ом!


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


def encode_sparse_query(text: str) -> dict:
    """Кодирует запрос в sparse vector.

    Для запроса не нужен IDF из корпуса — каждый терм получает вес 1.0.
    Qdrant с модификатором Modifier.IDF сам применит IDF при скоринге.
    """
    tokens = tokenize(text)
    if not tokens:
        return None

    seen = {}
    for term in tokens:
        idx = _stable_hash(term) % _VOCAB_SIZE
        if idx not in seen:
            seen[idx] = 1.0  # Вес = 1.0, IDF применит Qdrant

    return {"indices": list(seen.keys()), "values": list(seen.values())}


logger = logging.getLogger(__name__)


class SearchEngine:
    """Микросервис для выполнения семантического поиска в RAG-пайплайне.

    Поисковый движок получает текстовый запрос, отправляет его в сервис embedder
    через gRPC для получения эмбеддингов, а затем выполняет поиск похожести
    в векторной базе данных (Qdrant), чтобы извлечь релевантные фрагменты документов.
    """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def __init__(self, config_path: str = "config.json", config: Dict[str, Any] = None):
        """Инициализирует поисковый движок с конфигурацией из JSON-файла или словаря.

        Args:
            config_path: Путь к файлу конфигурации (если config не передан)
            config: Словарь конфигурации (альтернатива загрузке из файла)
        """
        if config is not None:
            self.load_config_from_dict(config)
        else:
            self.load_config(config_path)

        # Настройка gRPC-канала для сервиса embedder
        embedder_url = f"{self.embedder_host}:{self.embedder_port}"
        self.embedder_channel = grpc.insecure_channel(embedder_url)
        self.embedder_stub = embedder_pb2_grpc.EmbedderServiceStub(self.embedder_channel)

        # Настройка клиента Qdrant
        self.qdrant_client = QdrantClient(host=self.qdrant_host, port=self.qdrant_port)

        # Проверка существования коллекции
        try:
            collection_info = self.qdrant_client.get_collection(self.collection_name)
            logger.info(f"Коллекция {self.collection_name} существует, векторное поле {self.vector_name} проверено")
        except Exception as e:
            logger.warning(f"Не удалось получить информацию о коллекции {self.collection_name}: {e}. Ошибка может возникнуть при первом запросе.")

        logger.info(f"SearchEngine инициализирован с конфигурацией: {'dict' if config is not None else config_path}")
        logger.info(f"Сервис embedder: {self.embedder_host}:{self.embedder_port}")
        logger.info(f"Сервис Qdrant: {self.qdrant_host}:{self.qdrant_port}")
        logger.info(f"Коллекция: {self.collection_name}")

    def load_config(self, config_path: str):
        """Загружает конфигурацию из JSON-файла.

        Args:
            config_path: Путь к файлу конфигурации
        """
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        self.load_config_from_dict(config)

    def load_config_from_dict(self, config: Dict[str, Any]):
        """Загружает конфигурацию из словаря.

        Args:
            config: Словарь конфигурации
        """
        # Извлечение значений конфигурации с умолчаниями
        self.searcher_host = config.get("searcher_host", "localhost")
        self.searcher_port = config.get("searcher_port", 50055)
        self.embedder_host = config.get("embedder_host", "localhost")
        self.embedder_port = config.get("embedder_port", 50051)
        self.qdrant_host = config.get("qdrant_host", "localhost")
        self.qdrant_port = config.get("qdrant_port", 6333)
        self.collection_name = config.get("collection_name", "documents")
        self.vector_name = config.get("vector_name") or None
        self.result_count = config.get("result_count", 10)
        self.grpc_timeout = config.get("grpc_timeout", 10)
        self.with_payload = config.get("with_payload", True)
        self.with_vectors = config.get("with_vectors", False)
        self.max_workers = config.get("max_workers", 10)
        self.qdrant_timeout = config.get("qdrant_timeout", 10)

        # ── Гибридный поиск ──
        self.hybrid_search = config.get("hybrid_search", False)
        self.sparse_vector_name = config.get("sparse_vector_name", "bm25_sparse")
        self.hybrid_prefetch_limit = config.get("hybrid_prefetch_limit", 20)

    def get_embedding(self, text: str) -> List[float]:
        """Получает векторное представление для заданного текста из сервиса embedder.

        Args:
            text: Входной текст для кодирования

        Returns:
            Список чисел с плавающей точкой, представляющий векторное представление
        """
        logger.debug(f"Получение эмбеддинга для текста: {text[:50]}...")

        try:
            # Создание gRPC-запроса
            request = embedder_pb2.TextChunk(text=text)

            # Вызов сервиса embedder
            response = self.embedder_stub.GetEmbedding(
                request,
                timeout=self.grpc_timeout
            )

            embedding_vector = list(response.values)

            # Проверка, что вектор содержит значения
            if not hasattr(response, 'values') or len(embedding_vector) == 0:
                logger.warning(f"Ответ от embedder-сервиса не содержит вектора для текста: {text[:50]}...")
                raise ValueError("Embedder-сервис вернул пустой вектор")

            logger.debug(f"Получен эмбеддинг с {len(embedding_vector)} размерностями")
            return embedding_vector

        except grpc.RpcError as e:
            logger.error(f"gRPC-ошибка при получении эмбеддинга: {e}")
            raise
        except Exception as e:
            logger.error(f"Ошибка при получении эмбеддинга: {e}")
            raise

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Выполняет поиск: чисто семантический или гибридный (dense + sparse).

        При hybrid_search=true используется Qdrant Prefetch + RRF:
        - prefetch 1: dense vector search (embedder → cosine)
        - prefetch 2: sparse vector search (BM25 tokens)
        - fusion: reciprocal rank fusion встроенный в Qdrant
        """
        logger.info(f"Выполнение поиска для запроса: {query}")

        try:
            # Получение dense-эмбеддинга (нужен в обоих режимах)
            query_vector = self.get_embedding(query)

            # ── Режим 1: Только dense (совместимость) ──
            if not self.hybrid_search:
                search_results = self.qdrant_client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    limit=self.result_count,
                    with_payload=self.with_payload,
                    with_vectors=self.with_vectors,
                    using=self.vector_name,
                    timeout=self.qdrant_timeout
                ).points
            else:
                # ── Режим 2: Гибридный (dense + sparse + RRF fusion) ──
                sparse_query = encode_sparse_query(query)

                prefetch = [
                    # Dense prefetch
                    models.Prefetch(
                        query=query_vector,
                        using=self.vector_name,
                        limit=self.hybrid_prefetch_limit,
                    ),
                ]

                # Sparse prefetch (только если есть токены)
                if sparse_query:
                    prefetch.append(
                        models.Prefetch(
                            query=models.SparseVector(
                                indices=sparse_query["indices"],
                                values=sparse_query["values"],
                            ),
                            using=self.sparse_vector_name,
                            limit=self.hybrid_prefetch_limit,
                        )
                    )

                # Qdrant Query с Reciprocal Rank Fusion
                try:
                    search_results = self.qdrant_client.query_points(
                        collection_name=self.collection_name,
                        prefetch=prefetch,
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=self.result_count,
                        with_payload=self.with_payload,
                        with_vectors=self.with_vectors,
                        timeout=self.qdrant_timeout,
                    ).points
                except Exception as e:
                    # Fallback на обычный dense-поиск при ошибке гибридного
                    logger.warning(f"Гибридный поиск не удался, fallback на dense: {e}")
                    search_results = self.qdrant_client.query_points(
                        collection_name=self.collection_name,
                        query=query_vector,
                        limit=self.result_count,
                        with_payload=self.with_payload,
                        with_vectors=self.with_vectors,
                        using=self.vector_name,
                        timeout=self.qdrant_timeout
                    ).points

            # Форматирование результатов
            formatted_results = []
            for hit in search_results:
                formatted_results.append({
                    "score": hit.score,
                    "payload": hit.payload,
                    "id": hit.id
                })

            logger.info(f"Поиск завершён, найдено {len(formatted_results)} результатов")
            return formatted_results

        except Exception as e:
            logger.error(f"Ошибка во время поиска: {e}")
            raise

    def close(self):
        """Закрывает соединения с внешними сервисами."""
        try:
            self.embedder_channel.close()
            logger.info("Закрыт gRPC-канал embedder")
        except Exception as e:
            logger.error(f"Ошибка при закрытии канала embedder: {e}")


class SearcherService(searcher_pb2_grpc.SearcherServiceServicer):
    """Реализация gRPC-сервиса для поиска."""

    def __init__(self, search_engine):
        """Инициализирует сервис поиска с движком поиска.

        Args:
            search_engine: Движок поиска для выполнения запросов
        """
        self.search_engine = search_engine

    def Search(self, request, context):
        """Обработчик для gRPC-метода Search.

        Args:
            request: Запрос с поисковым запросом
            context: Контекст gRPC вызова

        Returns:
            SearchResponse с результатами поиска
        """
        try:
            # Выполнение поиска с заданным запросом
            query = request.query

            # Проверка на пустой запрос
            if not query.strip():
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("Поисковый запрос не может быть пустым")
                return searcher_pb2.SearchResponse()

            # Измеряем время поиска
            start_time = time.perf_counter()
            # Используем конфигурационное значение result_count
            # (не изменяем его для каждого запроса, чтобы избежать проблем с параллелизмом)
            search_results = self.search_engine.search(query)
            end_time = time.perf_counter()
            search_time = end_time - start_time

            # Форматирование ответа
            results = []
            for result in search_results:
                payload = result.get('payload', {})

                # Преобразование payload в формат, подходящий для proto
                metadata = {}
                if isinstance(payload, dict):
                    for key, value in payload.items():
                        # Исключаем ключ 'text' из metadata, чтобы избежать дублирования
                        if key != 'text':
                            # Для нестроковых значений используем json.dumps для лучшего представления
                            metadata[str(key)] = json.dumps(value, ensure_ascii=False) if not isinstance(value, (str, int, float, bool)) else str(value)

                # Гарантируем, что text - это строка, даже если payload не содержит ключа 'text'
                text = ""
                if isinstance(payload, dict) and 'text' in payload:
                    text = str(payload.get('text', ''))

                search_result = searcher_pb2.SearchResult(
                    text=text,
                    score=result.get('score', 0.0),
                    source_id=str(result.get('id', '')),
                    metadata=metadata
                )
                results.append(search_result)

            response = searcher_pb2.SearchResponse(
                results=results,
                search_time=search_time  # Возвращаем реальное время поиска
            )

            return response

        except grpc.RpcError as e:
            logger.exception("Search failed due to RPC error")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(f"Сервис недоступен: {str(e)}")
            return searcher_pb2.SearchResponse()
        except ValueError as e:  # Ошибки валидации, например пустой эмбеддинг
            logger.exception("Search failed due to value error")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"Неверный аргумент: {str(e)}")
            return searcher_pb2.SearchResponse()
        except Exception as e:
            logger.exception("Search failed")  # Логирование стека вызовов при ошибке
            # Для сетевых ошибок (например, Qdrant недоступен) используем UNAVAILABLE
            if "connection" in str(e).lower() or "timeout" in str(e).lower() or "network" in str(e).lower():
                context.set_code(grpc.StatusCode.UNAVAILABLE)
                context.set_details(f"Сервис недоступен: {str(e)}")
            else:
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(f"Ошибка во время поиска: {str(e)}")
            return searcher_pb2.SearchResponse()



def serve(config_path="config.json"):
    """Запускает gRPC-сервер для сервиса поиска.

    Args:
        config_path: Путь к файлу конфигурации
    """
    # Загрузка конфигурации один раз
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # Настройка логирования при запуске как сервиса
    log_handlers = [logging.StreamHandler(sys.stdout)]

    # Если в конфиге указан файл лога, добавляем FileHandler
    log_file = config.get("log_file", "")
    if log_file:
        log_handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=log_handlers
    )

    # Создание экземпляра поискового движка с использованием контекстного менеджера
    with SearchEngine(config=config) as search_engine:

        # Создание gRPC-сервера
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=search_engine.max_workers))

        # Добавление нашего сервиса к серверу
        searcher_service = SearcherService(search_engine)
        searcher_pb2_grpc.add_SearcherServiceServicer_to_server(searcher_service, server)

        # Добавление адреса прослушивания из конфигурации
        host = search_engine.searcher_host
        port = search_engine.searcher_port
        server.add_insecure_port(f'{host}:{port}')

        # Запуск сервера
        logger.info(f"Запуск searcher gRPC-сервера на {host}:{port}")
        server.start()

        try:
            server.wait_for_termination()
        except KeyboardInterrupt:
            logger.info("Остановка searcher-сервера")
            server.stop(0)
            # Контекстный менеджер автоматически вызовет search_engine.close()


def main():
    """Основная функция для запуска gRPC-сервера."""
    serve()


if __name__ == "__main__":
    main()