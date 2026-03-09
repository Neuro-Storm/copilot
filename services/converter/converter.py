import json
import logging
import os
import signal
from concurrent import futures
from pathlib import Path
import sys

# 1. Загружаем конфиг один раз в начале
CONFIG_PATH = "config.json"

# Загрузка и объединение конфигурации с значениями по умолчанию
DEFAULTS = {
    "server": {"host": "localhost", "port": 50053},
    "logging": {"level": "INFO", "file": "converter.log", "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    "system": {"cpu_threads": 8, "max_concurrent_requests": 1, "workspace_dir": "./workspace"},
    "docling": {
        "models_path": "./models",
        "ocr": {
            "enabled": True,
            "engine": "easyocr",
            "force_full_page_ocr": True,
            "languages": ["ru", "en"],
            "use_gpu": False
        },
        "table_structure": True,
        "images_scale": 1.0
    }
}

config = DEFAULTS.copy()

if Path(CONFIG_PATH).exists():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            file_config = json.load(f)
            # Обновляем defaults с учетом вложенной структуры
            for section, values in file_config.items():
                if section in config and isinstance(config[section], dict):
                    config[section].update(values)
                else:
                    config.update({section: values})
    except Exception as e:
        logging.warning(f"Config load failed: {e}")

# 2. Настройка окружения ДО импорта тяжелых библиотек
def setup_env(cfg: dict):
    sys_cfg = cfg.get('system', {})
    doc_cfg = cfg.get('docling', {})

    cpu_threads = str(sys_cfg.get('cpu_threads', 4))
    os.environ.update({
        "OMP_NUM_THREADS": cpu_threads,
        "MKL_NUM_THREADS": cpu_threads,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DOCLING_ARTIFACTS_PATH": doc_cfg.get('models_path', './models')
    })

setup_env(config)

# Импорты после настройки ENV
import grpc
from docling.document_converter import (
    DocumentConverter, PdfFormatOption, ImageFormatOption,
    InputFormat
)
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions, EasyOcrOptions, RapidOcrOptions,
    AcceleratorOptions, AcceleratorDevice
)

# Импорт сгенерированных gRPC-файлов
import converter_pb2
import converter_pb2_grpc

# Настройка логгера
log_level = getattr(logging, config['logging']['level'].upper())
logging.basicConfig(
    level=log_level,
    format=config['logging']['format'],
    handlers=[
        logging.FileHandler(config['logging']['file']),
        logging.StreamHandler()
    ]
)


class DoclingService(converter_pb2_grpc.DoclingConverterServicer):
    """gRPC сервис для конвертации документов с использованием Docling."""

    def __init__(self, cfg: dict):
        """Инициализирует сервис конвертации документов.

        Args:
            cfg: Конфигурация сервиса
        """
        logging.info("Инициализация сервиса Docling...")
        self.cfg = cfg
        self._validate_models_dir()
        self._setup_workspace_dir()
        self.converter = self._build_converter()
        logging.info("Сервис готов к работе.")

    def _setup_workspace_dir(self):
        """Настройка рабочей директории для безопасности путей"""
        workspace_path = Path(self.cfg['system'].get('workspace_dir', './workspace'))
        workspace_path.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = workspace_path.resolve()

    def _validate_models_dir(self):
        """Проверяет существование и доступность директории с моделями."""
        models_path = Path(self.cfg['docling']['models_path'])
        if not models_path.exists():
            raise RuntimeError(f"Директория моделей не существует: {models_path}")

        try:
            if not any(models_path.iterdir()):
                raise RuntimeError(f"Директория моделей пуста: {models_path}")
        except PermissionError:
            raise RuntimeError(f"Нет доступа к директории моделей: {models_path}")
        except OSError as e:
            raise RuntimeError(f"Ошибка доступа к директории моделей {models_path}: {e}")

    def _validate_path_access(self, path: Path) -> Path:
        """Проверяет, что путь находится в пределах рабочей директории и не является симлинком вне её.

        Args:
            path: Путь для проверки

        Returns:
            Проверенный и разрешенный путь
        """
        resolved_path = path.resolve()

        # Проверяем, что путь не указывает на симлинк вне рабочей директории
        if path.is_symlink():
            link_target = Path(os.readlink(path)).resolve()
            try:
                link_target.relative_to(self.workspace_dir)
            except ValueError:
                raise ValueError(f"Симлинк указывает вне рабочей директории: {link_target}")

        try:
            resolved_path.relative_to(self.workspace_dir)
        except ValueError:
            raise ValueError(f"Путь находится вне разрешенной рабочей директории: {resolved_path}")

        return resolved_path

    def _build_converter(self) -> DocumentConverter:
        """Создает и настраивает конвертер документов Docling.

        Returns:
            Настроенный экземпляр DocumentConverter
        """
        d_cfg = self.cfg['docling']
        ocr_cfg = d_cfg['ocr']

        # Настройка ускорителя
        device = AcceleratorDevice.CUDA if ocr_cfg.get('use_gpu') else AcceleratorDevice.CPU
        acc_opts = AcceleratorOptions(
            device=device,
            num_threads=self.cfg['system'].get('cpu_threads', 4)
        )

        # Настройка параметров конвейера
        pipeline_options = PdfPipelineOptions(
            artifacts_path=d_cfg['models_path'],
            do_ocr=ocr_cfg['enabled'],
            do_table_structure=d_cfg.get('table_structure', True),
            accelerator_options=acc_opts,
            images_scale=d_cfg.get('images_scale', 1.0)
        )

        # Настройка параметров OCR только если OCR включен
        if ocr_cfg['enabled']:
            ocr_engine = ocr_cfg.get('engine', 'easyocr')
            if ocr_engine == 'rapidocr':
                # Настройка RapidOCR (исправлено: добавлены языки)
                pipeline_options.ocr_options = RapidOcrOptions(
                    force_full_page_ocr=ocr_cfg.get('force_full_page_ocr', True),
                    lang=ocr_cfg.get('languages', ['ru', 'en'])
                )
            else:
                # Настройка EasyOCR (по умолчанию)
                pipeline_options.ocr_options = EasyOcrOptions(
                    force_full_page_ocr=ocr_cfg.get('force_full_page_ocr', True),
                    lang=ocr_cfg.get('languages', ['ru', 'en']),
                    use_gpu=ocr_cfg.get('use_gpu', False),
                    download_enabled=False,
                    model_storage_directory=str(Path(d_cfg['models_path']) / "easyocr")
                )

        # Настройки для форматов, требующих специальной обработки (PDF и изображения)
        from docling.datamodel.document import InputFormat
        format_options = {
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options)
        }

        # Инициализируйте конвертер с поддержкой всех форматов
        return DocumentConverter(
            allowed_formats=None,  # Разрешить все поддерживаемые форматы
            format_options=format_options
        )

    def ConvertFile(self, request, context):
        """Обработчик gRPC метода для конвертации файлов.

        Args:
            request: Запрос с информацией о файле для конвертации
            context: Контекст gRPC вызова

        Returns:
            ConvertResponse с результатом конвертации
        """
        logging.info(f"Запрос конвертации: {request.input_path}")
        try:
            # Валидация параметров запроса
            if not request.input_path:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Input path is required")

            input_path = Path(request.input_path)

            # Проверяем безопасность пути
            try:
                self._validate_path_access(input_path)
            except ValueError as e:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))

            if not input_path.is_file():
                context.abort(grpc.StatusCode.NOT_FOUND, f"Input file not found: {input_path}")

            # Если return_content == false и output_path пустой, используем input_path с .md расширением
            if not request.return_content and not request.output_path:
                output_path = input_path.with_suffix('.md')
            else:
                output_path = Path(request.output_path)

                # Проверяем безопасность пути для выходного файла
                try:
                    self._validate_path_access(output_path)
                except ValueError as e:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))

            result = self.converter.convert(input_path)
            if not result.document:
                raise RuntimeError(f"Ошибка конвертации: документ не был создан")

            md_content = result.document.export_to_markdown()

            if request.return_content:
                return converter_pb2.ConvertResponse(
                    success=True,
                    markdown_content=md_content
                )
            else:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(md_content, encoding='utf-8')
                logging.info(f"Сохранено в: {output_path}")
                return converter_pb2.ConvertResponse(
                    success=True,
                    output_path=str(output_path)
                )

        except grpc.RpcError:
            # Перебрасываем gRPC ошибки как есть
            raise
        except Exception as e:
            logging.error(f"Внутренняя ошибка: {e}", exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, str(e))


def serve():
    """Запускает gRPC сервер для сервиса конвертации документов."""
    server_host = config['server']['host']
    server_port = config['server']['port']
    max_msg_size = 50 * 1024 * 1024  # 50MB

    # Используем отдельную настройку для максимального числа одновременных gRPC-воркеров
    # чтобы избежать "thread explosion" - умножения потоков при параллельной обработке
    max_concurrent_workers = config['system'].get('max_concurrent_requests', 2)  # По умолчанию 2, а не cpu_threads

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_concurrent_workers),
        options=[
            ('grpc.max_send_message_length', max_msg_size),
            ('grpc.max_receive_message_length', max_msg_size)
        ]
    )

    converter_pb2_grpc.add_DoclingConverterServicer_to_server(DoclingService(config), server)

    address = f"{server_host}:{server_port}"
    server.add_insecure_port(address)
    server.start()
    logging.info(f"Сервер запущен на {address}")

    # Graceful shutdown
    def handle_sigterm(*_):
        logging.info("Получен сигнал остановки, завершаем работу...")
        done_event = server.stop(grace=10)
        done_event.wait(10)
        logging.info("Сервер остановлен.")

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    server.wait_for_termination()


if __name__ == '__main__':
    serve()