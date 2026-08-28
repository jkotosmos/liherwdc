"""Протокол встречи: разбор на структуру и постановка поручений одним действием.

ТЗ требует «готовить протоколы встреч и списки следующих действий». Половина
этого закрывается промптом: модель умеет пересказать расшифровку в разделы.
Но список действий, оставшийся текстом в чате, назавтра исчезает — а ТЗ
рядом требует «фиксировать поручения и контролировать сроки».

Поэтому здесь протокол — не текст, а операция: разобранные поручения попадают
в реестр, откуда их видит и контроль сроков, и утренняя сводка. Одно
подтверждение на весь протокол, а не по одному на каждое поручение: пять
одинаковых карточек подряд перестают читать, и смысл подтверждения теряется.

Разбор расшифровки на структуру делает модель — это её работа. Проверка
структуры, запись и связь с реестром — здесь, потому что молча потерянное
поручение хуже, чем ненайденное.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..config import settings
from ..storage import read_json, update_json
from .base import Preview, ToolError, ToolSpec, registry
from .tasks import _parse_date, _decorate


def _now() -> str:
    return datetime.now(settings.tz).isoformat(timespec="seconds")


def _protocols() -> list[dict[str, Any]]:
    data = read_json(settings.protocols_path, {"protocols": []})
    return data.get("protocols", []) if isinstance(data, dict) else []


def _as_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolError(f"Поле {field}: ожидается список.")
    return value


def _clean_actions(raw: Any) -> list[dict[str, Any]]:
    """Проверяет поручения до записи: без ответственного и срока контроль невозможен."""
    actions = []
    for index, item in enumerate(_as_list(raw, "actions"), start=1):
        if not isinstance(item, dict):
            raise ToolError(f"Поручение {index}: ожидается объект с полями title, assignee, due_date.")
        title = (item.get("title") or "").strip()
        if not title:
            raise ToolError(f"Поручение {index}: пустая формулировка.")
        due = item.get("due_date")
        actions.append(
            {
                "title": title,
                "assignee": (item.get("assignee") or "").strip(),
                "due_date": _parse_date(due, f"actions[{index}].due_date") if due else None,
                "notes": (item.get("notes") or "").strip(),
            }
        )
    return actions


def _protocol_save(tool_input: dict[str, Any]) -> Any:
    title = (tool_input.get("title") or "").strip()
    if not title:
        raise ToolError("Не указано название встречи (title).")

    held_on = tool_input.get("held_on")
    day = _parse_date(held_on, "held_on") if held_on else datetime.now(settings.tz).date().isoformat()

    actions = _clean_actions(tool_input.get("actions"))
    decisions = [str(d).strip() for d in _as_list(tool_input.get("decisions"), "decisions") if str(d).strip()]
    agreements = [str(a).strip() for a in _as_list(tool_input.get("agreements"), "agreements") if str(a).strip()]
    questions = [str(q).strip() for q in _as_list(tool_input.get("open_questions"), "open_questions") if str(q).strip()]
    participants = [str(p).strip() for p in _as_list(tool_input.get("participants"), "participants") if str(p).strip()]

    if not (actions or decisions or agreements or questions):
        raise ToolError(
            "Протокол пуст: нет ни договорённостей, ни решений, ни поручений, ни открытых "
            "вопросов. Не сохраняй пустой протокол — уточни, что обсуждалось."
        )

    def save_protocol(data: dict[str, Any]) -> dict[str, Any]:
        items = data.setdefault("protocols", [])
        record = {
            "id": f"P-{len(items) + 1:04d}",
            "title": title,
            "held_on": day,
            "participants": participants,
            "agreements": agreements,
            "decisions": decisions,
            "open_questions": questions,
            "actions": actions,
            "task_ids": [],
            "created_at": _now(),
        }
        items.append(record)
        return record

    record = update_json(settings.protocols_path, {"protocols": []}, save_protocol)

    # Поручения уходят в общий реестр: там их видит и контроль сроков,
    # и утренняя сводка. Протокол без этого — просто текст.
    created_tasks = []
    if actions:
        def add_tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
            tasks = data.setdefault("tasks", [])
            made = []
            for action in actions:
                task = {
                    "id": f"T-{len(tasks) + 1:04d}",
                    "title": action["title"],
                    "assignee": action["assignee"],
                    "due_date": action["due_date"],
                    "status": "open",
                    "priority": "normal",
                    "source": f"{record['id']}: {title} ({day})",
                    "notes": action["notes"],
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                tasks.append(task)
                made.append(task)
            return made

        created_tasks = update_json(settings.tasks_path, {"tasks": []}, add_tasks)

        def link(data: dict[str, Any]) -> None:
            for item in data.get("protocols", []):
                if item.get("id") == record["id"]:
                    item["task_ids"] = [t["id"] for t in created_tasks]
            return None

        update_json(settings.protocols_path, {"protocols": []}, link)

    without_assignee = [a["title"] for a in actions if not a["assignee"]]
    without_due = [a["title"] for a in actions if not a["due_date"]]

    return {
        "status": "created",
        "protocol_id": record["id"],
        "title": title,
        "held_on": day,
        "counts": {
            "agreements": len(agreements),
            "decisions": len(decisions),
            "actions": len(actions),
            "open_questions": len(questions),
        },
        "tasks_created": [_decorate(t) for t in created_tasks],
        "warnings": {
            "without_assignee": without_assignee,
            "without_due_date": without_due,
        },
        "hint": (
            "Поручения занесены в реестр — их подхватят контроль сроков и утренняя сводка. "
            "Если у поручения нет ответственного или срока, скажи об этом прямо: "
            "проконтролировать такое нельзя."
        ),
    }


def _protocol_list(tool_input: dict[str, Any]) -> Any:
    items = _protocols()
    if not items:
        return {
            "status": "empty",
            "hint": "Протоколов пока нет. Не придумывай прошедшие встречи и договорённости.",
        }

    result = list(items)
    query = (tool_input.get("query") or "").strip().lower()
    if query:
        result = [
            p
            for p in result
            if query in (p.get("title") or "").lower()
            or any(query in str(d).lower() for d in p.get("decisions", []))
            or any(query in str(a).lower() for a in p.get("agreements", []))
        ]
    since = (tool_input.get("since") or "").strip()
    if since:
        limit = _parse_date(since, "since")
        result = [p for p in result if (p.get("held_on") or "") >= limit]

    if not result:
        return {"status": "not_found", "total": len(items), "hint": "Под фильтр ничего не подошло."}

    result.sort(key=lambda p: p.get("held_on") or "", reverse=True)
    if not tool_input.get("full"):
        result = [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "held_on": p.get("held_on"),
                "participants": p.get("participants", []),
                "counts": {
                    "agreements": len(p.get("agreements", [])),
                    "decisions": len(p.get("decisions", [])),
                    "actions": len(p.get("actions", [])),
                    "open_questions": len(p.get("open_questions", [])),
                },
                "task_ids": p.get("task_ids", []),
            }
            for p in result[:30]
        ]
    return {"status": "ok", "count": len(result), "protocols": result}


def _preview_save(tool_input: dict[str, Any]) -> Preview:
    actions = tool_input.get("actions") or []
    lines = []
    for action in actions[:12]:
        if not isinstance(action, dict):
            continue
        who = action.get("assignee") or "БЕЗ ОТВЕТСТВЕННОГО"
        when = action.get("due_date") or "БЕЗ СРОКА"
        lines.append(f"{action.get('title', '')} — {who}, {when}")

    details: dict[str, Any] = {
        "Встреча": tool_input.get("title", ""),
        "Дата": tool_input.get("held_on") or "сегодня",
        "Участники": ", ".join(tool_input.get("participants") or []) or "—",
        "Договорённостей": len(tool_input.get("agreements") or []),
        "Решений": len(tool_input.get("decisions") or []),
        "Открытых вопросов": len(tool_input.get("open_questions") or []),
    }
    if lines:
        details["Поручения в реестр"] = "\n".join(lines) + (
            f"\n…и ещё {len(actions) - 12}" if len(actions) > 12 else ""
        )

    return Preview(
        title="Сохранить протокол и поставить поручения",
        summary=(
            f"«{tool_input.get('title', '')}»: {len(actions)} поручений уйдут в реестр "
            "и попадут в контроль сроков."
        ),
        details=details,
    )


registry.register(
    ToolSpec(
        name="protocol_save",
        description=(
            "Сохраняет протокол встречи и заносит поручения из него в реестр одним "
            "действием. Применяй, когда пользователь дал расшифровку, заметки или "
            "надиктовку встречи: сначала разбери их на договорённости, решения, "
            "поручения (кто/что/срок) и открытые вопросы, затем вызови этот инструмент. "
            "НИЧЕГО НЕ ДОДУМЫВАЙ: если ответственный или срок не назван — оставь поле "
            "пустым, инструмент отметит это отдельно. Выполняется после подтверждения."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название встречи."},
                "held_on": {"type": "string", "description": "Дата встречи YYYY-MM-DD. По умолчанию сегодня."},
                "participants": {"type": "array", "items": {"type": "string"}},
                "agreements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "О чём договорились.",
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Какие решения приняты.",
                },
                "actions": {
                    "type": "array",
                    "description": "Поручения. Каждое уйдёт в реестр отдельной записью.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Что сделать."},
                            "assignee": {"type": "string", "description": "Кто. Пусто, если не назвали."},
                            "due_date": {"type": "string", "description": "Срок YYYY-MM-DD. Пусто, если не назвали."},
                            "notes": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
                "open_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Что осталось нерешённым.",
                },
            },
            "required": ["title"],
        },
        handler=_protocol_save,
        requires_confirmation=True,
        preview=_preview_save,
        activity="Сохраняю протокол и ставлю поручения",
    )
)

registry.register(
    ToolSpec(
        name="protocol_list",
        description=(
            "Сохранённые протоколы встреч: о чём договаривались, что решили, какие "
            "поручения выданы. Используй, когда спрашивают о прошлых встречах и "
            "договорённостях. Если протоколов нет — так и скажи."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поиск по названию, решениям, договорённостям."},
                "since": {"type": "string", "description": "Не раньше даты YYYY-MM-DD."},
                "full": {"type": "boolean", "description": "Вернуть протоколы целиком, а не сводку."},
            },
        },
        handler=_protocol_list,
        activity="Смотрю протоколы встреч",
    )
)
