import logging
import json
import time
from concurrent import futures
from pathlib import Path
from typing import List, Dict, Any

import grpc
import llama_cpp

import generator_pb2
import generator_pb2_grpc

# --- Конфигурация ---
# Путь к файлу конфигурации
CONFIG_PATH = Path("config.json")

# Значения по умолчанию для конфигурации сервиса
DEFAULTS = {
    "server": {
        "host": "[::]",      # IP-адрес, на котором будет запущен gRPC сервер
        "port": 50056,       # Порт, на котором будет работать сервис
        "max_workers": 4     # Максимальное количество рабочих потоков для обработки запросов
    },
    "model": {
        "path": "./models/model.gguf",  # Путь к GGUF-модели
        "n_ctx": 2048,                  # Размер контекста модели
        "n_threads": 4,                 # Количество потоков CPU
        "n_gpu_layers": 0,              # Количество слоев для GPU (0 для CPU)
        "temperature": 0.7,             # Температура генерации
        "top_p": 0.9,                   # Параметр top-p для генерации
        "repeat_penalty": 1.1,          # Штраф за повторения
        "max_tokens": 1024              # Максимальное количество токенов в ответе
    },
    "system_prompt": "Вы являетесь помощником в предоставлении ответов на вопросы на основе предоставленных документов. Отвечайте точно и кратко, опираясь только на информацию, содержащуюся в документах.",
    "logging": {
        "level": "INFO",                # Уровень логирования (DEBUG, INFO, WARNING, ERROR)
        "file": "generator.log",        # Имя файла для сохранения логов
        "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"  # Формат строки лога
    }
}

# Загрузка конфигурации из файла, если он существует
if CONFIG_PATH.exists():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            file_config = json.load(f)
            # Обновляем defaults с учетом вложенной структуры
            for section, values in file_config.items():
                if section in DEFAULTS and isinstance(DEFAULTS[section], dict):
                    DEFAULTS[section].update(values)
                else:
                    DEFAULTS.update({section: values})
    except Exception as e:
        logging.warning(f"Config load failed: {e}")

# Извлечение конфигурации для сервера
SERVER_HOST = DEFAULTS["server"]["host"]  # IP-адрес сервера
SERVER_PORT = DEFAULTS["server"]["port"]  # Порт сервера
MAX_WORKERS = DEFAULTS["server"].get("max_workers", 4)  # Количество рабочих потоков

# Извлечение конфигурации для модели
MODEL_PATH = DEFAULTS["model"]["path"]        # Путь к модели
N_CTX = DEFAULTS["model"]["n_ctx"]            # Размер контекста модели
N_THREADS = DEFAULTS["model"]["n_threads"]    # Количество потоков CPU
N_GPU_LAYERS = DEFAULTS["model"]["n_gpu_layers"]  # Количество слоев для GPU
TEMPERATURE = DEFAULTS["model"]["temperature"]    # Температура генерации
TOP_P = DEFAULTS["model"]["top_p"]              # Параметр top-p для генерации
REPEAT_PENALTY = DEFAULTS["model"]["repeat_penalty"]  # Штраф за повторения
MAX_TOKENS = DEFAULTS["model"]["max_tokens"]          # Максимальное количество токенов в ответе

# Системный промпт по умолчанию
SYSTEM_PROMPT = DEFAULTS["system_prompt"]

# Настройка логирования
log_level = getattr(logging, DEFAULTS["logging"]["level"].upper())  # Уровень логирования
logging.basicConfig(
    level=log_level,
    format=DEFAULTS["logging"]["format"],  # Формат строки лога
    handlers=[
        logging.FileHandler(DEFAULTS["logging"]["file"]),  # Логирование в файл
        logging.StreamHandler()  # Логирование в консоль
    ]
)

class GeneratorService(generator_pb2_grpc.GeneratorServiceServicer):
    """gRPC сервис для генерации ответов на основе поисковой выдачи.

    Этот класс реализует gRPC-сервис, который принимает поисковый запрос и результаты поиска,
    генерирует на их основе осмысленный ответ с использованием локальной LLM модели.
    """

    def __init__(self):
        """Инициализирует модель LLM при создании экземпляра сервиса.

        Загружает модель llama-cpp с параметрами, указанными в конфигурации.
        В случае ошибки при загрузке модели выбрасывает исключение.
        """
        try:
            # Инициализация модели llama-cpp с параметрами из конфигурации
            self.model = llama_cpp.Llama(
                model_path=MODEL_PATH,      # Путь к модели
                n_ctx=N_CTX,                # Размер контекста модели
                n_threads=N_THREADS,        # Количество потоков CPU
                n_gpu_layers=N_GPU_LAYERS,  # Количество слоев для GPU (0 для CPU)
                verbose=False               # Отключаем внутренние логи llama.cpp
            )
            logging.info(f"Модель успешно загружена: {MODEL_PATH}")
        except Exception as e:
            logging.error(f"Ошибка при загрузке модели: {e}")
            raise

    def Generate(self, request, context):
        """Обработчик gRPC метода для генерации ответа на основе поисковой выдачи.

        Принимает поисковый запрос и результаты поиска, формирует промпт с учетом
        системного промпта и контекста, после чего генерирует ответ с использованием
        локальной LLM модели.

        Args:
            request (generator_pb2.GenerationRequest): Запрос с поисковым запросом и результатами поиска
                - query: поисковый запрос пользователя
                - search_results: результаты поиска с текстом и метаданными
                - system_prompt: системный промпт (опционально, используется из конфига по умолчанию)
            context (grpc.ServicerContext): Контекст gRPC вызова

        Returns:
            generator_pb2.GenerationResponse: Сгенерированный ответ с источниками
                - answer: сгенерированный ответ
                - sources: список источников, использованных для генерации
                - generation_time: время генерации в секундах

        Raises:
            grpc.RpcError: В случае ошибки при генерации
        """
        start_time = time.time()

        # Извлечение данных из запроса
        query = request.query                      # Поисковый запрос пользователя
        search_results = request.search_results  # Результаты поиска с текстом и метаданными

        # Логирование получения запроса
        logging.info(f"Получен запрос на генерацию для вопроса: {query[:50]}{'...' if len(query) > 50 else ''}")

        # Подготовка контекста из результатов поиска
        context_texts = []  # Тексты из результатов поиска
        sources = []        # Идентификаторы источников

        # Извлечение текстов и источников из результатов поиска
        for result in search_results:
            context_texts.append(result.text)      # Добавляем текст результата
            sources.append(result.source_id)       # Добавляем идентификатор источника

        # Формирование общего контекста из всех текстов результатов
        search_context = "\n\n".join(context_texts)

        # Определение системного промпта: из запроса или из конфигурации по умолчанию
        # В proto3 строковые поля всегда присутствуют, поэтому проверяем на пустоту
        system_prompt = request.system_prompt if request.system_prompt else SYSTEM_PROMPT

        # Формируем промпт в формате, подходящем для модели
        # Структура промпта: системный промпт -> контекст -> вопрос -> генерация ответа
        full_prompt = f"""{system_prompt}

Контекст:
{search_context}

Вопрос: {query}

Ответ:"""

        try:
            # Генерация ответа с помощью llama-cpp с параметрами из конфигурации
            response = self.model(
                full_prompt,              # Полный промпт для модели
                max_tokens=MAX_TOKENS,    # Максимальное количество токенов в ответе
                temperature=TEMPERATURE,  # Температра генерации
                top_p=TOP_P,              # Параметр top-p для генерации
                repeat_penalty=REPEAT_PENALTY,  # Штраф за повторения
                stop=["Вопрос:", "Контекст:", "</s>", "Ответ:"],  # Останавливаем на этих токенах
                echo=False  # Не включаем промпт в вывод
            )

            # Извлечение сгенерированного текста из ответа модели
            generated_text = response['choices'][0]['text'].strip()

            # Расчет времени генерации
            generation_time = time.time() - start_time

            # Логирование успешной генерации
            logging.info(f"Генерация завершена за {generation_time:.3f} секунд")

            # Возвращение сгенерированного ответа с источниками и временем генерации
            return generator_pb2.GenerationResponse(
                answer=generated_text,      # Сгенерированный ответ
                sources=sources,            # Список источников, использованных для генерации
                generation_time=generation_time  # Время генерации в секундах
            )

        except Exception as e:
            # Логирование ошибки при генерации с трассировкой стека
            logging.error(f"Ошибка при генерации: {e}", exc_info=True)
            # Прерывание gRPC контекста с ошибкой INTERNAL
            context.abort(grpc.StatusCode.INTERNAL, f"Ошибка при генерации ответа: {str(e)}")


def serve():
    """Запускает gRPC сервер для сервиса генерации.

    Создает gRPC сервер с указанным количеством рабочих потоков, регистрирует
    обработчики сервиса GeneratorService и запускает сервер на указанном хосте и порту.
    """
    # Создание gRPC сервера с пулом рабочих потоков
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=MAX_WORKERS))

    # Регистрация обработчиков сервиса GeneratorService
    generator_pb2_grpc.add_GeneratorServiceServicer_to_server(GeneratorService(), server)

    # Привязка сервера к указанному адресу и порту
    server.add_insecure_port(f'{SERVER_HOST}:{SERVER_PORT}')

    # Логирование запуска сервиса
    logging.info(f"Generator service активен на {SERVER_HOST}:{SERVER_PORT}")

    # Запуск сервера
    server.start()

    # Блокировка до завершения работы сервера
    server.wait_for_termination()


if __name__ == '__main__':
    serve()