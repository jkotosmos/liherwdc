"""Кто может писать боту: список из настроек плюс добавленные командой.

Основной список — TELEGRAM_ALLOWED_USERS. Но правка переменной требует панели
хостинга и перезапуска, а на практике значение может и не доехать до
контейнера. Поэтому тот, кто уже в основном списке, может добавить человека
прямо из бота: /adduser <ID>. Добавленные хранятся на постоянном диске и
переживают передеплой.

Добавлять и удалять могут только люди из основного списка — добавленный
командой не может раздавать доступ дальше.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import settings
from ..storage import read_json, update_json


def _path():
    return settings.data_dir / "telegram_users.json"


def admins() -> frozenset[int]:
    """Основной список из настроек — им можно управлять доступом."""
    return settings.telegram_allowed_users


def extra() -> dict[int, dict[str, Any]]:
    data = read_json(_path(), {})
    users = data.get("users") if isinstance(data, dict) else None
    result: dict[int, dict[str, Any]] = {}
    for key, meta in (users or {}).items():
        if str(key).lstrip("-").isdigit():
            result[int(key)] = meta if isinstance(meta, dict) else {}
    return result


def allowed() -> frozenset[int]:
    return admins() | frozenset(extra())


def add(user_id: int, added_by: int) -> bool:
    """True — добавлен, False — уже был."""
    if user_id in allowed():
        return False

    def mutate(data: dict[str, Any]) -> None:
        data.setdefault("users", {})[str(user_id)] = {
            "added_by": added_by,
            "added_at": datetime.now(settings.tz).isoformat(timespec="seconds"),
        }

    update_json(_path(), {"users": {}}, mutate)
    return True


def remove(user_id: int) -> bool:
    """True — удалён. Основной список командой не меняется."""
    if user_id not in extra():
        return False

    def mutate(data: dict[str, Any]) -> None:
        data.setdefault("users", {}).pop(str(user_id), None)

    update_json(_path(), {"users": {}}, mutate)
    return True
