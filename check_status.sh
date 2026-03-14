#!/bin/bash

# Скрипт для проверки статуса микросервисов RAG-системы

echo "$(date '+%Y-%m-%d %H:%M:%S') - Проверяю статус сервисов..."

# Определяем директорию скрипта для правильного поиска процессов
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
SERVICES_DIR="$SCRIPT_DIR/services"

# Проверяем статус Qdrant
if pgrep -f "qdrant" > /dev/null; then
    echo "Qdrant: РАБОТАЕТ ($(pgrep -f 'qdrant' | tr '\n' ' '))"
else
    echo "Qdrant: НЕ РАБОТАЕТ"
fi

# Определяем список сервисов и их главных файлов
declare -A SERVICE_FILES
SERVICE_FILES=(
    ["auth"]="auth.py"
    ["chunker"]="chunker.py"
    ["converter"]="converter.py"
    ["embedder"]="embedder.py"
    ["indexer"]="indexer.py"
    ["manager"]="manager.py"
    ["searcher"]="searcher.py"
    ["websearch"]="websearch.py"
    ["webconfig"]="webconfig.py"
    ["web_ui"]="web_ui_service.py"
    ["generator"]="generator.py"
)

# Порядок вывода сервисов
SERVICE_ORDER=("auth" "chunker" "converter" "embedder" "indexer" "manager" "searcher" "websearch" "webconfig" "web_ui" "generator")

# Проверяем статус каждого сервиса
for service in "${SERVICE_ORDER[@]}"; do
    main_file="${SERVICE_FILES[$service]}"
    pattern="python.*$SERVICES_DIR/$service/$main_file"

    if pgrep -f "$pattern" > /dev/null; then
        PIDS=$(pgrep -f "$pattern" | tr '\n' ' ')
        echo "$service: РАБОТАЕТ ($PIDS)"
    else
        echo "$service: НЕ РАБОТАЕТ"
    fi
done

echo "$(date '+%Y-%m-%d %H:%M:%S') - Проверка статуса завершена."