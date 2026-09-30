"""Личные напоминания: «напомни завтра в 10:00 позвонить Иванову».

Календарь для этого не всегда подходит: не у каждого подключён Google, а
«позвонить» — не встреча. Поэтому бот хранит такие напоминания сам и присылает
их в Telegram в назначенное время (см. app/reminders.py).

ТЗ требует создавать напоминания только после подтверждения — создание и
отмена идут через карточку. Каждый видит и получает только свои напоминания.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..integrations import accounts
from ..storage import read_json, update_json
from .base import Preview, ToolError, ToolSpec, registry

MAX_TEXT = 1000
MAX_ACTIVE = 200
# Давно доставленные и отменённые не храним вечно.
KEEP_DAYS = 30


def _now() -> datetime:
    return datetime.now(settings.tz)


def _load() -> list[dict[str, Any]]:
    data = read_json(settings.personal_reminders_path, {"reminders": []})
    items = data.get("reminders") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _same_person(stored: str, account: str) -> bool:
    """Владелец в Telegram и веб-чат по паролю — один и тот же человек."""
    return accounts.resolve(stored) == accounts.resolve(account)


def _parse_moment(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise ToolError("Не указано время напоминания (at).")
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"Не разобрал время «{value}». Формат: YYYY-MM-DDTHH:MM.") from exc
    if len(raw) == 10:
        raise ToolError("Укажите не только дату, но и время: YYYY-MM-DDTHH:MM.")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=settings.tz)
    return moment


def _check(tool_input: dict[str, Any]) -> tuple[str, datetime]:
    text = (tool_input.get("text") or "").strip()
    if not text:
        raise ToolError("Не указано, о чём напомнить (text).")
    if len(text) > MAX_TEXT:
        raise ToolError(f"Текст напоминания длиннее {MAX_TEXT} символов — сократите.")
    moment = _parse_moment(tool_input.get("at") or "")
    if moment <= _now():
        raise ToolError(
            f"Время {moment.strftime('%d.%m.%Y %H:%M')} уже прошло. Уточните у пользователя дату и время."
        )
    return text, moment


def _view(item: dict[str, Any]) -> dict[str, Any]:
    moment = datetime.fromisoformat(item["at"])
    return {
        "reminder_id": item["id"],
        "text": item["text"],
        "at": moment.strftime("%Y-%m-%d %H:%M"),
        "status": item.get("status", "active"),
    }


def _create(tool_input: dict[str, Any]) -> Any:
    text, moment = _check(tool_input)
    account = accounts.current()

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        items = data.setdefault("reminders", [])
        active = [r for r in items if r.get("status") == "active" and _same_person(r.get("account", ""), account)]
        if len(active) >= MAX_ACTIVE:
            raise ToolError(f"Активных напоминаний уже {MAX_ACTIVE} — отмените ненужные.")
        item = {
            "id": "R-" + secrets.token_hex(3).upper(),
            "account": account,
            "text": text,
            "at": moment.isoformat(),
            "created": _now().isoformat(timespec="seconds"),
            "status": "active",
        }
        items.append(item)
        return item

    item = update_json(settings.personal_reminders_path, {"reminders": []}, mutate)
    return {
        "status": "created",
        "reminder": _view(item),
        "note": "Бот пришлёт напоминание в Telegram в указанное время.",
    }


def _list(tool_input: dict[str, Any]) -> Any:
    account = accounts.current()
    mine = [
        _view(r)
        for r in _load()
        if r.get("status") == "active" and _same_person(r.get("account", ""), account)
    ]
    mine.sort(key=lambda r: r["at"])
    if not mine:
        return {"status": "empty", "hint": "Активных напоминаний нет."}
    return {"status": "ok", "count": len(mine), "reminders": mine}


def _cancel(tool_input: dict[str, Any]) -> Any:
    reminder_id = (tool_input.get("reminder_id") or "").strip().upper()
    if not reminder_id:
        raise ToolError("Не указан reminder_id — сначала посмотрите список (reminder_list).")
    account = accounts.current()

    def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
        for item in data.setdefault("reminders", []):
            if item.get("id") == reminder_id and _same_person(item.get("account", ""), account):
                if item.get("status") != "active":
                    raise ToolError(f"Напоминание {reminder_id} уже не активно ({item.get('status')}).")
                item["status"] = "cancelled"
                return item
        return None

    item = update_json(settings.personal_reminders_path, {"reminders": []}, mutate)
    if item is None:
        raise ToolError(f"Напоминание {reminder_id} не найдено среди ваших.")
    return {"status": "cancelled", "reminder": _view(item)}


# --- для планировщика -------------------------------------------------------


def due(account: str, moment: datetime) -> list[dict[str, Any]]:
    """Наступившие напоминания этого человека."""
    result = []
    for item in _load():
        if item.get("status") != "active" or not _same_person(item.get("account", ""), account):
            continue
        try:
            when = datetime.fromisoformat(item["at"])
        except (KeyError, ValueError):
            continue
        if when <= moment:
            result.append(item)
    result.sort(key=lambda r: r["at"])
    return result


def mark_delivered(ids: list[str], moment: datetime) -> None:
    if not ids:
        return
    wanted = set(ids)
    horizon = moment - timedelta(days=KEEP_DAYS)

    def mutate(data: dict[str, Any]) -> None:
        kept = []
        for item in data.setdefault("reminders", []):
            if item.get("id") in wanted:
                item["status"] = "sent"
                item["sent"] = moment.isoformat(timespec="seconds")
            if item.get("status") != "active":
                try:
                    if datetime.fromisoformat(item["at"]) < horizon:
                        continue
                except (KeyError, ValueError):
                    continue
            kept.append(item)
        data["reminders"] = kept

    update_json(settings.personal_reminders_path, {"reminders": []}, mutate)


# --- карточки подтверждения -------------------------------------------------


def _preview_create(tool_input: dict[str, Any]) -> Preview:
    at = tool_input.get("at", "?")
    try:
        at = _parse_moment(at).strftime("%d.%m.%Y %H:%M")
    except ToolError:
        pass
    return Preview(
        title="Создать напоминание",
        summary=f"{at} бот напишет в Telegram: «{tool_input.get('text', '')}»",
        details={"Когда": at, "Текст": tool_input.get("text", "")},
    )


def _preview_cancel(tool_input: dict[str, Any]) -> Preview:
    reminder_id = (tool_input.get("reminder_id") or "?").strip().upper()
    details: dict[str, Any] = {"Напоминание": reminder_id}
    for item in _load():
        if item.get("id") == reminder_id:
            details.update({"Текст": item.get("text", ""), "Когда": _view(item)["at"]})
            break
    return Preview(
        title="Отменить напоминание",
        summary=f"Напоминание {reminder_id}" + (f" «{details['Текст']}»" if "Текст" in details else "") + " не придёт.",
        details=details,
    )


registry.register(
    ToolSpec(
        name="reminder_create",
        description=(
            "Ставит личное напоминание: в указанное время бот сам напишет пользователю в Telegram. "
            "Для «напомни мне…», «не дай забыть…» — когда это не встреча. Для встречи используй "
            "calendar_create_event (о ней бот тоже напомнит заранее). Время — абсолютное, считай его "
            "от текущей даты. Не знаешь время — спроси. Создаётся только после подтверждения."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "О чём напомнить."},
                "at": {"type": "string", "description": "Когда: YYYY-MM-DDTHH:MM."},
            },
            "required": ["text", "at"],
        },
        handler=_create,
        requires_confirmation=True,
        preview=_preview_create,
        activity="Ставлю напоминание",
    )
)

registry.register(
    ToolSpec(
        name="reminder_list",
        description="Показывает активные личные напоминания пользователя: что и когда бот пришлёт.",
        input_schema={"type": "object", "properties": {}},
        handler=_list,
        activity="Смотрю напоминания",
    )
)

registry.register(
    ToolSpec(
        name="reminder_cancel",
        description=(
            "Отменяет (удаляет) личное напоминание по reminder_id из reminder_list. "
            "Только после подтверждения."
        ),
        input_schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "string", "description": "Идентификатор вида R-1A2B3C."}},
            "required": ["reminder_id"],
        },
        handler=_cancel,
        requires_confirmation=True,
        preview=_preview_cancel,
        activity="Отменяю напоминание",
    )
)
