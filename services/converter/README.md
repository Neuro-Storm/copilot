# Микросервис конвертации документов (Docling + gRPC)

## Описание

Микросервис для конвертации PDF-документов в формат Markdown с использованием библиотеки Docling и gRPC-интерфейса. Сервис поддерживает:

- Конвертацию PDF-документов в Markdown
- OCR-обработку с поддержкой EasyOCR и RapidOCR
- GPU-акселерацию для ускорения обработки
- Возврат результата как в файл, так и в виде содержимого в ответе
- Оффлайн-работу (все модели хранятся локально)

## Архитектура

Сервис построен по модернизированной архитектуре:

- Использует объект `PdfPipelineOptions` для настройки конвейера
- Поддерживает выбор OCR-движка (EasyOCR/RapidOCR) через конфигурацию
- Управляет CPU-потоками и GPU-акселерацией
- Обеспечивает минималистичный и чистый код

## Установка

1. Установите зависимости:
```bash
pip install grpcio grpcio-tools docling-core docling-datamodel docling-document-converter
```

2. Убедитесь, что все модели находятся в директории `models/` (путь указывается в `config.json`)

3. Запустите сервер:
```bash
python converter.py
```

## Конфигурация

Сервис использует файл `config.json` для настройки:

```json
{
  "server": {
    "host": "localhost",
    "port": 50053
  },
  "logging": {
    "level": "INFO",
    "file": "converter.log",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
  },
  "system": {
    "cpu_threads": 8,
    "max_concurrent_requests": 1,
    "workspace_dir": "./workspace"
  },
  "docling": {
    "models_path": "./models",
    "ocr": {
      "enabled": true,
      "engine": "easyocr",
      "force_full_page_ocr": true,
      "languages": ["ru", "en"],
      "use_gpu": false
    },
    "table_structure": true,
    "images_scale": 1.0
  }
}
```

- `server`: настройки gRPC-сервера (host, port)
- `logging`: настройки логирования
- `system`: системные настройки
  - `cpu_threads`: количество потоков CPU
  - `max_concurrent_requests`: максимальное количество одновременных запросов
  - `workspace_dir`: рабочая директория для безопасности путей
- `docling`: настройки конвертации
  - `models_path`: путь к директории с моделями
  - `ocr`: настройки OCR
    - `enabled`: включить/выключить OCR
    - `engine`: выбор движка ('easyocr' или 'rapidocr')
    - `languages`: языки для OCR
    - `use_gpu`: использовать GPU для OCR
    - `force_full_page_ocr`: обязательная OCR-обработка всех страниц
  - `table_structure`: извлекать структуру таблиц
  - `images_scale`: масштабирование изображений для OCR

## Использование

### gRPC-интерфейс

Сервис предоставляет метод `ConvertFile`:

```protobuf
message ConvertRequest {
  string input_path = 1;    // Путь к исходному файлу
  string output_path = 2;   // Путь для сохранения (если return_content=false)
  bool return_content = 3;  // Возвращать содержимое в ответе
}

message ConvertResponse {
  bool success = 1;         // Успешность операции
  string markdown_content = 3; // Содержимое Markdown (если return_content=true)
  string output_path = 4;   // Путь к сохраненному файлу (если return_content=false)
}
```

## Улучшения

- **Упрощенная инициализация**: Используется единая архитектура `PdfPipelineOptions`
- **Поддержка двух OCR-движков**: Возможность выбора между EasyOCR и RapidOCR
- **Улучшенная обработка ошибок**: Установка gRPC-статусов при ошибках
- **Возврат содержимого**: Возможность получения результата в виде содержимого в ответе
- **Управление ресурсами**: Ограничение количества потоков и памяти
- **Graceful shutdown**: Корректное завершение работы при получении сигнала
- **Минимализм**: Убран дублирующийся код и улучшена структура