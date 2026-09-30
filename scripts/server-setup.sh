#!/bin/sh
# Установка бота на Linux-сервере или виртуальном хостинге (SpaceWeb и т. п.).
# Запуск с компьютера: ssh <логин>@<сервер> "sh operon/scripts/server-setup.sh"
# Перед этим положите настройки: scp .env <логин>@<сервер>:operon/.env
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1

echo "== Обновляю код"
git pull --ff-only 2>&1 | tail -n 1

echo "== Ищу Python 3.10+"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ОШИБКА: нужен Python 3.10 или новее. Найдено: $(ls /usr/bin/python3* /usr/local/bin/python3* 2>/dev/null | tr '\n' ' ')"
    exit 1
fi
echo "Python: $("$PY" --version 2>&1)"

echo "== Окружение и зависимости (несколько минут при первом запуске)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv || exit 1
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1

echo "== Настройки"
if [ ! -f .env ]; then
    echo "ОШИБКА: нет файла .env. С компьютера: scp .env <логин>@<сервер>:operon/.env"
    exit 1
fi
# Файл из Windows: переводы строк CRLF.
sed -i 's/\r$//' .env
# На общем хостинге порт 8000 может быть занят другими клиентами.
if grep -q '^OPERON_PORT=' .env; then
    sed -i 's/^OPERON_PORT=.*/OPERON_PORT=38417/' .env
else
    echo 'OPERON_PORT=38417' >> .env
fi
grep -q '^OPERON_HOST=' .env || echo 'OPERON_HOST=127.0.0.1' >> .env

echo "== Самопроверка"
.venv/bin/python -m app.selfcheck
echo
echo "Дальше: ssh <логин>@<сервер> \"sh operon/scripts/install-cron.sh\""
