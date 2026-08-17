#!/usr/bin/env python3
"""Запуск ассистента OPERON.

    python run.py            # http://127.0.0.1:8000
    python run.py --reload   # режим разработки
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Деловой ИИ-ассистент OPERON")
    parser.add_argument("--host", default=None, help="Адрес прослушивания")
    parser.add_argument("--port", type=int, default=None, help="Порт")
    parser.add_argument("--reload", action="store_true", help="Автоперезапуск при изменении кода")
    args = parser.parse_args()

    from app.config import settings

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "Не задан ANTHROPIC_API_KEY.\n"
            "Укажите ключ в файле .env (см. .env.example) или в переменной окружения:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    uvicorn.run(
        "app.server:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
