import logging
import json
import grpc
import sys
from typing import Dict, List, Any
from concurrent import futures
import time

from qdrant_client import QdrantClient

# Импорт сгенерированных protobuf модулей
import embedder_pb2
import embedder_pb2_grpc

# Импорт searcher proto модулей
import searcher_pb2
import searcher_pb2_grpc

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
        self.vector_name = config.get("vector_name", "dense_vector")
        self.result_count = config.get("result_count", 10)
        self.grpc_timeout = config.get("grpc_timeout", 10)
        self.with_payload = config.get("with_payload", True)
        self.with_vectors = config.get("with_vectors", False)
        self.max_workers = config.get("max_workers", 10)
        self.qdrant_timeout = config.get("qdrant_timeout", 10)

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
        """Выполняет семантический поиск с заданным запросом.

        Args:
            query: Текст поискового запроса

        Returns:
            Список словарей, содержащих результаты поиска с их метаданными
        """
        logger.info(f"Выполнение поиска для запроса: {query}")

        try:
            # Получение эмбеддинга для запроса
            query_vector = self.get_embedding(query)

            # Выполнение поиска в Qdrant с использованием метода query_points
            # Указание имени вектора, если коллекция имеет именованные векторы
            search_results = self.qdrant_client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=self.result_count,
                with_payload=self.with_payload,
                with_vectors=self.with_vectors,
                using=self.vector_name,  # Использование имени вектора из конфигурации
                timeout=self.qdrant_timeout  # Использование таймаута из конфигурации
            ).points

            # Форматирование результатов
            formatted_results = []
            for hit in search_results:
                result = {
                    "score": hit.score,
                    "payload": hit.payload,
                    "id": hit.id
                }
                formatted_results.append(result)

            logger.info(f"Поиск успешно завершен, найдено {len(formatted_results)} результатов")
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