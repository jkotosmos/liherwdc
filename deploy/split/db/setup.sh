#!/bin/sh
# Сервер баз: MongoDB и Qdrant, доступ только через WireGuard.
#   Первый раз:  sh ../wireguard.sh db <IP сервера приложения>  (и шаги из README)
#                cp .env.example .env && nano .env && sh setup.sh
#   Обновление:  sh setup.sh
set -e
cd "$(dirname "$0")"

echo "== Docker"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "ОШИБКА: нет docker compose"; exit 1; }

echo "== Настройки"
[ -f .env ] || { echo "НЕТ файла .env — скопируйте .env.example в .env и заполните"; exit 1; }
sed -i 's/\r$//' .env
chmod 600 .env
. ./.env
for v in MONGO_ROOT_USER MONGO_ROOT_PASSWORD MONGO_APP_PASSWORD MONGO_BOT_PASSWORD QDRANT_API_KEY QDRANT_READ_KEY; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || { echo "ОШИБКА: в .env не задан $v"; exit 1; }
done
for v in MONGO_ROOT_PASSWORD MONGO_APP_PASSWORD MONGO_BOT_PASSWORD; do
    eval "val=\${$v}"
    case "$val" in
        *[!A-Za-z0-9]*) echo "ОШИБКА: $v — только латиница и цифры (openssl rand -hex 24)"; exit 1 ;;
    esac
done
[ "$QDRANT_API_KEY" != "$QDRANT_READ_KEY" ] || { echo "ОШИБКА: QDRANT_READ_KEY должен отличаться от QDRANT_API_KEY"; exit 1; }

echo "== WireGuard"
wg_ip="${WG_IP:-10.8.0.1}"
if ! ip -4 addr show 2>/dev/null | grep -q "inet $wg_ip/"; then
    echo "ОШИБКА: на сервере нет адреса $wg_ip — сначала: sh ../wireguard.sh db <IP сервера приложения>"
    exit 1
fi

echo "== Запуск"
mkdir -p data/mongo data/qdrant
docker compose up -d
docker compose ps
echo
echo "Готово. Базы слушают только $wg_ip (канал WireGuard), из интернета не видны."
echo "Проверка с сервера приложения: nc -zv $wg_ip 27017 && nc -zv $wg_ip 6333"
