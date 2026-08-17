"""Подключение к Google API от имени пользователя.

Агент никогда не использует сервисный аккаунт с расширенными правами: он
работает по OAuth-токену конкретного пользователя, поэтому видит ровно те
документы и календари, к которым у пользователя есть доступ.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import IntegrationUnavailable

try:  # Google-библиотеки опциональны: без них агент запускается, но интеграции выключены.
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_LIBS_AVAILABLE = True
    GOOGLE_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover — зависит от окружения
    GOOGLE_LIBS_AVAILABLE = False
    GOOGLE_IMPORT_ERROR = str(exc)
    AuthorizedSession = Request = Credentials = build = None  # type: ignore[assignment]

    class HttpError(Exception):  # type: ignore[no-redef]
        """Заглушка, чтобы except-блоки в инструментах оставались валидными."""

        resp: Any = None


_lock = threading.Lock()
_cached: Any = None

SETUP_HINT = (
    "Интеграция с Google не подключена. Чтобы включить её, выполните в каталоге проекта: "
    "1) положите client_secret.json (OAuth client ID типа Desktop app) в credentials/; "
    "2) запустите `python -m app.integrations.google_auth`; "
    "3) подтвердите доступ в браузере под нужной учётной записью."
)


def _load_credentials() -> Any:
    global _cached

    if not GOOGLE_LIBS_AVAILABLE:
        raise IntegrationUnavailable(
            "Библиотеки Google API не установлены "
            f"({GOOGLE_IMPORT_ERROR}). Установите зависимости: pip install -r requirements.txt"
        )

    with _lock:
        if _cached is not None and _cached.valid:
            return _cached

        token_path: Path = settings.google_token_path
        if not token_path.exists():
            raise IntegrationUnavailable(SETUP_HINT)

        try:
            creds = Credentials.from_authorized_user_file(str(token_path), list(settings.google_scopes))
        except (ValueError, json.JSONDecodeError) as exc:
            raise IntegrationUnavailable(
                f"Файл токена Google повреждён ({exc}). Пройдите авторизацию заново: "
                "python -m app.integrations.google_auth"
            ) from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    token_path.write_text(creds.to_json(), encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    raise IntegrationUnavailable(
                        f"Не удалось обновить токен Google ({exc}). Пройдите авторизацию заново: "
                        "python -m app.integrations.google_auth"
                    ) from exc
            else:
                raise IntegrationUnavailable(
                    "Токен Google недействителен и не может быть обновлён. "
                    "Пройдите авторизацию заново: python -m app.integrations.google_auth"
                )

        _cached = creds
        return creds


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None


def get_service(api: str, version: str) -> Any:
    creds = _load_credentials()
    return build(api, version, credentials=creds, cache_discovery=False)


def authorized_session() -> Any:
    return AuthorizedSession(_load_credentials())


def describe_http_error(exc: Any, context: str) -> str:
    """Переводит ошибку Google API в понятное объяснение для модели и пользователя."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = ""
    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
            detail = payload.get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
    if status == 401:
        return f"{context}: Google отклонил токен (401). Требуется повторная авторизация. {detail}"
    if status == 403:
        return (
            f"{context}: доступ запрещён (403). У текущей учётной записи нет прав на этот "
            f"объект, либо не выданы нужные OAuth-разрешения. {detail}"
        )
    if status == 404:
        return f"{context}: объект не найден (404) или недоступен этой учётной записи. {detail}"
    return f"{context}: ошибка Google API {status or ''}. {detail or exc}".strip()


def status() -> dict[str, Any]:
    """Состояние интеграции — показывается в интерфейсе."""
    if not GOOGLE_LIBS_AVAILABLE:
        return {
            "connected": False,
            "reason": "Библиотеки Google API не установлены",
            "hint": "pip install -r requirements.txt",
        }
    if not settings.google_token_path.exists():
        return {
            "connected": False,
            "reason": "Нет OAuth-токена пользователя",
            "hint": SETUP_HINT,
            "client_secret_present": settings.google_client_secret_path.exists(),
        }
    try:
        creds = _load_credentials()
    except IntegrationUnavailable as exc:
        return {"connected": False, "reason": str(exc), "hint": SETUP_HINT}
    return {
        "connected": True,
        "scopes": list(getattr(creds, "scopes", []) or settings.google_scopes),
        "account_hint": _account_email(),
    }


def _account_email() -> str:
    try:
        service = get_service("oauth2", "v2")
        info = service.userinfo().get().execute()
        return info.get("email", "")
    except Exception:  # noqa: BLE001 — необязательная информация
        return ""
