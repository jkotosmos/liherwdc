"""Реестр поручений и контрольных точек — функция бизнес-ассистента.

Хранится локально в data/tasks.json. Чтение свободное, любое изменение —
только после подтверждения пользователя.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..config import settings
from ..storage import read_json, update_json
from .base import Preview, ToolError, ToolSpec, registry

STATUSES = ("open", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("high", "normal", "low")

STATUS_RU = {
    "open": "открыто",
    "in_progress": "в работе",
    "blocked": "заблокировано",
    "done": "выполнено",
    "cancelled": "отменено",
}


def _now() -> str:
    return datetime.now(settings.tz).isoformat(timespec="seconds")


def _load() -> list[dict[str, Any]]:
    data = read_json(settings.tasks_path, {"tasks": []})
    return data.get("tasks", []) if isinstance(data, dict) else []


def _parse_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError as exc:
        raise ToolError(f"Поле {field}: ожидается дата в формате YYYY-MM-DD, получено «{value}».") from exc


def _decorate(task: dict[str, Any]) -> dict[str, Any]:
    view = dict(task)
    view["status_ru"] = STATUS_RU.get(task.get("status", ""), task.get("status", ""))
    due = task.get("due_date")
    if due and task.get("status") not in {"done", "cancelled"}:
        today = datetime.now(settings.tz).date()
        delta = (date.fromisoformat(due) - today).days
        view["days_left"] = delta
        view["overdue"] = delta < 0
    else:
        view["days_left"] = None
        view["overdue"] = False
    return view


def _tasks_list(tool_input: dict[str, Any]) -> Any:
    tasks = _load()
    if not tasks:
        return {
            "status": "empty",
            "hint": (
                "Реестр поручений пуст. Сообщи об этом пользователю и предложи зафиксировать "
                "поручения через task_create. Не придумывай существующие задачи."
            ),
        }

    result = [_decorate(t) for t in tasks]

    status = (tool_input.get("status") or "").strip().lower()
    if status:
        if status not in STATUSES:
            raise ToolError(f"Неизвестный статус «{status}». Допустимо: {list(STATUSES)}")
        result = [t for t in result if t.get("status") == status]

    assignee = (tool_input.get("assignee") or "").strip().lower()
    if assignee:
        result = [t for t in result if assignee in (t.get("assignee") or "").lower()]

    query = (tool_input.get("query") or "").strip().lower()
    if query:
        result = [
            t
            for t in result
            if query in (t.get("title") or "").lower() or query in (t.get("notes") or "").lower()
        ]

    if tool_input.get("overdue_only"):
        result = [t for t in result if t.get("overdue")]

    due_before = (tool_input.get("due_before") or "").strip()
    if due_before:
        limit = _parse_date(due_before, "due_before")
        result = [t for t in result if t.get("due_date") and t["due_date"] <= limit]

    result.sort(key=lambda t: (t.get("due_date") or "9999-12-31", t.get("id", "")))

    if not result:
        return {"status": "not_found", "total_in_registry": len(tasks), "hint": "Под фильтр ничего не подошло."}
    return {
        "status": "ok",
        "count": len(result),
        "overdue_count": sum(1 for t in result if t.get("overdue")),
        "tasks": result,
    }


def _task_create(tool_input: dict[str, Any]) -> Any:
    title = (tool_input.get("title") or "").strip()
    if not title:
        raise ToolError("Не указана формулировка поручения (title).")

    priority = (tool_input.get("priority") or "normal").strip().lower()
    if priority not in PRIORITIES:
        raise ToolError(f"Неизвестный приоритет «{priority}». Допустимо: {list(PRIORITIES)}")

    due_date = tool_input.get("due_date")
    due = _parse_date(due_date, "due_date") if due_date else None

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        tasks = data.setdefault("tasks", [])
        task = {
            "id": f"T-{len(tasks) + 1:04d}",
            "title": title,
            "assignee": (tool_input.get("assignee") or "").strip(),
            "due_date": due,
            "status": "open",
            "priority": priority,
            "source": (tool_input.get("source") or "").strip(),
            "notes": (tool_input.get("notes") or "").strip(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        tasks.append(task)
        return task

    created = update_json(settings.tasks_path, {"tasks": []}, mutate)
    return {"status": "created", "task": _decorate(created)}


def _task_update(tool_input: dict[str, Any]) -> Any:
    task_id = (tool_input.get("task_id") or "").strip()
    if not task_id:
        raise ToolError("Не указан task_id.")

    status = (tool_input.get("status") or "").strip().lower()
    if status and status not in STATUSES:
        raise ToolError(f"Неизвестный статус «{status}». Допустимо: {list(STATUSES)}")
    priority = (tool_input.get("priority") or "").strip().lower()
    if priority and priority not in PRIORITIES:
        raise ToolError(f"Неизвестный приоритет «{priority}». Допустимо: {list(PRIORITIES)}")
    due_date = tool_input.get("due_date")
    due = _parse_date(due_date, "due_date") if due_date else None

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        tasks = data.setdefault("tasks", [])
        for task in tasks:
            if task.get("id") == task_id:
                before = dict(task)
                if status:
                    task["status"] = status
                if priority:
                    task["priority"] = priority
                if due:
                    task["due_date"] = due
                if tool_input.get("assignee") is not None and tool_input["assignee"] != "":
                    task["assignee"] = tool_input["assignee"].strip()
                if tool_input.get("notes"):
                    existing = task.get("notes") or ""
                    stamp = datetime.now(settings.tz).strftime("%d.%m.%Y")
                    task["notes"] = f"{existing}\n[{stamp}] {tool_input['notes']}".strip()
                task["updated_at"] = _now()
                return {"before": before, "after": dict(task)}
        raise ToolError(f"Поручение {task_id} не найдено. Список даёт tasks_list.")

    changed = update_json(settings.tasks_path, {"tasks": []}, mutate)
    return {
        "status": "updated",
        "before": _decorate(changed["before"]),
        "after": _decorate(changed["after"]),
    }


def _preview_create(tool_input: dict[str, Any]) -> Preview:
    return Preview(
        title="Зафиксировать поручение",
        summary=f"В реестр будет добавлено: «{tool_input.get('title', '')}»"
        + (f", срок {tool_input['due_date']}" if tool_input.get("due_date") else ", без срока"),
        details={
            "Формулировка": tool_input.get("title", ""),
            "Ответственный": tool_input.get("assignee") or "—",
            "Срок": tool_input.get("due_date") or "—",
            "Приоритет": tool_input.get("priority") or "normal",
            "Основание": tool_input.get("source") or "—",
            "Комментарий": tool_input.get("notes") or "—",
        },
    )


def _preview_update(tool_input: dict[str, Any]) -> Preview:
    changes = {
        key: value
        for key, value in tool_input.items()
        if key != "task_id" and value not in (None, "")
    }
    return Preview(
        title="Изменить поручение",
        summary=f"Поручение {tool_input.get('task_id', '?')}: {', '.join(changes) or 'без изменений'}.",
        details={"Поручение": tool_input.get("task_id", ""), **changes},
    )


registry.register(
    ToolSpec(
        name="tasks_list",
        description=(
            "Показывает реестр поручений и контрольных точек: что открыто, за кем закреплено, "
            "какие сроки прошли. Используй для вопросов о статусе задач, просрочках и загрузке, "
            "а также перед подготовкой отчёта или списка следующих действий."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(STATUSES),
                    "description": "Фильтр по статусу.",
                },
                "assignee": {"type": "string", "description": "Фильтр по ответственному."},
                "query": {"type": "string", "description": "Поиск по формулировке и комментариям."},
                "overdue_only": {"type": "boolean", "description": "Только просроченные."},
                "due_before": {"type": "string", "description": "Срок не позднее YYYY-MM-DD."},
            },
        },
        handler=_tasks_list,
        activity="Смотрю реестр поручений",
    )
)

registry.register(
    ToolSpec(
        name="task_create",
        description=(
            "Фиксирует поручение с ответственным и сроком в реестре. Применяй по итогам встреч и "
            "договорённостей, когда пользователь просит «зафиксировать», «поставить задачу», "
            "«взять на контроль». Записывается только после подтверждения пользователя."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Что именно нужно сделать."},
                "assignee": {"type": "string", "description": "Ответственный."},
                "due_date": {"type": "string", "description": "Срок, YYYY-MM-DD."},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "source": {"type": "string", "description": "Основание: встреча, договор, письмо."},
                "notes": {"type": "string", "description": "Дополнительный контекст."},
            },
            "required": ["title"],
        },
        handler=_task_create,
        requires_confirmation=True,
        preview=_preview_create,
        activity="Фиксирую поручение",
    )
)

registry.register(
    ToolSpec(
        name="task_update",
        description=(
            "Меняет статус, срок, ответственного или комментарий существующего поручения. "
            "Выполняется только после подтверждения пользователя."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Идентификатор вида T-0001."},
                "status": {"type": "string", "enum": list(STATUSES)},
                "due_date": {"type": "string", "description": "Новый срок, YYYY-MM-DD."},
                "assignee": {"type": "string"},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "notes": {"type": "string", "description": "Комментарий; добавляется с датой."},
            },
            "required": ["task_id"],
        },
        handler=_task_update,
        requires_confirmation=True,
        preview=_preview_update,
        activity="Обновляю поручение",
    )
)
