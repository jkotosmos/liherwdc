#!/bin/sh
# Держит бота запущенным на виртуальном хостинге (SpaceWeb и подобные).
# Хостинг может останавливать долгие процессы — cron раз в минуту вызывает
# этот скрипт, и он поднимает бота, если тот не работает.
#   crontab:  * * * * * $HOME/operon/scripts/keepalive.sh
# Журнал: ~/operon/bot.log. Остановить: ~/operon/scripts/keepalive.sh stop
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/bot.pid"

running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

if [ "$1" = "stop" ]; then
    running && kill "$(cat "$PIDFILE")" && echo "Остановлен"
    rm -f "$PIDFILE"
    exit 0
fi

if running; then
    exit 0
fi

cd "$DIR" || exit 1
nohup "$DIR/.venv/bin/python" "$DIR/run.py" >> "$DIR/bot.log" 2>&1 &
echo $! > "$PIDFILE"
echo "$(date '+%F %T') запущен, pid $(cat "$PIDFILE")" >> "$DIR/bot.log"
