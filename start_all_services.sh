#!/bin/bash

# Скрипт для запуска всех микросервисов RAG-системы, включая Qdrant

echo "$(date '+%Y-%m-%d %H:%M:%S') - Начинаю запуск всех микросервисов..."

# Проверяем, запущен ли уже Qdrant
if ! pgrep -f "qdrant" > /dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Запускаю Qdrant на порту 6333..."
    cd "$(dirname "$0")/services/qdrant" && nohup qdrant >/dev/null 2>&1 &
    cd - >/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Qdrant запущен с PID $!"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Qdrant уже запущен"
fi

# Определяем список сервисов для запуска с их портами
declare -A SERVICES_PORTS
SERVICES_PORTS["auth"]="5050"
SERVICES_PORTS["chunker"]="50052"
SERVICES_PORTS["converter"]="50053"
SERVICES_PORTS["embedder"]="50051"
SERVICES_PORTS["indexer"]="50054"
SERVICES_PORTS["manager"]="5001"
SERVICES_PORTS["searcher"]="50055"
SERVICES_PORTS["websearch"]="8001"
SERVICES_PORTS["webconfig"]="50057"
SERVICES_PORTS["web_ui"]="8000"
SERVICES_PORTS["generator"]="50056"

SERVICES=("auth" "chunker" "converter" "embedder" "indexer" "manager" "searcher" "websearch" "webconfig" "web_ui" "generator")

# Получаем корневую директорию проекта
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
SERVICES_DIR="$SCRIPT_DIR/services"

# Запускаем каждый сервис в отдельном процессе
for service in "${SERVICES[@]}"; do
    SERVICE_DIR="$SERVICES_DIR/$service"

    # Особый случай для web_ui, который использует web_ui_service.py вместо web_ui.py
    if [ "$service" = "web_ui" ]; then
        MAIN_FILE="$SERVICE_DIR/web_ui_service.py"
    else
        MAIN_FILE="$SERVICE_DIR/$service.py"
    fi

    if [ -f "$MAIN_FILE" ]; then
        PORT="${SERVICES_PORTS[$service]}"
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Запускаю $service на порту $PORT..."

        # Запускаем сервис в фоновом режиме, подавляя вывод
        # Меняем директорию на директорию сервиса перед запуском
        cd "$SERVICE_DIR" && nohup python "$MAIN_FILE" --port="$PORT" >/dev/null 2>&1 &

        # Возвращаемся обратно в исходную директорию
        cd - >/dev/null

        echo "$(date '+%Y-%m-%d %H:%M:%S') - $service запущен с PID $! на порту $PORT"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ПРЕДУПРЕЖДЕНИЕ: Файл $MAIN_FILE не найден для $service"
    fi
done

echo
echo "$(date '+%Y-%m-%d %H:%M:%S') - Все сервисы запущены."
echo "$(date '+%Y-%m-%d %H:%M:%S') - Каждый сервис использует свои собственные лог-файлы"
echo "$(date '+%Y-%m-%d %H:%M:%S') - Список сервисов и их портов:"
for service in "${SERVICES[@]}"; do
    PORT="${SERVICES_PORTS[$service]}"
    echo "  - $service: localhost:$PORT"
done
echo "  - Qdrant: localhost:6333"
echo
echo "$(date '+%Y-%m-%d %H:%M:%S') - Нажмите Ctrl+C для остановки всех сервисов."
echo

# Ждем сигнал для завершения
trap 'echo; echo "$(date "+%Y-%m-%d %H:%M:%S") - Останавливаю все сервисы..."; pkill -f "python.*--port"; pkill -f "qdrant"; echo "$(date "+%Y-%m-%d %H:%M:%S") - Все сервисы остановлены."; exit 0' INT TERM

# Бесконечный цикл для удержания скрипта в работающем состоянии
while true; do
    sleep 1
done