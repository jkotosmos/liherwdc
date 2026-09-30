#!/bin/sh
# Запускает бота и ставит автоперезапуск раз в минуту (cron).
# Запуск с компьютера: ssh <логин>@<сервер> "sh operon/scripts/install-cron.sh"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LINE="* * * * * $DIR/scripts/keepalive.sh"

chmod +x "$DIR/scripts/keepalive.sh"
if command -v crontab >/dev/null 2>&1 \
    && { crontab -l 2>/dev/null | grep -v 'scripts/keepalive.sh'; echo "$LINE"; } | crontab - 2>/dev/null; then
    echo "Автоперезапуск включён (cron)."
else
    echo "crontab недоступен — добавьте строку в панели хостинга, раздел «Crontab»:"
    echo "$LINE"
fi

"$DIR/scripts/keepalive.sh"
echo "Жду запуска…"
sleep 10
echo "== Последние строки журнала ($DIR/bot.log)"
tail -n 15 "$DIR/bot.log"
