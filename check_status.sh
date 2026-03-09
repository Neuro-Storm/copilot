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

# Определяем список сервисов
SERVICES=("chunker" "converter" "embedder" "indexer" "manager" "searcher" "websearch" "webconfig" "web_ui" "generator")

# Проверяем статус каждого сервиса
for service in "${SERVICES[@]}"; do
    if pgrep -f "python.*$SERVICES_DIR/$service/$service\.py" > /dev/null; then
        PIDS=$(pgrep -f "python.*$SERVICES_DIR/$service/$service\.py" | tr '\n' ' ')
        echo "$service: РАБОТАЕТ ($PIDS)"
    else
        echo "$service: НЕ РАБОТАЕТ"
    fi
done

echo "$(date '+%Y-%m-%d %H:%M:%S') - Проверка статуса завершена."