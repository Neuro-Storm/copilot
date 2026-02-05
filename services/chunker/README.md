# Микросервис Chunker

Минимальный gRPC микросервис для разделения текста на более мелкие фрагменты. Предназначен для использования в RAG (Retrieval-Augmented Generation) системах, где все функции реализованы в виде микросервисов.

## Возможности

- Принимает текст как из файлов, так и из памяти (гибридный подход)
- Поддерживает настраиваемый размер фрагментов и перекрытие
- Использует рекурсивный character splitter для более умной нарезки с сохранением семантики
- Безопасное чтение файлов с защитой от Path Traversal (ограничение в пределах BASE_DIR)
- Ограничение размера файлов для предотвращения проблем с памятью
- Валидация параметров (chunk_size, overlap) для предотвращения ошибок
- Поддержка метаданных в чанках для улучшения контекста в RAG-системах
- Минимальная реализация, подходящая для архитектуры микросервисов

## Конфигурация

Сервис настраивается через файл `config.json` с поддержкой значений по умолчанию:

```json
{
  "server": {
    "host": "[::]",
    "port": 50052,
    "max_workers": 4
  },
  "chunking": {
    "chunk_size": 500,
    "overlap_size": 50,
    "max_file_size_mb": 50
  },
  "data": {
    "base_dir": "."
  },
  "logging": {
    "level": "INFO",
    "file": "chunker.log",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
  }
}
```

## Файлы

- `chunker.proto`: Определение gRPC сервиса
- `chunker.py`: Реализация gRPC сервера
- `config.json`: Конфигурационный файл
- `chunker_pb2.py`: Сгенерированные protobuf классы
- `chunker_pb2_grpc.py`: Сгенерированные gRPC классы

## Использование

### Запуск сервера

```bash
python chunker.py
```

По умолчанию сервер запускается на порту 50052.

## Определение gRPC сервиса

Сервис реализует следующий интерфейс:

```protobuf
service ChunkerService {
  rpc ChunkIt(ChunkRequest) returns (stream Chunk);
}

message ChunkRequest {
  // oneof экономит память: передается либо то, либо другое
  oneof source {
    string file_path = 1;  // Для локальных файлов (быстро)
    string content = 2;    // Для данных из памяти/сети (гибко)
  }
  int32 chunk_size = 3;
  int32 overlap = 4;
}

message Chunk {
  string text = 1;
  int32 start = 2;
  int32 end = 3;
  map<string, string> metadata = 4;  // Добавлены метаданные
}
```