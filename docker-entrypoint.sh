#!/bin/sh
# Готовит постоянный диск и запускает приложение.
set -e

mkdir -p "$OPERON_DATA_DIR" "$OPERON_CREDENTIALS_DIR" "$OPERON_KB_DIR"

# База знаний живёт на постоянном диске, чтобы её можно было пополнять
# через файловый менеджер Amvera без пересборки. При первом запуске
# переносим туда документы из репозитория, если диск ещё пуст.
if [ -d /app/knowledge_base ] && [ -z "$(ls -A "$OPERON_KB_DIR" 2>/dev/null)" ]; then
    echo "Постоянный диск пуст — переношу базу знаний из репозитория в $OPERON_KB_DIR"
    cp -r /app/knowledge_base/. "$OPERON_KB_DIR"/ 2>/dev/null || true
fi

echo "Документов на диске: $(find "$OPERON_KB_DIR" -type f 2>/dev/null | wc -l)"

exec python run.py
