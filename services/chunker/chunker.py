import logging
import re
import os
import json
from concurrent import futures
from pathlib import Path
from typing import Generator, Tuple, Iterator

import grpc
import chunker_pb2
import chunker_pb2_grpc

# --- Конфигурация ---
CONFIG_PATH = Path("config.json")
DEFAULTS = {
    "server": {"host": "[::]", "port": 50052, "max_workers": 4},
    "chunking": {"chunk_size": 500, "overlap_size": 50, "max_file_size_mb": 50},
    "data": {"base_dir": "."},
    "logging": {"level": "INFO", "file": "chunker.log", "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"}
}

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

# Извлечение конфигурации
SERVER_HOST = DEFAULTS["server"]["host"]
SERVER_PORT = DEFAULTS["server"]["port"]
MAX_WORKERS = DEFAULTS["server"].get("max_workers", 4)
CHUNK_SIZE = DEFAULTS["chunking"]["chunk_size"]
OVERLAP = DEFAULTS["chunking"]["overlap_size"]
MAX_FILE_SIZE_MB = DEFAULTS["chunking"]["max_file_size_mb"]
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024  # Convert to bytes
BASE_DIR = Path(DEFAULTS["data"]["base_dir"]).resolve()

# Настройка логирования
log_level = getattr(logging, DEFAULTS["logging"]["level"].upper())
logging.basicConfig(
    level=log_level,
    format=DEFAULTS["logging"]["format"],
    handlers=[
        logging.FileHandler(DEFAULTS["logging"]["file"]),
        logging.StreamHandler()
    ]
)

# --- Логика Чанкинга ---

def safe_read(path_str: str) -> str:
    """Безопасно читает содержимое файла с проверкой безопасности пути и размера файла.

    Args:
        path_str: Путь к файлу для чтения

    Returns:
        Содержимое файла в виде строки

    Raises:
        FileNotFoundError: Если файл не найден или находится вне разрешенной директории
        ValueError: Если размер файла превышает максимальный разрешенный размер
    """
    # Convert to Path object and resolve
    path = Path(path_str).resolve()

    # Check if the resolved path is within the BASE_DIR (for relative paths)
    # or if it's an absolute path that should be allowed (with additional checks)
    is_within_base = str(path).startswith(str(BASE_DIR))

    # For security, only allow files within BASE_DIR
    if not is_within_base or not path.is_file():
        raise FileNotFoundError(f"File not found: {path_str}")

    # Check file size to prevent memory issues
    file_size = path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File size {file_size} bytes exceeds maximum allowed size of {MAX_FILE_SIZE} bytes")

    return path.read_text(encoding='utf-8')


class RecursiveChunker:
    """Класс для рекурсивного разделения текста на фрагменты с возможностью перекрытия."""

    def __init__(self, chunk_size: int, overlap: int):
        """Инициализирует рекурсивный чанкер с заданным размером фрагментов и перекрытием.

        Args:
            chunk_size: Размер фрагмента текста
            overlap: Размер перекрытия между фрагментами
        """
        self.chunk_size = chunk_size
        self.overlap = overlap
        # Regex паттерны с захватом (): сохраняют разделитель в результате
        self.separators = [
            r"(\n{2,})",    # Абзацы
            r"(\n)",        # Строки
            r"(?<=[.?!])\s+", # Предложения (lookbehind оставляет знак препинания)
            r"( )",         # Слова
            r""             # Посимвольно (fallback)
        ]

    def split_text(self, text: str) -> Iterator[Tuple[str, int]]:
        """Генератор, возвращающий (текст_чанка, стартовая_позиция).

        Args:
            text: Входной текст для разделения

        Yields:
            Кортеж из текста фрагмента и начальной позиции в исходном тексте
        """
        start_idx = 0
        for chunk in self._process_text(text):
            yield chunk, start_idx
            # Расчет смещения для следующего чанка с учетом перекрытия сложен,
            # для простоты считаем от начала, но в RAG важнее сам текст.
            # Для точного start_idx можно искать подстроку, но это дорого.
            start_idx += len(chunk) - self.overlap if self.overlap else len(chunk)

    def _process_text(self, text: str, sep_idx: int = 0) -> Iterator[str]:
        """Рекурсивный генератор чанков.

        Args:
            text: Текст для разделения на фрагменты
            sep_idx: Индекс текущего разделителя в списке separators

        Yields:
            Фрагменты текста
        """
        length = len(text)
        if length <= self.chunk_size:
            yield text
            return

        if sep_idx >= len(self.separators):
            # Hard limit: режем жестко по символам
            for i in range(0, length, self.chunk_size - self.overlap):
                yield text[i : i + self.chunk_size]
            return

        separator = self.separators[sep_idx]
        # re.split с () вернет ['текст', 'разделитель', 'текст', ...]
        parts = re.split(separator, text) if separator else list(text)

        # Склеиваем части обратно, пока влезают в chunk_size
        buffer = ""
        for part in parts:
            if not part: continue

            # Если одна часть сама по себе огромная — рекурсивно бьем её
            if len(part) > self.chunk_size:
                if buffer:
                    yield buffer
                    buffer = ""
                yield from self._process_text(part, sep_idx + 1)
                continue

            if len(buffer) + len(part) <= self.chunk_size:
                buffer += part
            else:
                # Выдаем текущий буфер
                yield buffer
                # Логика Overlap: берем хвост от старого буфера
                if self.overlap > 0:
                    # Проверяем, что буфер достаточно длинный для оверлапа
                    if len(buffer) >= self.overlap:
                        buffer = buffer[-self.overlap:] + part
                    else:
                        buffer = buffer + part
                else:
                    buffer = part
                # Проверяем, не превышает ли новый буфер размер
                if len(buffer) > self.chunk_size:
                    # Если превышает, разбиваем его
                    yield from self._process_text(buffer, sep_idx + 1)
                    buffer = ""

        if buffer:
            # Если буфер в итоге превышает размер, нужно его разбить
            if len(buffer) > self.chunk_size:
                yield from self._process_text(buffer, sep_idx + 1)
            else:
                yield buffer



# --- gRPC Сервис ---

class Chunker(chunker_pb2_grpc.ChunkerServiceServicer):
    """gRPC сервис для разделения текста на фрагменты."""

    def ChunkIt(self, request, context):
        """Обработчик gRPC метода для разделения текста на фрагменты.

        Args:
            request: Запрос с текстом или путем к файлу для разделения
            context: Контекст gRPC вызова

        Yields:
            Объекты Chunk с фрагментами текста и метаданными
        """
        size = request.chunk_size or CHUNK_SIZE
        overlap = request.overlap or OVERLAP

        if size <= 0 or overlap < 0 or overlap >= size:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Bad chunk_size/overlap")
            return

        try:
            filename = "memory"
            if request.HasField('file_path'):
                text = safe_read(request.file_path)
                filename = Path(request.file_path).name
            elif request.HasField('content'):
                text = request.content
            else:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "No source provided")
                return

            if not text: return

            splitter = RecursiveChunker(size, overlap)

            # Используем генератор напрямую
            current_pos = 0
            for chunk_text, _ in splitter.split_text(text):
                # В RAG метаданные критичны для citations
                meta = {"source": filename, "length": str(len(chunk_text))}

                yield chunker_pb2.Chunk(
                    text=chunk_text,
                    start=current_pos,
                    end=current_pos + len(chunk_text),
                    metadata=meta
                )
                current_pos += len(chunk_text) # Примерная позиция

        except FileNotFoundError:
            context.abort(grpc.StatusCode.NOT_FOUND, "File not found")
        except ValueError as e:
            # Handle file size limit errors
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        except Exception as e:
            logging.error(f"Error: {e}", exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, str(e))

def serve():
    """Запускает gRPC сервер для сервиса чанкинга."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=MAX_WORKERS))
    chunker_pb2_grpc.add_ChunkerServiceServicer_to_server(Chunker(), server)
    server.add_insecure_port(f'{SERVER_HOST}:{SERVER_PORT}')
    logging.info(f"Chunker service active on {SERVER_HOST}:{SERVER_PORT} (Size: {CHUNK_SIZE}, Overlap: {OVERLAP})")
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    serve()