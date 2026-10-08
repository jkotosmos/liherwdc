"""Google через сервисный аккаунт — вход без OAuth-экранов и адресов возврата.

OAuth требует точной настройки в Google Cloud (тип клиента, адреса возврата,
экран согласия, публикация), и любая неточность — «Доступ заблокирован» у
заказчика. Сервисный аккаунт — технический пользователь бота: ключ кладётся
на сервер один раз, дальше ничего не истекает и не требует подтверждений.

Два режима, бот выбирает сам при подключении:

* **Делегирование** (аккаунт в организации Google Workspace). Администратор
  организации один раз разрешает сервисному аккаунту действовать от имени
  сотрудников (Admin console → Security → API controls → Domain-wide
  delegation). Человеку делать ничего не нужно: бот работает с его основным
  календарём и Диском как он сам — приглашения участникам тоже уходят.
* **Общий доступ** (личный Gmail). Человек открывает свой календарь адресу
  сервисного аккаунта, как коллеге («Вносить изменения в мероприятия»), и
  нужные папки Диска. Бот видит только открытое. Приглашения участникам от
  сервисного аккаунта Google не рассылает — участники пишутся в описание.

Чей Google — по-прежнему по пользователю Telegram (integrations/accounts.py):
каждый подключает свою почту, чужая не подставляется.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..storage import read_json, update_json
from . import accounts

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)

_lock = threading.Lock()
_preset_failed: dict[str, float] = {}


class LinkError(Exception):
    """Текст пригоден для показа пользователю."""


# --- ключ ----------------------------------------------------------------------


def _key_path() -> Path | None:
    """Где лежит ключ. Скачанный из Google файл можно положить в /data как есть,
    не переименовывая: ищем любой JSON с type=service_account."""
    explicit = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    named = settings.credentials_dir / "service_account.json"
    if named.is_file():
        return named
    from ..config import PERSIST_DIR

    for folder in (settings.credentials_dir, PERSIST_DIR):
        try:
            candidates = sorted(folder.glob("*.json"))
        except OSError:
            continue
        for path in candidates:
            if _read_key(path) is not None:
                return path
    return None


def _read_key(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 20_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("type") == "service_account" else None


def info() -> dict[str, Any] | None:
    """Ключ сервисного аккаунта: переменная (JSON или base64) или файл на диске."""
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw:
        if not raw.startswith("{"):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("type") == "service_account" else None
    path = _key_path()
    return _read_key(path) if path is not None and path.is_file() else None


def enabled() -> bool:
    return info() is not None


def problem() -> str:
    """Почему ключ задан, но не читается — для самопроверки."""
    if (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip() and info() is None:
        return (
            "GOOGLE_SERVICE_ACCOUNT_JSON задан, но это не ключ сервисного аккаунта: вставьте "
            "содержимое скачанного JSON-файла целиком (или его base64)."
        )
    return ""


def email() -> str:
    data = info() or {}
    return str(data.get("client_email", ""))


def client_id() -> str:
    data = info() or {}
    return str(data.get("client_id", ""))


def credentials(link: dict[str, Any]) -> Any:
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(info(), scopes=list(SCOPES))
    if link.get("mode") == "delegated":
        creds = creds.with_subject(link["email"])
    return creds


# --- чья почта у какого пользователя -----------------------------------------


def _links_path() -> Path:
    return settings.data_dir / "google_links.json"


def _preset() -> dict[str, str]:
    """GOOGLE_ACCOUNTS=1107365044=kirill@firma.ru,8058569481=me@gmail.com — заранее, без команд."""
    result: dict[str, str] = {}
    for part in (os.getenv("GOOGLE_ACCOUNTS") or "").replace(";", ",").split(","):
        if "=" in part:
            user, _, mail = part.partition("=")
            if user.strip() and "@" in mail:
                result[accounts.resolve(user.strip())] = mail.strip().lower()
    return result


def current_link() -> dict[str, Any] | None:
    key = accounts.resolve()
    stored = read_json(_links_path(), {})
    link = stored.get(key) if isinstance(stored, dict) else None
    if isinstance(link, dict) and link.get("email"):
        return link
    preset = _preset().get(key)
    if preset:
        # Не получилось — не стучимся в Google на каждом сообщении: повтор через 10 мин.
        if time.monotonic() - _preset_failed.get(key, -1e9) < 600:
            return None
        try:
            return connect(preset)
        except Exception:  # noqa: BLE001 — LinkError или сеть: подключим позже
            _preset_failed[key] = time.monotonic()
            return None
    return None


def forget() -> None:
    key = accounts.resolve()
    update_json(_links_path(), {}, lambda data: data.pop(key, None))


def _save(link: dict[str, Any]) -> None:
    key = accounts.resolve()

    def mutate(data: dict[str, Any]) -> None:
        data[key] = link

    update_json(_links_path(), {}, mutate)


# --- подключение ---------------------------------------------------------------


def _calendar(creds: Any) -> Any:
    from googleapiclient.discovery import build

    from ..net import httplib2_http
    import google_auth_httplib2

    return build("calendar", "v3", http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2_http()),
                 cache_discovery=False)


def _try_delegated(mail: str) -> bool:
    """Получится ли действовать от имени человека (делегирование в Workspace)."""
    try:
        creds = credentials({"mode": "delegated", "email": mail})
        _calendar(creds).calendarList().get(calendarId="primary").execute()
        return True
    except Exception:  # noqa: BLE001 — нет делегирования: unauthorized_client / invalid_grant
        return False


def _shared_access(mail: str) -> str:
    """Какие права дал человек сервисному аккаунту на свой календарь.

    events.list отдаёт accessRole самого календаря — хватает узких scope'ов,
    в отличие от calendarList.insert, которому нужен полный доступ к календарю.
    """
    from googleapiclient.errors import HttpError

    service = _calendar(credentials({"mode": "shared"}))
    try:
        result = service.events().list(calendarId=mail, maxResults=1).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            return ""
        raise LinkError(f"Google ответил ошибкой при проверке календаря: {exc}") from exc
    return str(result.get("accessRole", "") or "reader")


def instructions(mail: str = "") -> str:
    sa = email()
    who = mail or "ваша почта"
    return (
        "Подключение Google — один из двух способов.\n\n"
        "▶ Аккаунт организации (Google Workspace) — делать ничего не нужно, если "
        "администратор один раз разрешил делегирование: admin.google.com → Безопасность → "
        "Управление API → Делегирование в масштабе домена → Добавить: "
        f"ID клиента {client_id()}, области:\n{','.join(SCOPES)}\n\n"
        "▶ Личный Gmail — откройте доступ боту, как коллеге:\n"
        "1. calendar.google.com (в браузере) → слева у своего календаря ⋮ → «Настройки и общий доступ».\n"
        f"2. «Открыть доступ пользователям» → добавить {sa} → «Вносить изменения в мероприятия».\n"
        f"3. Для документов: на Google Диске откройте нужные папки для {sa} (читатель).\n\n"
        f"Затем отправьте: /auth {who}"
    )


def connect(mail: str) -> dict[str, Any]:
    """Подключает почту текущего пользователя. Возвращает описание связи."""
    mail = (mail or "").strip().lower()
    if "@" not in mail or " " in mail:
        raise LinkError("Нужен адрес почты Google, например: /auth kirill@gmail.com")
    if not enabled():
        raise LinkError("Ключ сервисного аккаунта не задан на сервере.")

    with _lock:
        if _try_delegated(mail):
            link = {"email": mail, "mode": "delegated", "access": "owner"}
        else:
            access = _shared_access(mail)
            if not access or access == "freeBusyReader":
                raise LinkError(
                    f"Нет доступа к календарю {mail}. "
                    + ("Открыт только «свободен/занят» — нужно «Вносить изменения в мероприятия».\n\n"
                       if access == "freeBusyReader" else "\n\n")
                    + instructions(mail)
                )
            link = {"email": mail, "mode": "shared", "access": access}
        _save(link)
    return link


def describe_link(link: dict[str, Any]) -> str:
    if link.get("mode") == "delegated":
        return f"{link['email']} — от имени пользователя (делегирование организации)"
    rights = {"owner": "полный", "writer": "чтение и изменение", "reader": "только чтение"}.get(
        link.get("access", ""), link.get("access", "")
    )
    return f"{link['email']} — открытый боту календарь ({rights})"
