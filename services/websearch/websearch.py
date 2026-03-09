from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import HTTPException
import sys
import os
# Добавляем путь к generator
generator_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'generator')
sys.path.insert(0, generator_path)

import grpc
import time
import json
import logging
from typing import Dict
from pydantic import BaseModel

# Импортируем gRPC файлы
import searcher_pb2
import searcher_pb2_grpc
import generator_pb2
import generator_pb2_grpc

# Класс для конфигурации
class Config:
    """Класс для хранения и загрузки конфигурации сервиса поиска.

    Класс загружает конфигурацию из JSON файла или переменных окружения,
    обеспечивая параметры для подключения к searcher и generator сервисам,
    а также настройки веб-сервера.
    """

    def __init__(self):
        """Инициализирует конфигурацию из JSON файла или переменных окружения.

        Загружает параметры конфигурации из файла config.json или переменных окружения,
        с возможностью переопределения значений по умолчанию. Поддерживает параметры
        для подключения к searcher и generator сервисам, а также настройки веб-сервера.
        """
        # Загружаем конфигурацию из JSON файла
        config_file = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        else:
            config_data = {}

        # Нормализуем config_data: приводим ключи к верхнему регистру для согласованности
        normalized_config = {k.upper(): v for k, v in config_data.items()}

        # Адрес и порт для подключения к searcher сервису
        self.SEARCHER_HOST: str = os.getenv("SEARCHER_HOST",
            normalized_config.get("SEARCHER_HOST", normalized_config.get("SEARCHERHOST", "localhost")))  # Адрес searcher сервиса
        self.SEARCHER_PORT: int = int(os.getenv("SEARCHER_PORT",
            normalized_config.get("SEARCHER_PORT", normalized_config.get("SEARCHERPORT", 50052))))  # Порт searcher сервиса

        # Полный адрес для подключения к searcher
        self.SEARCHER_ADDRESS: str = f"{self.SEARCHER_HOST}:{self.SEARCHER_PORT}"  # Полный адрес searcher сервиса

        # Порт для запуска веб-сервера
        self.WEB_SERVER_PORT: int = int(os.getenv("WEB_SERVER_PORT",
            normalized_config.get("WEB_SERVER_PORT", normalized_config.get("WEBSERVERPORT", "8001"))))  # Порт веб-сервера

        # Хост для запуска веб-сервера
        self.WEB_SERVER_HOST: str = os.getenv("WEB_SERVER_HOST",
            normalized_config.get("WEB_SERVER_HOST", normalized_config.get("WEBSERVERHOST", "0.0.0.0")))  # Хост веб-сервера

        # Таймаут для gRPC запросов (в секундах)
        self.GRPC_TIMEOUT: float = float(os.getenv("GRPC_TIMEOUT",
            normalized_config.get("GRPC_TIMEOUT", normalized_config.get("GRPCTIMEOUT", 30.0))))  # Таймаут gRPC запросов

        # Константы
        self.MAX_QUERY_LENGTH: int = int(os.getenv("MAX_QUERY_LENGTH",
            normalized_config.get("MAX_QUERY_LENGTH", normalized_config.get("MAXQUERYLENGTH", 1000))))  # Максимальная длина запроса

        # Директории
        self.TEMPLATES_DIR: str = os.getenv("TEMPLATES_DIR",
            normalized_config.get("TEMPLATES_DIR", normalized_config.get("TEMPLATESDIR", "templates")))  # Директория шаблонов
        self.STATIC_DIR: str = os.getenv("STATIC_DIR",
            normalized_config.get("STATIC_DIR", normalized_config.get("STATICDIR", "static")))  # Директория статических файлов

        # Параметры генерации (для интеграции с generator сервисом)
        self.ENABLE_GENERATION: bool = bool(os.getenv("ENABLE_GENERATION",
            normalized_config.get("ENABLE_GENERATION", False)))  # Включение генерации ответов
        self.GENERATOR_HOST: str = os.getenv("GENERATOR_HOST",
            normalized_config.get("GENERATOR_HOST", normalized_config.get("GENERATORHOST", "localhost")))  # Адрес generator сервиса
        self.GENERATOR_PORT: int = int(os.getenv("GENERATOR_PORT",
            normalized_config.get("GENERATOR_PORT", normalized_config.get("GENERATORPORT", 50056))))  # Порт generator сервиса
        self.GENERATOR_ADDRESS: str = f"{self.GENERATOR_HOST}:{self.GENERATOR_PORT}"  # Полный адрес generator сервиса

        # Проверяем существование директорий
        if not os.path.isdir(self.TEMPLATES_DIR):
            raise FileNotFoundError(f"Директория шаблонов не найдена: {self.TEMPLATES_DIR}")
        if not os.path.isdir(self.STATIC_DIR):
            raise FileNotFoundError(f"Директория статики не найдена: {self.STATIC_DIR}")

# Глобальная переменная конфигурации
config = Config()

async def startup_event():
    """Инициализация gRPC каналов при старте приложения.

    Создает асинхронные gRPC каналы к searcher и generator сервисам
    в зависимости от конфигурации. Канал к searcher создается всегда,
    канал к generator - только если генерация включена.
    """
    import grpc.aio

    logger.info(f"Инициализация gRPC канала к {config.SEARCHER_ADDRESS}")
    searcher_channel = grpc.aio.insecure_channel(config.SEARCHER_ADDRESS)
    searcher_stub = searcher_pb2_grpc.SearcherServiceStub(searcher_channel)

    # Сохраняем канал и stub searcher в app.state
    app.state.searcher_channel = searcher_channel
    app.state.searcher_stub = searcher_stub

    # Инициализация канала к generator, если генерация включена
    if config.ENABLE_GENERATION:
        logger.info(f"Инициализация gRPC канала к Generator {config.GENERATOR_ADDRESS}")
        generator_channel = grpc.aio.insecure_channel(config.GENERATOR_ADDRESS)
        generator_stub = generator_pb2_grpc.GeneratorServiceStub(generator_channel)

        # Сохраняем канал и stub generator в app.state
        app.state.generator_channel = generator_channel
        app.state.generator_stub = generator_stub

        logger.info("gRPC канал к Generator успешно инициализирован")
    else:
        logger.info("Генерация ответов отключена")

    logger.info("gRPC каналы успешно инициализированы")


async def shutdown_event():
    """Закрытие gRPC каналов при завершении работы приложения.

    Закрывает асинхронные gRPC каналы к searcher и generator сервисам
    в порядке, обратном их созданию.
    """
    if hasattr(app.state, 'searcher_channel') and app.state.searcher_channel:
        logger.info("Закрытие gRPC канала к searcher...")
        await app.state.searcher_channel.close()
        logger.info("gRPC канал к searcher закрыт")

    if hasattr(app.state, 'generator_channel') and app.state.generator_channel:
        logger.info("Закрытие gRPC канала к generator...")
        await app.state.generator_channel.close()
        logger.info("gRPC канал к generator закрыт")


async def lifespan(app):
    """Функция жизненного цикла приложения FastAPI.

    Обеспечивает инициализацию и завершение работы приложения,
    включая создание и закрытие gRPC каналов к внешним сервисам.

    Args:
        app: Экземпляр приложения FastAPI

    Yields:
        None: Передает управление приложению
    """
    await startup_event()
    yield
    await shutdown_event()


# Создаем приложение с lifespan
app = FastAPI(
    title="Web Search Interface",
    description="RAG Search System Web Interface",
    lifespan=lifespan
)

# Pydantic модель для входных данных поиска
class SearchIn(BaseModel):
    """Модель для входных данных поискового запроса.

    Attributes:
        query (str): Текст поискового запроса
        enable_generation (bool): Флаг включения генерации ответа на основе найденных документов
    """
    query: str  # Текст поискового запроса
    enable_generation: bool = False  # Флаг включения генерации ответа

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("websearch.log", encoding='utf-8'),
        logging.StreamHandler()  # Также выводим в консоль
    ]
)
logger = logging.getLogger(__name__)

# Монтируем статические файлы и шаблоны
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Отображает основную страницу поиска.

    Страница позволяет ввести поисковый запрос и, при наличии соответствующей
    функциональности в интерфейсе, включить генерацию ответа на основе найденных документов.

    Args:
        request: Запрос от клиента

    Returns:
        HTMLResponse: Основная страница поиска
    """
    logger.info(f"Запрос главной страницы от {request.client.host}")
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/search")
async def search(query_data: SearchIn):
    """Обрабатывает поисковый запрос через gRPC.

    Выполняет поиск с использованием searcher сервиса и, при необходимости,
    генерирует ответ на основе найденных документов с использованием generator сервиса.

    Args:
        query_data (SearchIn): Данные запроса с поисковым запросом и флагом генерации
            - query: текст поискового запроса
            - enable_generation: флаг включения генерации ответа (требует включенной генерации в конфигурации)

    Returns:
        dict: Результаты поиска с метаданными и, при необходимости, сгенерированный ответ
            - results: список найденных документов с текстом, оценкой, ID источника и метаданными
            - search_time: время выполнения поиска
            - query: исходный поисковый запрос
            - generation (опционально): информация о сгенерированном ответе, если включена генерация
                - answer: сгенерированный ответ
                - sources: список источников, использованных для генерации
                - generation_time: время генерации из generator сервиса
                - total_generation_time: общее время генерации

    Raises:
        HTTPException: При ошибках валидации или во время поиска
    """
    query = query_data.query.strip()  # Текст поискового запроса
    enable_generation = query_data.enable_generation and config.ENABLE_GENERATION  # Флаг включения генерации
    logger.info(f"Получен поисковый запрос: {query[:50]}{'...' if len(query) > 50 else ''}, генерация: {enable_generation}")

    # Проверяем длину запроса
    if not query:
        logger.warning("Получен пустой поисковый запрос")
        raise HTTPException(status_code=400, detail="Query parameter is required")

    if len(query) > config.MAX_QUERY_LENGTH:
        logger.warning(f"Слишком длинный поисковый запрос: {len(query)} символов")
        raise HTTPException(status_code=400, detail=f"Query is too long. Maximum length is {config.MAX_QUERY_LENGTH} characters")

    # Проверяем, что searcher_stub инициализирован
    if not hasattr(app.state, 'searcher_stub') or app.state.searcher_stub is None:
        logger.error("gRPC stub не инициализирован")
        raise HTTPException(status_code=503, detail="Сервис searcher временно недоступен: не инициализирован gRPC stub")

    try:
        # Формируем gRPC запрос к searcher
        request_proto = searcher_pb2.SearchRequest(query=query)

        # Отправляем запрос в searcher
        start_time = time.perf_counter()  # Время начала поиска
        logger.info(f"Отправка запроса в searcher: {config.SEARCHER_ADDRESS}")
        response = await app.state.searcher_stub.Search(request_proto, timeout=config.GRPC_TIMEOUT)
        end_time = time.perf_counter()  # Время окончания поиска

        search_time = end_time - start_time  # Время выполнения поиска
        logger.info(f"Получен ответ от searcher за {search_time:.3f} секунд, результатов: {len(response.results)}")

        # Преобразуем результаты поиска в JSON
        results = []  # Список результатов поиска
        for result in response.results:
            result_dict = {
                "text": result.text,        # Текст найденного документа
                "score": result.score,      # Оценка релевантности
                "source_id": result.source_id,  # ID источника
                "metadata": dict(result.metadata)  # Метаданные документа
            }
            results.append(result_dict)

        logger.info(f"Поиск завершен, найдено {len(results)} результатов")

        # Подготовим основной ответ
        response_data = {
            "results": results,      # Результаты поиска
            "search_time": search_time,  # Время поиска
            "query": query           # Исходный запрос
        }

        # Если включена генерация и есть результаты поиска
        if enable_generation and results:
            if not hasattr(app.state, 'generator_stub') or app.state.generator_stub is None:
                logger.error("gRPC stub для generator не инициализирован")
                logger.warning("Пропускаем генерацию ответа")
            else:
                try:
                    # Подготовим результаты поиска для генерации
                    generator_results = []  # Результаты для передачи в generator
                    for result in results:
                        generator_result = generator_pb2.SearchResult(
                            text=result["text"],        # Текст результата
                            score=result["score"],      # Оценка релевантности
                            source_id=result["source_id"],  # ID источника
                            metadata=result["metadata"]  # Метаданные
                        )
                        generator_results.append(generator_result)

                    # Создаем запрос к генератору
                    gen_request = generator_pb2.GenerationRequest(
                        query=query,              # Поисковый запрос
                        search_results=generator_results  # Результаты поиска
                    )

                    # Вызываем генерацию
                    gen_start_time = time.perf_counter()  # Время начала генерации
                    gen_response = await app.state.generator_stub.Generate(
                        gen_request,
                        timeout=config.GRPC_TIMEOUT
                    )
                    gen_end_time = time.perf_counter()  # Время окончания генерации

                    generation_time = gen_end_time - gen_start_time  # Общее время генерации
                    logger.info(f"Генерация завершена за {generation_time:.3f} секунд")

                    # Добавляем информацию о генерации к ответу
                    response_data["generation"] = {
                        "answer": gen_response.answer,  # Сгенерированный ответ
                        "sources": list(gen_response.sources),  # Источники, использованные для генерации
                        "generation_time": gen_response.generation_time,  # Время генерации из generator сервиса
                        "total_generation_time": generation_time  # Общее время генерации
                    }

                except grpc.RpcError as e:
                    logger.error(f"gRPC ошибка при генерации: {e.code()} - {e.details()}")
                    # Не прерываем выполнение, просто не добавляем генерацию к результатам
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при генерации: {str(e)}", exc_info=True)
                    # Не прерываем выполнение, просто не добавляем генерацию к результатам

        return response_data

    except grpc.RpcError as e:
        logger.error(f"gRPC ошибка: {e.code()} - {e.details()}")
        if e.code() == grpc.StatusCode.UNIMPLEMENTED:
            raise HTTPException(status_code=503, detail=f"Метод поиска не реализован на сервере searcher: {e.details()}")
        elif e.code() == grpc.StatusCode.UNAVAILABLE:
            raise HTTPException(status_code=503, detail=f"Сервис searcher недоступен по адресу {config.SEARCHER_ADDRESS}: {e.details()}")
        else:
            raise HTTPException(status_code=500, detail=f"gRPC ошибка при поиске: {e.code().name} - {e.details()}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при поиске: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при поиске: {str(e)}")


@app.get("/api/health")
async def health_check():
    """Проверяет состояние сервиса и доступность searcher и generator (если генерация включена).

    Returns:
        dict: Статус сервиса, searcher и generator (если используется)
    """
    logger.info("Запрос проверки состояния сервиса")

    # Проверяем, что searcher_stub инициализирован
    if not hasattr(app.state, 'searcher_stub') or app.state.searcher_stub is None:
        logger.warning("Health check: searcher_stub не инициализирован")
        return {"status": "unhealthy", "details": "gRPC stub для searcher не инициализирован"}

    # Проверяем searcher канал
    try:
        # Проверяем готовность aio-канала через асинхронный метод
        await app.state.searcher_channel.channel_ready(timeout=5.0)

        logger.info("Health check: канал searcher доступен")

        # Если генерация включена, проверяем также generator
        if config.ENABLE_GENERATION:
            if not hasattr(app.state, 'generator_stub') or app.state.generator_stub is None:
                logger.warning("Health check: generator_stub не инициализирован, но генерация включена")
                return {"status": "degraded", "searcher": "available", "generator": "not_initialized"}

            try:
                # Проверяем готовность канала generator
                await app.state.generator_channel.channel_ready(timeout=5.0)

                logger.info("Health check: каналы searcher и generator доступны")
                return {"status": "healthy", "searcher": "available", "generator": "available", "channel": "ready"}
            except grpc.FutureTimeoutError:
                logger.warning("Health check: канал generator не готов в течение 5 секунд")
                return {"status": "degraded", "searcher": "available", "generator": "channel_not_ready"}
            except Exception as e:
                logger.error(f"Health check: ошибка при проверке канала generator - {str(e)}")
                return {"status": "degraded", "searcher": "available", "generator": "error", "error": str(e)}
        else:
            # Генерация отключена, возвращаем статус только для searcher
            return {"status": "healthy", "searcher": "available", "channel": "ready"}

    except grpc.FutureTimeoutError:
        logger.warning("Health check: канал searcher не готов в течение 5 секунд")
        return {"status": "degraded", "searcher": "channel_not_ready"}
    except Exception as e:
        logger.error(f"Health check: ошибка при проверке канала - {str(e)}")
        return {"status": "unhealthy", "searcher": "error", "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.WEB_SERVER_HOST, port=config.WEB_SERVER_PORT)