#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
prompt()  { echo -e "${YELLOW}$1${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "=================================================="
echo "       FCC TG Bot — Deploy Script"
echo "=================================================="
echo ""

# ── 1. Docker ──────────────────────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
    info "Docker не найден. Устанавливаю..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    info "Docker установлен."
else
    info "Docker: $(docker --version)"
fi

if ! docker compose version &>/dev/null; then
    info "Docker Compose plugin не найден. Устанавливаю..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    info "Docker Compose установлен."
else
    info "Docker Compose: $(docker compose version --short)"
fi

# ── 2. .env ────────────────────────────────────────────────────────────────────

if [ -f .env ]; then
    warn ".env уже существует. Пропускаю создание."
    warn "Если нужно изменить конфиг — отредактируй .env и перезапусти: docker compose up -d --build"
else
    info "Создаю .env..."
    echo ""

    prompt "Введите Telegram Bot Token (от @BotFather):"
    read -r BOT_TOKEN
    [ -z "$BOT_TOKEN" ] && error "Bot Token не может быть пустым."

    prompt "Введите Telegram ID суперадминов через запятую (например: 123456789,987654321):"
    read -r SUPERADMIN_IDS
    [ -z "$SUPERADMIN_IDS" ] && error "Нужен хотя бы один суперадмин."

    prompt "Введите пароль для PostgreSQL (Enter = сгенерировать автоматически):"
    read -r POSTGRES_PASSWORD
    if [ -z "$POSTGRES_PASSWORD" ]; then
        POSTGRES_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
        info "Сгенерирован пароль PostgreSQL: $POSTGRES_PASSWORD"
    fi

    cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
SUPERADMIN_IDS=${SUPERADMIN_IDS}

POSTGRES_DB=fcc_bot
POSTGRES_USER=fcc
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOF

    info ".env создан."
fi

echo ""

# ── 3. Build & Up ──────────────────────────────────────────────────────────────

info "Собираю и запускаю контейнеры..."
docker compose pull postgres redis 2>/dev/null || true
docker compose build bot

info "Запускаю стек..."
docker compose up -d

# ── 4. Ждём бот ────────────────────────────────────────────────────────────────

info "Жду запуска бота (миграции + старт)..."
TIMEOUT=60
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    STATUS=$(docker compose ps bot --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('State',''))" 2>/dev/null || echo "")
    if [ "$STATUS" = "running" ]; then
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo ""
info "Статус контейнеров:"
docker compose ps

echo ""

# ── 5. Проверка ────────────────────────────────────────────────────────────────

BOT_STATUS=$(docker compose ps bot --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('State','unknown'))" 2>/dev/null || echo "unknown")

if [ "$BOT_STATUS" = "running" ]; then
    echo ""
    echo -e "${GREEN}=================================================="
    echo "  Деплой завершён успешно!"
    echo -e "==================================================${NC}"
    echo ""
    info "Логи бота: docker compose logs -f bot"
    info "Остановить: docker compose down"
    info "Перезапустить: docker compose restart bot"
    info "Обновить (после git pull): docker compose up -d --build"
else
    echo ""
    warn "Бот не запустился за ${TIMEOUT}с. Последние логи:"
    echo ""
    docker compose logs --tail=40 bot
    echo ""
    error "Проверь ошибки выше. Возможно неверный BOT_TOKEN или проблема с БД."
fi
