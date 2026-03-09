import grpc
import json
import logging
import os
import sys
import torch
from concurrent import futures
from typing import List

# Импорт сгенерированных gRPC-классов
import embedder_pb2
import embedder_pb2_grpc

# Импорт трансформеров для загрузки модели
from transformers import AutoTokenizer, AutoModel

# --- Загрузка конфигурации ---
CONFIG_PATH = "config.json"
DEFAULTS = {
    "server": {"host": "[::]", "port": 50051, "max_workers": 4},
    "model": {"path": "./models/models--ai-forever--ru-en-RoSBERTa", "device": "cpu", "max_length": 512},
    "logging": {"level": "INFO", "file": "embedder.log", "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"}
}

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

# Извлечение настроек из конфигурации
SERVER_HOST = DEFAULTS["server"]["host"]
SERVER_PORT = DEFAULTS["server"]["port"]
MAX_WORKERS = DEFAULTS["server"].get("max_workers", 4)
MODEL_PATH = DEFAULTS["model"]["path"]
DEVICE = DEFAULTS["model"]["device"]
MAX_LENGTH = DEFAULTS["model"]["max_length"]

# Настройка логирования
log_level = getattr(logging, DEFAULTS["logging"]["level"].upper())
logging.basicConfig(
    level=log_level,
    format=DEFAULTS["logging"]["format"],
    handlers=[
        logging.FileHandler(DEFAULTS["logging"]["file"]),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

class EmbedderService(embedder_pb2_grpc.EmbedderServiceServicer):
    """gRPC сервис для генерации эмбеддингов текста."""

    def __init__(self, model_path: str, device: str = 'cpu', max_length: int = 512):
        """Инициализирует сервис эмбеддинга с указанным моделем.

        Args:
            model_path: Путь к файлу модели эмбеддинга
            device: Устройство для выполнения ('cpu', 'gpu', 'cuda')
            max_length: Максимальная длина токенизированного текста
        """
        self.model_path = model_path
        self.device = device
        self.max_length = max_length
        self.tokenizer = None
        self.embedding_model = None
        self._load_model()

    def _load_model(self):
        """Загружает модель эмбеддинга по указанному пути."""
        try:
            # Проверяем доступность CUDA при запросе использования GPU
            if self.device == 'gpu':
                if torch.cuda.is_available():
                    self.device = 'cuda'
                else:
                    logger.warning("CUDA недоступна, используется CPU вместо GPU")
                    self.device = 'cpu'

            logger.info(f"Загрузка модели эмбеддинга из {self.model_path} на устройство {self.device}")

            # Проверяем существование пути к модели
            if not os.path.exists(self.model_path):
                raise FileNotFoundError(f"Каталог модели не найден: {self.model_path}")

            # Загружаем токенизатор и модель
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            self.embedding_model = AutoModel.from_pretrained(self.model_path)

            # Перемещаем модель на указанное устройство
            self.embedding_model = self.embedding_model.to(self.device)

            # Переводим модель в режим оценки (inference) для отключения dropout и batch normalization
            self.embedding_model.eval()

            logger.info(f"Модель перемещена на {self.device.upper()}")

            logger.info(f"Модель эмбеддинга успешно загружена из {self.model_path}")

        except Exception as e:
            logger.error(f"Не удалось загрузить модель эмбеддинга: {e}")
            raise

    def GetEmbedding(self, request: embedder_pb2.TextChunk, context) -> embedder_pb2.EmbeddingVector:
        """Генерирует вектор эмбеддинга для входного фрагмента текста.

        Args:
            request: TextChunk, содержащий текст для эмбеддинга
            context: Контекст gRPC

        Returns:
            EmbeddingVector, содержащий значения эмбеддинга
        """
        try:
            # Проверяем, не пустой ли текст
            if not request.text or request.text.strip() == "":
                logger.warning("Получен запрос с пустым текстом")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("Текст для генерации эмбеддинга не может быть пустым")
                return embedder_pb2.EmbeddingVector(values=[])

            logger.info(f"Получен запрос эмбеддинга для текста: {request.text[:50]}...")

            # Генерируем эмбеддинг с использованием загруженной модели
            embedding_values = self._generate_embedding(request.text)

            # Создаем и возвращаем вектор эмбеддинга
            response = embedder_pb2.EmbeddingVector(values=embedding_values)
            logger.info(f"Сгенерирован вектор эмбеддинга с {len(embedding_values)} размерностями")

            return response

        except Exception as e:
            logger.error(f"Ошибка при генерации эмбеддинга: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return embedder_pb2.EmbeddingVector(values=[])

    def _generate_embedding(self, text: str) -> List[float]:
        """Внутренний метод для генерации эмбеддинга для заданного текста с использованием загруженной модели.

        Args:
            text: Входной текст для генерации эмбеддинга

        Returns:
            Список значений с плавающей запятой, представляющих эмбеддинг
        """
        # Токенизируем входной текст
        inputs = self.tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=self.max_length)

        # Получаем устройство модели
        device = next(self.embedding_model.parameters()).device

        # Перемещаем входные данные на то же устройство, что и модель
        inputs = {key: val.to(device) for key, val in inputs.items()}

        # Генерируем эмбеддинги
        with torch.inference_mode():
            outputs = self.embedding_model(**inputs)

            # Используем masked mean pooling для лучшего качества эмбеддингов
            # Берем последние скрытые состояния
            last_hidden_states = outputs.last_hidden_state
            # Получаем attention mask
            attention_mask = inputs['attention_mask']
            # Расширяем attention mask до размерности скрытых состояний (..., seq_len, 1)
            expanded_attention_mask = attention_mask.unsqueeze(-1).expand(last_hidden_states.size()).float()
            # Умножаем скрытые состояния на маску, чтобы обнулить паддинги
            masked_last_hidden_states = last_hidden_states * expanded_attention_mask
            # Вычисляем сумму маски для нормализации (суммируем по sequence dimension)
            sum_mask = expanded_attention_mask.sum(1)
            # Избегаем деления на ноль
            sum_mask = torch.clamp(sum_mask, min=1e-9)
            # Вычисляем среднее с учетом маски
            embeddings = masked_last_hidden_states.sum(1) / sum_mask

            # Извлекаем результат
            embeddings = embeddings.squeeze().cpu().numpy()

        # Преобразуем в список значений с плавающей запятой
        return embeddings.tolist()

    def GetEmbeddings(self, request: embedder_pb2.TextChunks, context) -> embedder_pb2.EmbeddingVectors:
        """Генерирует векторы эмбеддинга для списка текстовых фрагментов.

        Args:
            request: TextChunks, содержащий список текстов для эмбеддинга
            context: Контекст gRPC

        Returns:
            EmbeddingVectors, содержащий список векторов эмбеддингов
        """
        try:
            # Проверяем, не пустой ли список текстов
            if not request.texts:
                logger.warning("Получен запрос с пустым списком текстов")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("Список текстов для генерации эмбеддингов не может быть пустым")
                return embedder_pb2.EmbeddingVectors(vectors=[])

            # Проверяем, не содержит ли список пустые тексты
            for i, text in enumerate(request.texts):
                if not text or text.strip() == "":
                    logger.warning(f"Получен запрос с пустым текстом в позиции {i}")
                    context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                    context.set_details(f"Текст в позиции {i} не может быть пустым")
                    return embedder_pb2.EmbeddingVectors(vectors=[])

            logger.info(f"Получен запрос на генерацию эмбеддингов для {len(request.texts)} текстов")

            # Токенизируем список текстов батчом
            texts = list(request.texts)
            inputs = self.tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=self.max_length)

            # Получаем устройство модели
            device = next(self.embedding_model.parameters()).device

            # Перемещаем входные данные на то же устройство, что и модель
            inputs = {key: val.to(device) for key, val in inputs.items()}

            # Генерируем эмбеддинги
            with torch.inference_mode():
                outputs = self.embedding_model(**inputs)

                # Используем masked mean pooling для лучшего качества эмбеддингов
                # Берем последние скрытые состояния
                last_hidden_states = outputs.last_hidden_state
                # Получаем attention mask
                attention_mask = inputs['attention_mask']
                # Расширяем attention mask до размерности скрытых состояний (..., seq_len, 1)
                expanded_attention_mask = attention_mask.unsqueeze(-1).expand(last_hidden_states.size()).float()
                # Умножаем скрытые состояния на маску, чтобы обнулить паддинги
                masked_last_hidden_states = last_hidden_states * expanded_attention_mask
                # Вычисляем сумму маски для нормализации (суммируем по sequence dimension)
                sum_mask = expanded_attention_mask.sum(1)
                # Избегаем деления на ноль
                sum_mask = torch.clamp(sum_mask, min=1e-9)
                # Вычисляем среднее с учетом маски
                embeddings_batch = masked_last_hidden_states.sum(1) / sum_mask

                # Преобразуем результаты в список векторов эмбеддингов
                embeddings_list = []
                for i in range(len(texts)):
                    embedding = embeddings_batch[i].cpu().numpy().tolist()
                    embeddings_list.append(embedding)

            # Создаем и возвращаем векторы эмбеддингов
            response = embedder_pb2.EmbeddingVectors()
            for embedding_values in embeddings_list:
                vector = embedder_pb2.EmbeddingVector(values=embedding_values)
                response.vectors.append(vector)

            logger.info(f"Сгенерировано {len(response.vectors)} векторов эмбеддингов")

            return response

        except Exception as e:
            logger.error(f"Ошибка при генерации эмбеддингов: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return embedder_pb2.EmbeddingVectors(vectors=[])


def serve():
    """Запускает сервер gRPC с сервисом эмбеддинга."""
    # Определение количества рабочих потоков в зависимости от устройства
    device_for_workers = DEVICE
    max_workers = MAX_WORKERS
    if device_for_workers in ['gpu', 'cuda'] and torch.cuda.is_available():
        # В оригинальном коде было ограничение при использовании GPU, сохраним эту логику
        gpu_max_workers = 1  # по умолчанию, как в оригинале
        # Если в конфигурации есть специальное значение для GPU, используем его
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
                gpu_max_workers = config.get('server', {}).get('gpu_max_workers', gpu_max_workers)
        except:
            pass
        max_workers = gpu_max_workers
        logger.info(f"GPU используется, ограничение потоков до {max_workers} для предотвращения пиков памяти")

    # Создание и настройка сервера
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))

    # Добавление сервиса эмбеддинга к серверу
    embedder_service = EmbedderService(model_path=MODEL_PATH, device=DEVICE, max_length=MAX_LENGTH)
    embedder_pb2_grpc.add_EmbedderServiceServicer_to_server(embedder_service, server)

    # Запуск сервера
    server_address = f"{SERVER_HOST}:{SERVER_PORT}"

    # В оригинальном коде TLS не использовался по умолчанию, поэтому оставляем простое подключение
    server.add_insecure_port(server_address)

    logger.info(f"Запуск сервиса эмбеддинга на {server_address}")
    server.start()
    logger.info("Сервер запущен. Ожидание запросов...")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
        server.stop(0)


if __name__ == '__main__':
    serve()