#!/bin/bash
#
# Запуск всех микросервисов RAG Copilot в отдельных окнах screen.
#
# Каждый сервис запускается из своей директории, чтобы корректно
# работали относительные пути к config.json, моделям, базам данных.
#
# Использование:
#   chmod +x start_screen.sh
#   ./start_screen.sh          — запустить все сервисы
#   ./start_screen.sh status   — показать статус screen-сессий
#   ./start_screen.sh stop     — остановить все сервисы
#   ./start_screen.sh restart  — перезапустить все сервисы
#
# Подключиться к окну конкретного сервиса:
#   screen -r rag_auth
#   screen -r rag_embedder
#   (выйти из screen без остановки: Ctrl+A, затем D)
#
# Посмотреть все screen-сессии:
#   screen -ls
#

# ── Определяем корень проекта ──────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES_DIR="$SCRIPT_DIR/services"

# Префикс для имён screen-сессий (чтобы не путать с другими)
PREFIX="rag"

# Путь к Python (можно заменить на путь к venv)
PYTHON="${PYTHON:-python3}"

# Пауза между запусками сервисов (секунды)
DELAY=2

# ── Список сервисов ────────────────────────────────────────────
# Формат: "имя_сервиса:файл_запуска"
# Порядок важен — зависимости должны запускаться раньше.
#
#   1. qdrant    — векторная БД (нужна indexer и searcher)
#   2. auth      — авторизация (нужна всем веб-сервисам)
#   3. embedder  — эмбеддинги (нужен indexer и searcher)
#   4. chunker   — чанкинг (нужен indexer)
#   5. converter — конвертация PDF (нужен manager)
#   6. indexer   — индексация (зависит от chunker, embedder, qdrant)
#   7. manager   — управление файлами (зависит от converter, indexer)
#   8. searcher  — поиск (зависит от embedder, qdrant)
#   9. generator — генерация ответов LLM
#  10. websearch — веб-интерфейс поиска (зависит от searcher, generator)
#  11. webconfig — управление конфигурациями
#  12. web_ui    — веб-интерфейс мониторинга (зависит от manager)

SERVICES=(
    "qdrant:qdrant"
    "auth:auth.py"
    "embedder:embedder.py"
    "chunker:chunker.py"
    "converter:converter.py"
    "indexer:indexer.py"
    "manager:manager.py"
    "searcher:searcher.py"
    "generator:generator.py"
    "websearch:websearch.py"
    "webconfig:webconfig.py"
    "web_ui:web_ui_service.py"
)

# ── Функции ────────────────────────────────────────────────────

print_header() {
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  RAG Copilot — $1"
    echo "════════════════════════════════════════════════════════"
    echo ""
}

# Проверяем, установлен ли screen
check_screen() {
    if ! command -v screen &> /dev/null; then
        echo "ОШИБКА: screen не установлен."
        echo "Установите: sudo apt install screen"
        exit 1
    fi
}

# Проверяем, запущена ли сессия с таким именем
is_running() {
    local session_name="$1"
    screen -ls 2>/dev/null | grep -q "\.${session_name}[[:space:]]"
}

# Запуск одного сервиса
start_service() {
    local name="$1"
    local main_file="$2"
    local session_name="${PREFIX}_${name}"
    local service_dir="$SERVICES_DIR/$name"

    # Проверяем, не запущен ли уже
    if is_running "$session_name"; then
        echo "  ⏩  $name — уже запущен (screen: $session_name)"
        return 0
    fi

    # Особый случай: Qdrant — бинарный файл, не Python
    if [ "$name" = "qdrant" ]; then
        local qdrant_bin="$service_dir/qdrant"
        if [ ! -f "$qdrant_bin" ]; then
            echo "  ⚠️   qdrant — бинарник не найден: $qdrant_bin (пропускаю)"
            return 1
        fi
        screen -dmS "$session_name" bash -c "cd '$service_dir' && ./qdrant 2>&1; echo '--- Qdrant завершён. Нажмите Enter ---'; read"
        echo "  ✅  $name — запущен (screen: $session_name)"
        return 0
    fi

    # Проверяем наличие файла
    if [ ! -f "$service_dir/$main_file" ]; then
        echo "  ⚠️   $name — файл не найден: $service_dir/$main_file (пропускаю)"
        return 1
    fi

    # Запускаем Python-сервис в screen
    # bash -c нужен чтобы: 1) cd в директорию сервиса, 2) после завершения не закрывать окно
    screen -dmS "$session_name" bash -c "cd '$service_dir' && $PYTHON '$main_file' 2>&1; echo '--- $name завершён (код: \$?). Нажмите Enter ---'; read"

    echo "  ✅  $name — запущен (screen: $session_name)"
    return 0
}

# Остановка одного сервиса
stop_service() {
    local name="$1"
    local session_name="${PREFIX}_${name}"

    if is_running "$session_name"; then
        # Посылаем Ctrl+C в screen-сессию для graceful shutdown
        screen -S "$session_name" -X stuff "^C"
        sleep 1

        # Если всё ещё работает — убиваем сессию
        if is_running "$session_name"; then
            screen -S "$session_name" -X quit 2>/dev/null
        fi

        echo "  ⏹️   $name — остановлен"
    else
        echo "  ⏩  $name — не был запущен"
    fi
}

# Показать статус всех сервисов
show_status() {
    print_header "Статус сервисов"

    local running=0
    local stopped=0

    for entry in "${SERVICES[@]}"; do
        local name="${entry%%:*}"
        local session_name="${PREFIX}_${name}"

        if is_running "$session_name"; then
            echo "  🟢  $name — работает (screen -r $session_name)"
            ((running++))
        else
            echo "  🔴  $name — не запущен"
            ((stopped++))
        fi
    done

    echo ""
    echo "  Итого: $running работает, $stopped остановлено"
    echo ""
    echo "  Подключиться к сервису:  screen -r ${PREFIX}_<имя>"
    echo "  Отключиться от screen:   Ctrl+A, затем D"
    echo ""
}

# Запуск всех сервисов
start_all() {
    print_header "Запуск сервисов"

    check_screen

    local started=0
    local skipped=0

    for entry in "${SERVICES[@]}"; do
        local name="${entry%%:*}"
        local main_file="${entry#*:}"

        if start_service "$name" "$main_file"; then
            ((started++))
        else
            ((skipped++))
        fi

        sleep "$DELAY"
    done

    echo ""
    echo "  Запущено: $started, пропущено: $skipped"
    echo ""
    echo "  Посмотреть логи сервиса:  screen -r ${PREFIX}_<имя>"
    echo "  Статус всех сервисов:     $0 status"
    echo "  Остановить всё:           $0 stop"
    echo ""
}

# Остановка всех сервисов
stop_all() {
    print_header "Остановка сервисов"

    # Останавливаем в обратном порядке (сначала зависимые)
    local reversed=()
    for entry in "${SERVICES[@]}"; do
        reversed=("$entry" "${reversed[@]}")
    done

    for entry in "${reversed[@]}"; do
        local name="${entry%%:*}"
        stop_service "$name"
    done

    echo ""
    echo "  Все сервисы остановлены."
    echo ""
}

# Перезапуск всех сервисов
restart_all() {
    stop_all
    sleep 2
    start_all
}

# ── Точка входа ────────────────────────────────────────────────

case "${1:-start}" in
    start)
        start_all
        ;;
    stop)
        stop_all
        ;;
    restart)
        restart_all
        ;;
    status)
        show_status
        ;;
    *)
        echo "Использование: $0 {start|stop|restart|status}"
        echo ""
        echo "  start   — запустить все сервисы (по умолчанию)"
        echo "  stop    — остановить все сервисы"
        echo "  restart — перезапустить все сервисы"
        echo "  status  — показать статус сервисов"
        exit 1
        ;;
esac
