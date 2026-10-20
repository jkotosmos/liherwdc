#!/bin/sh
# Сервер приложения: сайт базы знаний, бот OPERON, HTTPS.
#   Первый раз:  канал WireGuard (../README.md), затем
#                cp .env.example .env; cp ../../timeweb/backend.env.example backend.env;
#                cp ../../timeweb/bot.env.example bot.env; заполнить; sh setup.sh
#   Обновление:  sh setup.sh
set -e
cd "$(dirname "$0")"

echo "== Docker"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "ОШИБКА: нет docker compose"; exit 1; }

echo "== Код"
git -C ../../.. pull --ff-only || true
mkdir -p src data/bot data/caddy
for repo in ai-knowledge-base-backend ai-knowledge-base-frontend; do
    if [ -d "src/$repo/.git" ]; then
        git -C "src/$repo" pull --ff-only
    else
        git clone --depth 1 "https://github.com/skullmare/$repo.git" "src/$repo"
    fi
done

echo "== Настройки"
missing=0
for f in .env backend.env bot.env; do
    if [ ! -f "$f" ]; then
        echo "НЕТ файла $f"
        missing=1
    else
        sed -i 's/\r$//' "$f"   # файл из Windows: убрать CRLF
        chmod 600 "$f"
    fi
done
[ "$missing" = 0 ] || exit 1
. ./.env
for v in KB_DOMAIN BOT_DOMAIN MONGO_APP_PASSWORD MONGO_BOT_PASSWORD QDRANT_API_KEY QDRANT_READ_KEY; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || { echo "ОШИБКА: в .env не задан $v"; exit 1; }
done
if grep -q '^TELEGRAM_BOT_TOKEN=' bot.env && grep -q '^TG_BOT_TOKEN=' backend.env; then
    a=$(grep '^TELEGRAM_BOT_TOKEN=' bot.env | cut -d= -f2-)
    b=$(grep '^TG_BOT_TOKEN=' backend.env | cut -d= -f2-)
    if [ -n "$a" ] && [ "$a" = "$b" ]; then
        echo "ОШИБКА: у бота OPERON и у бота сайта один и тот же токен Telegram —"
        echo "они будут отбирать друг у друга сообщения. Нужны два разных бота."
        exit 1
    fi
fi

echo "== Канал к базам"
db="${DB_HOST:-10.8.0.1}"
for port in 27017 6333; do
    if ! nc -z -w 5 "$db" "$port" 2>/dev/null; then
        echo "ОШИБКА: база $db:$port недоступна. Проверьте канал (ping -c 3 $db, wg show)"
        echo "и что на сервере баз запущен setup.sh."
        exit 1
    fi
done
echo "MongoDB и Qdrant доступны через канал ($db)"

echo "== Сборка и запуск (первый раз — несколько минут)"
docker compose up -d --build
docker compose ps
echo
echo "Готово. Проверьте:"
echo "  сайт:  https://$KB_DOMAIN"
echo "  бот:   https://$BOT_DOMAIN/api/health  и /check в Telegram"
echo "  журнал: docker compose logs -f backend bot"
