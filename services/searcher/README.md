# Микросервис поискового движка

Это микросервис, который служит поисковым движком в RAG (Retrieval Augmented Generation) пайплайне. Он принимает текстовый запрос, получает эмбеддинги из сервиса embedder и выполняет семантический поиск в векторной базе Qdrant.

## Архитектура

Сервис следует следующему рабочему процессу:
1. Принимает текстовый поисковый запрос
2. Отправляет текст в сервис embedder через gRPC для получения векторного представления
3. Выполняет запрос к Qdrant с векторным представлением для поиска похожих фрагментов документов
4. Возвращает результаты поиска с метаданными

## Конфигурация

Конфигурация хранится в `config.json` со следующими параметрами:

```json
{
  "searcher_host": "localhost",
  "searcher_port": 50055,
  "embedder_host": "localhost",
  "embedder_port": 50051,
  "qdrant_host": "localhost",
  "qdrant_port": 6333,
  "collection_name": "documents",
  "vector_name": "dense_vector",
  "result_count": 10,
  "grpc_timeout": 10,
  "with_payload": true,
  "with_vectors": false,
  "max_workers": 10,
  "qdrant_timeout": 10
}
```

- `searcher_host`: Адрес хоста gRPC-сервиса поиска
- `searcher_port`: Порт gRPC-сервиса поиска (по умолчанию 50055)
- `embedder_host`: Адрес хоста gRPC-сервиса embedder
- `embedder_port`: Порт gRPC-сервиса embedder
- `qdrant_host`: Адрес хоста сервиса Qdrant
- `qdrant_port`: Порт сервиса Qdrant
- `collection_name`: Название коллекции Qdrant для поиска
- `vector_name`: Название поля вектора в Qdrant (для именованных векторов)
- `result_count`: Количество результатов поиска для возврата
- `grpc_timeout`: Таймаут для gRPC-вызовов в секундах
- `with_payload`: Возвращать ли payload с результатами (по умолчанию true)
- `with_vectors`: Возвращать ли векторы с результатами (по умолчанию false)
- `max_workers`: Количество рабочих потоков gRPC-сервера (по умолчанию 10)
- `qdrant_timeout`: Таймаут для запросов к Qdrant (в секундах, по умолчанию 10)

## Зависимости

- grpcio
- protobuf
- qdrant-client

## Логирование

Логи записываются в stdout с уровнем INFO и выше.

## Использование

### Как библиотека
```python
from searcher import SearchEngine

search_engine = SearchEngine()
results = search_engine.search("ваш поисковый запрос")
search_engine.close()
```

### Как gRPC-сервис
Поиск может работать как gRPC-сервер, предоставляющий функции поиска другим сервисам:

```bash
python searcher.py
```

Это запускает gRPC-сервис поиска, который прослушивает настроенного порта (по умолчанию: 50055).

## Запуск сервиса

Установите зависимости:
```bash
pip install -r requirements.txt
```

Убедитесь, что Qdrant запущен и коллекция существует. Можно запустить Qdrant с помощью Docker:
```bash
docker run -p 6333:6333 --name qdrant-container qdrant/qdrant
```

Перед запуском сервиса убедитесь, что создали необходимую коллекцию в Qdrant:
```bash
curl -X PUT 'http://localhost:6333/collections/documents' \
-H 'Content-Type: application/json' \
-d '{
  "vectors": {
    "size": 384,
    "distance": "Cosine"
  }
}'
```

Также убедитесь, что сервис embedder запущен на настроенном хосте и порту.

Запустите сервис:
```bash
python searcher.py
```

## Тестирование сервиса

Для тестирования запущенного gRPC-сервиса можно использовать клиент тестирования из директории tests:

```bash
python tests/test_searcher_client.py
```