#!/bin/bash

# Скрипт для остановки всех микросервисов RAG-системы

# Определяем директорию скрипта для правильной остановки процессов
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
SERVICES_DIR="$SCRIPT_DIR/services"

echo "$(date '+%Y-%m-%d %H:%M:%S') - Останавливаю все сервисы..."

# Останавливаем все Python-процессы, связанные с микросервисами
pkill -f "python.*$SERVICES_DIR/\(chunker\|converter\|embedder\|indexer\|manager\|searcher\|websearch\|webconfig\|web_ui\|generator\)\.py"

# Останавливаем Qdrant
pkill -f "qdrant"

echo "$(date '+%Y-%m-%d %H:%M:%S') - Все сервисы остановлены."