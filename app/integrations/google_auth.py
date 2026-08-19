"""Разовая OAuth-авторизация в Google.

Запуск:  python -m app.integrations.google_auth

Открывает браузер, просит подтвердить доступ и сохраняет токен пользователя в
credentials/google_token.json. Токен привязан к учётной записи, под которой
подтверждён доступ, — агент не получает прав больше, чем есть у пользователя.
"""

from __future__ import annotations

import sys

from ..config import settings


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Не установлены библиотеки Google. Выполните: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    secret_path = settings.google_client_secret_path
    if not secret_path.exists():
        print(
            "Не найден файл OAuth-клиента: "
            f"{secret_path}\n\n"
            "Как получить:\n"
            "  1. console.cloud.google.com → создайте (или выберите) проект;\n"
            "  2. включите Google Drive API и Google Calendar API;\n"
            "  3. APIs & Services → Credentials → Create credentials → OAuth client ID;\n"
            "  4. тип приложения: Desktop app;\n"
            f"  5. скачайте JSON и сохраните как {secret_path}",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), list(settings.google_scopes))
    creds = flow.run_local_server(port=0, prompt="consent")

    from . import token_store

    path = token_store.save_token(creds.to_json())

    print(f"Токен сохранён: {path}")
    if token_store.encryption_enabled():
        print("Файл зашифрован (OPERON_TOKEN_KEY задан).")
        print("Для переноса на сервер скопируйте и файл токена, и token_salt.bin,")
        print("и задайте там ту же OPERON_TOKEN_KEY.")
    else:
        print("ВНИМАНИЕ: токен не зашифрован. Перед переносом на сервер задайте")
        print("OPERON_TOKEN_KEY и выполните авторизацию заново.")
    print("Выданные разрешения:")
    for scope in creds.scopes or []:
        print(f"  • {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
