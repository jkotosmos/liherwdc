#!/usr/bin/env python3
"""Запуск ассистента OPERON.

    python run.py            # http://127.0.0.1:8000
    python run.py --reload   # режим разработки
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Деловой ИИ-ассистент OPERON")
    parser.add_argument("--host", default=None, help="Адрес прослушивания")
    parser.add_argument("--port", type=int, default=None, help="Порт")
    parser.add_argument("--reload", action="store_true", help="Автоперезапуск при изменении кода")
    args = parser.parse_args()

    from app.config import settings

    host = args.host or settings.host
    port = args.port or settings.port

    if not settings.api_key:
        variable = "OPENROUTER_API_KEY" if settings.provider == "openrouter" else "ANTHROPIC_API_KEY"
        print(
            f"Не задан ключ доступа к модели ({variable}).\n"
            "Укажите его в файле .env (см. .env.example) или в переменных окружения:\n"
            f"    export {variable}=...",
            file=sys.stderr,
        )
        return 1

    # На сервере вместе с приложением лежит OAuth-токен пользователя Google.
    # Открыть такой интерфейс наружу без пароля — отдать доступ к его Диску
    # и календарю всем, кто знает адрес.
    is_public = host not in {"127.0.0.1", "localhost", "::1"}
    if is_public and not settings.auth_required:
        print(
            "Отказ в запуске: приложение слушает внешний адрес "
            f"({host}), но пароль не задан.\n"
            "Задайте OPERON_ACCESS_PASSWORD — иначе доступ к вашим документам,\n"
            "календарю и бюджету API получит любой, кто узнает адрес.\n"
            "Для локальной работы без пароля используйте --host 127.0.0.1",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    uvicorn.run(
        "app.server:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
        # За обратным прокси Amvera важно доверять заголовкам X-Forwarded-*,
        # иначе схема определится как http и Secure-куки не установятся.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
