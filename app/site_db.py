"""База сайта базы знаний (MongoDB): роли пользователей агента и даты документов.

ТЗ: бот отвечает «только в рамках прав пользователя». Права к документам
«Кванта» назначают на сайте: у каждого документа список ролей
(metadata.accessibleByRoles), у каждого пользователя агента — роль. Бот
читает роль человека из той же базы, что и сайт, и ищет в «Кванте» только
среди документов этой роли — так же, как бот самого сайта.

Отсюда же — даты документов: в «Кванте» лежит только текст, а когда документ
менялся, знает сайт. ТЗ требует указывать дату источника.

Бот в эту базу ничего не пишет. Подключение — OPERON_MONGODB_URI (на
сервере Timeweb: mongodb://mongo:27017/operon). Не задано — всё как раньше:
роли из QDRANT_ROLES или без фильтра.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .integrations import accounts

logger = logging.getLogger(__name__)

ROLE_TTL = 60  # роль меняют на сайте — подхватываем за минуту
STALE_TTL = 3600  # база недоступна — час работаем по последней известной роли
DATES_TTL = 300
TIMEOUT_MS = 5000

_OBJECT_ID = re.compile(r"[0-9a-f]{24}")
_lock = threading.Lock()
_client: Any = None
_roles: dict[str, tuple[float, "Access"]] = {}
_dates: dict[str, tuple[float, dict[str, str]]] = {}


class SiteDBError(Exception):
    """Текст пригоден для показа пользователю."""


@dataclass(frozen=True)
class Access:
    # role — роль есть; blocked — заблокирован на сайте; no_user — нет среди
    # пользователей агента; no_role — роль ещё не назначена; error — база недоступна.
    state: str
    role_id: str = ""
    role_name: str = ""
    detail: str = ""


def uri() -> str:
    return (os.getenv("OPERON_MONGODB_URI") or "").strip()


def enabled() -> bool:
    return bool(uri())


def _db() -> Any:
    global _client
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover — пакет в requirements
        raise SiteDBError("Не установлен пакет pymongo.") from exc
    with _lock:
        if _client is None:
            _client = MongoClient(
                uri(),
                serverSelectionTimeoutMS=TIMEOUT_MS,
                connectTimeoutMS=TIMEOUT_MS,
                socketTimeoutMS=TIMEOUT_MS * 2,
                appname="operon-bot",
            )
        client = _client
    # Как у mongoose: база из адреса, без неё — «test».
    name = (os.getenv("OPERON_MONGODB_DB") or "").strip()
    return client[name] if name else client.get_default_database(default="test")


def reset() -> None:
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None
        _roles.clear()
        _dates.clear()


def _problem(exc: Exception) -> str:
    # У pymongo после причины идёт длинное описание топологии — оно для логов.
    text = str(exc).split(", Timeout:")[0].split(" (configured timeouts")[0]
    return f"База сайта недоступна: {type(exc).__name__}: {text[:160]}"


def _object_id(value: str) -> Any:
    from bson import ObjectId

    return ObjectId(value)


# --- роль пользователя ---------------------------------------------------------


def telegram_id(account: str | None = None) -> str:
    """Кто спрашивает: ID в Telegram. Веб-чат по паролю — это владелец."""
    key = accounts.current() if account is None else str(account or "").strip()
    return key if key.lstrip("-").isdigit() else accounts.owner_id()


def _lookup(user: str) -> Access:
    try:
        db = _db()  # неверный адрес — ошибка уже здесь
        found = db.agentusers.find_one({"chatIdTG": user}, {"role": 1, "status": 1})
        if not found:
            return Access("no_user")
        if found.get("status") == "blocked":
            return Access("blocked")
        role = found.get("role")
        if not role:
            return Access("no_role")
        named = db.agentroles.find_one({"_id": role}, {"name": 1}) or {}
    except Exception as exc:  # noqa: BLE001 — сеть, авторизация, таймаут
        raise SiteDBError(_problem(exc)) from exc
    return Access("role", role_id=str(role), role_name=str(named.get("name", "")))


def access(account: str | None = None) -> Access:
    """Роль текущего пользователя на сайте (с кэшем на минуту)."""
    user = telegram_id(account)
    if not user:
        return Access("no_user", detail="не известен Telegram ID")
    now = time.monotonic()
    cached = _roles.get(user)
    if cached and now - cached[0] < ROLE_TTL:
        return cached[1]
    try:
        result = _lookup(user)
    except SiteDBError as exc:
        logger.warning("%s", exc)
        if cached and cached[1].state != "error" and now - cached[0] < STALE_TTL:
            return cached[1]
        return Access("error", detail=str(exc))
    _roles[user] = (now, result)
    return result


def explain(found: Access, user: str) -> str:
    """Почему документы «Кванта» не показаны — для пользователя."""
    if found.state == "blocked":
        return "Ваш аккаунт заблокирован на сайте базы знаний — документы «Кванта» недоступны."
    if found.state == "error":
        # Подробности — в журнале и в /check; пользователю — суть.
        return "Не удалось проверить ваши права: сайт базы знаний сейчас не отвечает. Попробуйте позже."
    who = f"Telegram ID {user}" if user else "ваш Telegram ID"
    if found.state == "no_role":
        return (
            f"На сайте базы знаний вам ({who}) ещё не назначена роль — документы «Кванта» "
            "недоступны. Попросите администратора назначить роль в разделе пользователей агента."
        )
    return (
        f"Вас ({who}) нет среди пользователей агента на сайте базы знаний — документы «Кванта» "
        "недоступны. Зарегистрируйтесь в боте сайта (он попросит номер телефона), затем "
        "администратор назначит роль."
    )


# --- даты документов -----------------------------------------------------------


def _day(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # MongoDB хранит UTC
    return value.astimezone(settings.tz).strftime("%d.%m.%Y")


TOPIC_STATUS = {"approved": "утверждена", "review": "на проверке", "archived": "в архиве"}


def _file_info(doc: dict[str, Any]) -> dict[str, str]:
    google = doc.get("google") or {}
    if _day(google.get("modifiedTime")):
        return {"date": _day(google["modifiedTime"]), "date_meaning": "изменён в Google Drive"}
    if _day(doc.get("createdAt")):
        return {"date": _day(doc["createdAt"]), "date_meaning": "загружен на сайт"}
    return {}


def _topic_info(doc: dict[str, Any]) -> dict[str, str]:
    info: dict[str, str] = {}
    if _day(doc.get("updatedAt")):
        info.update(date=_day(doc["updatedAt"]), date_meaning="обновлена на сайте")
    status = TOPIC_STATUS.get(str(doc.get("status", "")))
    if status:
        info["topic_status"] = status
    return info


def _fetch(collection: str, ids: list[str], fields: dict[str, int], convert) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    if not ids:
        return result
    cursor = _db()[collection].find({"_id": {"$in": [_object_id(i) for i in ids]}}, fields)
    for doc in cursor:
        result[str(doc["_id"])] = convert(doc)
    return result


def add_dates(results: list[dict[str, Any]]) -> str:
    """Дописывает к фрагментам «Кванта» дату документа с сайта. Возвращает предупреждение или ""."""
    now = time.monotonic()
    wanted: dict[str, set[str]] = {"knowledgefiles": set(), "topics": set()}
    for item in results:
        for key, collection in (("file_id", "knowledgefiles"), ("topic_id", "topics")):
            value = str(item.get(key) or "")
            if _OBJECT_ID.fullmatch(value):
                cached = _dates.get(value)
                if not cached or now - cached[0] >= DATES_TTL:
                    wanted[collection].add(value)
    warning = ""
    if any(wanted.values()):
        try:
            found = _fetch("knowledgefiles", sorted(wanted["knowledgefiles"]),
                           {"google.modifiedTime": 1, "createdAt": 1}, _file_info)
            found |= _fetch("topics", sorted(wanted["topics"]), {"updatedAt": 1, "status": 1}, _topic_info)
        except Exception as exc:  # noqa: BLE001 — без дат ответ всё равно полезен
            logger.warning("Даты документов с сайта не получены: %s", exc)
            warning = "Даты документов с сайта сейчас недоступны."
        else:
            for value in wanted["knowledgefiles"] | wanted["topics"]:
                _dates[value] = (now, found.get(value, {}))
    for item in results:
        for key in ("file_id", "topic_id"):
            info = (_dates.get(str(item.get(key) or "")) or (0, {}))[1]
            if info:
                item.update(info)
                break
    return warning


# --- самопроверка -------------------------------------------------------------


def check() -> tuple[bool, str]:
    """Доступна ли база и сколько в ней пользователей агента и ролей."""
    try:
        db = _db()
        users = db.agentusers.count_documents({})
        with_role = db.agentusers.count_documents({"role": {"$ne": None}, "status": {"$ne": "blocked"}})
        roles = db.agentroles.count_documents({})
    except SiteDBError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, _problem(exc)
    return True, f"пользователей агента {users} (с ролью {with_role}), ролей {roles}"
