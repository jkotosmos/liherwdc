"""Подключение к Google API от имени пользователя.

Агент никогда не использует сервисный аккаунт с расширенными правами: он
работает по OAuth-токену конкретного пользователя, поэтому видит ровно те
документы и календари, к которым у пользователя есть доступ.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from ..config import settings
from ..errors import IntegrationUnavailable
from . import token_store

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
    "Интеграция с Google не подключена. Владелец бота должен отправить боту /auth "
    "и разрешить доступ под нужной учётной записью Google (нужны заданные "
    "GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET)."
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

        try:
            payload = token_store.load_token()
        except token_store.TokenDecryptionError as exc:
            raise IntegrationUnavailable(str(exc)) from exc
        if payload is None:
            raise IntegrationUnavailable(SETUP_HINT)

        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(payload), list(settings.google_scopes)
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise IntegrationUnavailable(
                f"Файл токена Google повреждён ({exc}). Пройдите авторизацию заново: "
                "python -m app.integrations.google_auth"
            ) from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    token_store.save_token(creds.to_json())
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
    disabled = api_disabled_message(detail)
    if disabled:
        return f"{context}: {disabled}"
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


_ACTIVATION_URL = re.compile(r"https://console\.(?:developers|cloud)\.google\.com/\S+")
_API_NAME = re.compile(r"([A-Za-z ]+ API) has not been used|([a-z]+)\.googleapis\.com")


def api_disabled_message(detail: str) -> str:
    """Отказ «API не включён в проекте» — самая частая ошибка первого запуска.

    Google отвечает на него 403, и без этой проверки человек видит «нет прав»
    и идёт перевыдавать доступ, хотя нужно нажать «Enable» в консоли.
    """
    text = detail or ""
    if "has not been used in project" not in text and "it is disabled" not in text:
        return ""
    name_match = _API_NAME.search(text)
    name = ""
    if name_match:
        name = name_match.group(1) or f"{name_match.group(2)}.googleapis.com"
    url_match = _ACTIVATION_URL.search(text)
    url = url_match.group(0).rstrip(".,)") if url_match else ""
    return (
        f"в проекте Google Cloud не включён {name or 'нужный API'}. "
        "Это настройка проекта, а не прав пользователя: включите API в консоли"
        + (f" ({url})" if url else " (APIs & Services → Library)")
        + ", подождите пару минут и повторите. Повторная авторизация не нужна."
    )


def status() -> dict[str, Any]:
    """Состояние интеграции — показывается в интерфейсе."""
    if not GOOGLE_LIBS_AVAILABLE:
        return {
            "connected": False,
            "reason": "Библиотеки Google API не установлены",
            "hint": "pip install -r requirements.txt",
        }
    if not token_store.token_exists():
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
        "token": token_store.describe(),
    }


def _account_email() -> str:
    # Адрес берём у Диска: userinfo требует отдельного разрешения (email),
    # которого в наборе нет, и раньше этот запрос молча возвращал пустоту.
    try:
        about = get_service("drive", "v3").about().get(fields="user(emailAddress)").execute()
        return (about.get("user") or {}).get("emailAddress", "")
    except Exception:  # noqa: BLE001 — необязательная информация
        return ""
