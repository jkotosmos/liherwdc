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
a=$(grep '^TELEGRAM_BOT_TOKEN=' bot.env | cut -d= -f2- || true)
b=$(grep '^TG_BOT_TOKEN=' backend.env | cut -d= -f2- || true)
if [ -n "$a" ] && [ "$a" = "$b" ]; then
    echo "ОШИБКА: у бота OPERON и у бота сайта один и тот же токен Telegram —"
    echo "они будут отбирать друг у друга сообщения. Нужны два разных бота."
    exit 1
fi

echo "== Проверка ключей (значения не выводятся)"
# Токен с опечаткой Telegram отвергает сразу — ловим это до сборки.
check_tg() {
    [ -n "$2" ] || { echo "ВНИМАНИЕ: $1 — токен не задан"; return 0; }
    resp=$(curl -s -m 15 "https://api.telegram.org/bot$2/getMe" || true)
    case "$resp" in
        *'"ok":true'*) echo "Telegram, $1: токен принят" ;;
        *'"ok":false'*) echo "ОШИБКА: Telegram, $1: токен не принят — проверьте значение"; exit 1 ;;
        *) echo "ВНИМАНИЕ: Telegram, $1: проверить не удалось (нет связи с api.telegram.org)" ;;
    esac
}
check_tg "бот OPERON (bot.env)" "$a"
check_tg "бот сайта (backend.env)" "$b"
# Почта: вход на сайт — по коду из письма. Не блокируем запуск, но говорим сразу.
if command -v python3 >/dev/null 2>&1; then
    smtp=$( (set -a; . ./backend.env; set +a; python3 - <<'PY'
import os, smtplib, socket
try:
    s = smtplib.SMTP(os.environ.get("EMAIL_HOST", ""), int(os.environ.get("EMAIL_PORT") or 587), timeout=15)
    s.starttls()
    s.login(os.environ.get("EMAIL_USER", ""), os.environ.get("EMAIL_PASSWORD", ""))
    s.quit()
    print("ok")
except smtplib.SMTPAuthenticationError:
    print("auth")
except (OSError, socket.timeout, smtplib.SMTPException):
    print("net")
PY
) 2>/dev/null || true)
    case "$smtp" in
        ok) echo "Почта: вход принят — коды входа на сайт будут приходить" ;;
        auth) echo "ВНИМАНИЕ: почта: пароль не принят (EMAIL_PASSWORD) — коды входа на сайт не придут" ;;
        *) echo "ВНИМАНИЕ: почта: сервер недоступен — вероятно, хостинг закрыл порт ${EMAIL_PORT:-587}; нужна заявка в поддержку, иначе коды входа на сайт не придут" ;;
    esac
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
