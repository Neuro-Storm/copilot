# Микросервис поискового движка

Это микросервис, который служит поисковым движком в RAG (Retrieval Augmented Generation) пайплайне. Он принимает текстовый запрос, получает эмбеддинги из сервиса embedder и выполняет семантический поиск в векторной базе Qdrant.

## Архитектура

Сервис следует следующему рабочему процессу:
1. Принимает текстовый поисковый запрос
2. **Dense поиск:** Отправляет текст в сервис embedder через gRPC для получения векторного представления
3. **Sparse поиск:** Токенизирует запрос, кодирует в sparse vector (BM25)
4. **Qdrant prefetch:** Параллельный поиск по dense + sparse векторам
5. **RRF fusion:** Слияние результатов через Reciprocal Rank Fusion
6. **MMR re-ranking:** Опциональное пере-ранжирование для разнообразия
7. Возвращает результаты поиска с метаданными

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

## Гибридный поиск (Dense + Sparse BM25)

### Конфигурация

```json
{
  "hybrid_search": true,
  "sparse_vector_name": "bm25_sparse",
  "hybrid_prefetch_limit": 20
}
```

Параметры:
- `hybrid_search`: `true/false` — включение гибридного поиска
- `sparse_vector_name`: имя sparse вектора в Qdrant (должно совпадать с indexer)
- `hybrid_prefetch_limit`: сколько результатов запрашивать из каждого источника

### Принцип работы

1. **Dense prefetch:** Поиск по векторным эмбеддингам (косинусная близость)
2. **Sparse prefetch:** Поиск по BM25 sparse vector (ключевые слова)
3. **RRF fusion:** Qdrant сливает результаты через Reciprocal Rank Fusion:
   ```
   score = 1/(k + rank_dense) + 1/(k + rank_sparse)
   ```
   где k=60 (стандартное сглаживание)

### Преимущества

| Аспект | Dense | Sparse | Гибридный |
|--------|-------|--------|-----------|
| Семантика | ✅ | ❌ | ✅ |
| Точные совпадения | ❌ | ✅ | ✅ |

## MMR (Maximal Marginal Relevance)

### Конфигурация

```json
{
  "mmr_enabled": true,
  "mmr_lambda": 0.7,
  "mmr_prefetch_multiplier": 3
}
```

Параметры:
- `mmr_enabled`: `true/false` — включение MMR
- `mmr_lambda`: баланс релевантность/разнообразие (0.0..1.0)
  - `0.7` = 70% релевантность, 30% разнообразие (рекомендуется)
  - `1.0` = только релевантность (MMR отключён)
  - `0.0` = только разнообразие
- `mmr_prefetch_multiplier`: во сколько раз больше запрашивать у Qdrant
  - `3` = при `result_count=10` запрашивается 30 результатов

### Принцип работы

MMR итеративно выбирает результаты, максимизируя:
```
score = λ · norm_relevance(d) - (1-λ) · max_similarity(d, selected)
```

- **Релевантность:** Нормализованный score от Qdrant [0, 1]
- **Сходство:** Jaccard similarity на множествах токенов
  ```
  J(A,B) = |A ∩ B| / |A ∪ B|
  ```

### Преимущества

- Уменьшает дублирование чанков из одного документа
- Повышает покрытие разных аспектов запроса
- Jaccard на токенах не требует запрашивать вектора из Qdrant

## Логирование

Логи записываются в stdout с уровнем INFO и выше.

## Использование

### Как библиотека

```python
from searcher import SearchEngine

search_engine = SearchEngine()

# Гибридный поиск и MMR применяются автоматически на основе конфигурации
results = search_engine.search("ваш поисковый запрос")

# results содержит отранжированные результаты с метаданными
for r in results:
    print(f"Score: {r['score']}, Text: {r['payload']['text'][:100]}...")

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
curl -X PUT 'http://localhost:6333/collections/documents1024' \
-H 'Content-Type: application/json' \
-d '{
  "vectors": {
    "size": 1024,
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