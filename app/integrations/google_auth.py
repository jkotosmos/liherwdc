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

    settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    settings.google_token_path.chmod(0o600)

    print(f"Токен сохранён: {settings.google_token_path}")
    print("Выданные разрешения:")
    for scope in creds.scopes or []:
        print(f"  • {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
