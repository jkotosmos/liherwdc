"""Инструменты Google Calendar: встречи, сроки, напоминания."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from ..config import settings
from ..integrations.google_client import HttpError, describe_http_error, get_service
from .base import Preview, ToolError, ToolSpec, registry


def _calendar():
    return get_service("calendar", "v3")


def _to_rfc3339(value: str, *, end_of_day: bool = False) -> str:
    """Принимает «2026-08-20», «2026-08-20T15:00» или полный RFC3339."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError("Пустая дата/время.")
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            moment = datetime.combine(day, time.max if end_of_day else time.min)
        else:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            f"Не удалось разобрать дату «{value}». Используйте YYYY-MM-DD или YYYY-MM-DDTHH:MM."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=settings.tz)
    return moment.isoformat()


def _event_view(event: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    start = event.get("start", {})
    end = event.get("end", {})
    attendees = [
        {
            "email": a.get("email"),
            "name": a.get("displayName", ""),
            "response": a.get("responseStatus"),
        }
        for a in event.get("attendees", [])
    ]
    return {
        "event_id": event.get("id"),
        "calendar_id": calendar_id,
        "title": event.get("summary", "(без названия)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "location": event.get("location", ""),
        "description": (event.get("description") or "")[:2000],
        "organizer": (event.get("organizer") or {}).get("email", ""),
        "attendees": attendees,
        "status": event.get("status"),
        "link": event.get("htmlLink"),
    }


def _calendar_list_calendars(_: dict[str, Any]) -> Any:
    try:
        items = _calendar().calendarList().list(maxResults=100).execute().get("items", [])
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Список календарей")) from exc
    return {
        "status": "ok",
        "calendars": [
            {
                "calendar_id": c.get("id"),
                "name": c.get("summary"),
                "primary": bool(c.get("primary")),
                "access_role": c.get("accessRole"),
                "timezone": c.get("timeZone"),
            }
            for c in items
        ],
    }


def _calendar_list_events(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or "primary").strip()
    now = datetime.now(settings.tz)

    time_min = tool_input.get("time_min")
    time_max = tool_input.get("time_max")
    start = _to_rfc3339(time_min) if time_min else now.isoformat()
    end = (
        _to_rfc3339(time_max, end_of_day=True)
        if time_max
        else (now + timedelta(days=14)).isoformat()
    )

    params: dict[str, Any] = {
        "calendarId": calendar_id,
        "timeMin": start,
        "timeMax": end,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": min(max(int(tool_input.get("max_results") or 50), 1), 250),
    }
    if tool_input.get("query"):
        params["q"] = tool_input["query"]

    try:
        events = _calendar().events().list(**params).execute().get("items", [])
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Чтение календаря")) from exc

    if not events:
        return {
            "status": "empty",
            "calendar_id": calendar_id,
            "period": {"from": start, "to": end},
            "hint": "В указанный период встреч нет. Не придумывай события.",
        }
    return {
        "status": "ok",
        "calendar_id": calendar_id,
        "period": {"from": start, "to": end},
        "events_count": len(events),
        "events": [_event_view(e, calendar_id) for e in events],
    }


def _build_event_body(tool_input: dict[str, Any]) -> dict[str, Any]:
    title = (tool_input.get("title") or "").strip()
    start = (tool_input.get("start") or "").strip()
    end = (tool_input.get("end") or "").strip()
    if not title:
        raise ToolError("Не указано название события (title).")
    if not start:
        raise ToolError("Не указано время начала (start).")

    all_day = len(start) == 10
    if not end:
        if all_day:
            end = start
        else:
            begin = datetime.fromisoformat(_to_rfc3339(start))
            end = (begin + timedelta(minutes=int(tool_input.get("duration_minutes") or 60))).isoformat()

    body: dict[str, Any] = {"summary": title}
    if all_day:
        finish = date.fromisoformat(end[:10]) + timedelta(days=1)
        body["start"] = {"date": start[:10]}
        body["end"] = {"date": finish.isoformat()}
    else:
        body["start"] = {"dateTime": _to_rfc3339(start), "timeZone": settings.timezone_name}
        body["end"] = {"dateTime": _to_rfc3339(end), "timeZone": settings.timezone_name}

    if tool_input.get("description"):
        body["description"] = tool_input["description"]
    if tool_input.get("location"):
        body["location"] = tool_input["location"]
    attendees = tool_input.get("attendees") or []
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees if email]
    reminder_minutes = tool_input.get("reminder_minutes")
    if reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(reminder_minutes)}],
        }
    return body


def _calendar_create_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or "primary").strip()
    body = _build_event_body(tool_input)
    try:
        created = (
            _calendar()
            .events()
            .insert(
                calendarId=calendar_id,
                body=body,
                sendUpdates="all" if body.get("attendees") else "none",
            )
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Создание события")) from exc
    return {"status": "created", **_event_view(created, calendar_id)}


def _calendar_update_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or "primary").strip()
    event_id = (tool_input.get("event_id") or "").strip()
    if not event_id:
        raise ToolError("Не указан event_id.")

    try:
        current = _calendar().events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Чтение события перед изменением")) from exc

    patch: dict[str, Any] = {}
    if tool_input.get("title"):
        patch["summary"] = tool_input["title"]
    if tool_input.get("description") is not None:
        patch["description"] = tool_input["description"]
    if tool_input.get("location") is not None:
        patch["location"] = tool_input["location"]
    if tool_input.get("start"):
        start = tool_input["start"]
        patch["start"] = (
            {"date": start[:10]}
            if len(start) == 10
            else {"dateTime": _to_rfc3339(start), "timeZone": settings.timezone_name}
        )
    if tool_input.get("end"):
        end = tool_input["end"]
        patch["end"] = (
            {"date": end[:10]}
            if len(end) == 10
            else {"dateTime": _to_rfc3339(end), "timeZone": settings.timezone_name}
        )
    if not patch:
        raise ToolError("Не переданы поля для изменения.")

    try:
        updated = (
            _calendar()
            .events()
            .patch(calendarId=calendar_id, eventId=event_id, body=patch, sendUpdates="all")
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Изменение события")) from exc
    return {
        "status": "updated",
        "previous": _event_view(current, calendar_id),
        "current": _event_view(updated, calendar_id),
    }


def _calendar_delete_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or "primary").strip()
    event_id = (tool_input.get("event_id") or "").strip()
    if not event_id:
        raise ToolError("Не указан event_id.")
    try:
        current = _calendar().events().get(calendarId=calendar_id, eventId=event_id).execute()
        _calendar().events().delete(
            calendarId=calendar_id, eventId=event_id, sendUpdates="all"
        ).execute()
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Удаление события")) from exc
    return {"status": "deleted", "deleted_event": _event_view(current, calendar_id)}


# --- карточки подтверждения ------------------------------------------------


def _preview_create(tool_input: dict[str, Any]) -> Preview:
    attendees = tool_input.get("attendees") or []
    return Preview(
        title="Создать событие в календаре",
        summary=f"«{tool_input.get('title', 'без названия')}», начало {tool_input.get('start', '?')}"
        + (f", участников: {len(attendees)} (им уйдут приглашения)" if attendees else ""),
        details={
            "Календарь": tool_input.get("calendar_id") or "primary (основной)",
            "Название": tool_input.get("title", ""),
            "Начало": tool_input.get("start", ""),
            "Окончание": tool_input.get("end") or f"+{tool_input.get('duration_minutes', 60)} мин",
            "Место": tool_input.get("location") or "—",
            "Участники": ", ".join(attendees) if attendees else "—",
            "Напоминание": (
                f"за {tool_input['reminder_minutes']} мин"
                if tool_input.get("reminder_minutes") is not None
                else "по умолчанию"
            ),
            "Описание": (tool_input.get("description") or "—")[:800],
        },
    )


def _preview_update(tool_input: dict[str, Any]) -> Preview:
    changes = {
        key: value
        for key, value in tool_input.items()
        if key not in {"event_id", "calendar_id"} and value not in (None, "")
    }
    return Preview(
        title="Изменить событие в календаре",
        summary=f"Событие {tool_input.get('event_id', '?')} будет изменено; участники получат уведомление.",
        details={"Календарь": tool_input.get("calendar_id") or "primary", **changes},
    )


def _preview_delete(tool_input: dict[str, Any]) -> Preview:
    return Preview(
        title="Удалить событие из календаря",
        summary=(
            f"Событие {tool_input.get('event_id', '?')} будет удалено безвозвратно, "
            "участникам уйдёт отмена."
        ),
        details={
            "Календарь": tool_input.get("calendar_id") or "primary",
            "Идентификатор события": tool_input.get("event_id", ""),
        },
    )


# --- регистрация -----------------------------------------------------------

registry.register(
    ToolSpec(
        name="calendar_list_calendars",
        description="Список календарей, доступных пользователю, с уровнем доступа к каждому.",
        input_schema={"type": "object", "properties": {}},
        handler=_calendar_list_calendars,
        activity="Смотрю список календарей",
    )
)

registry.register(
    ToolSpec(
        name="calendar_list_events",
        description=(
            "Показывает встречи и сроки из Google Calendar за период. По умолчанию — ближайшие "
            "14 дней. Используй для вопросов о расписании, дедлайнах, загрузке и при подготовке "
            "к встречам. Если событий нет — так и скажи, не придумывай."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "Начало периода: YYYY-MM-DD или YYYY-MM-DDTHH:MM."},
                "time_max": {"type": "string", "description": "Конец периода: YYYY-MM-DD или YYYY-MM-DDTHH:MM."},
                "calendar_id": {"type": "string", "description": "По умолчанию primary."},
                "query": {"type": "string", "description": "Фильтр по тексту события."},
                "max_results": {"type": "integer", "description": "Максимум событий (1–250), по умолчанию 50."},
            },
        },
        handler=_calendar_list_events,
        activity="Смотрю календарь",
    )
)

registry.register(
    ToolSpec(
        name="calendar_create_event",
        description=(
            "Создаёт событие или напоминание в Google Calendar. Применяй для встреч, контрольных "
            "точек, сроков отчётности и напоминаний о поручениях. ВАЖНО: событие создаётся только "
            "после подтверждения пользователя. Если в событии есть участники, им будут отправлены "
            "приглашения — обязательно предупреди об этом."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события."},
                "start": {
                    "type": "string",
                    "description": "Начало: YYYY-MM-DDTHH:MM или YYYY-MM-DD для события на весь день.",
                },
                "end": {"type": "string", "description": "Окончание в том же формате. Можно не указывать."},
                "duration_minutes": {
                    "type": "integer",
                    "description": "Длительность, если не указано окончание. По умолчанию 60.",
                },
                "description": {"type": "string", "description": "Описание, повестка, ссылки."},
                "location": {"type": "string", "description": "Место или ссылка на видеовстречу."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "E-mail участников. Им будут отправлены приглашения.",
                },
                "reminder_minutes": {
                    "type": "integer",
                    "description": "За сколько минут напомнить.",
                },
                "calendar_id": {"type": "string", "description": "По умолчанию primary."},
            },
            "required": ["title", "start"],
        },
        handler=_calendar_create_event,
        requires_confirmation=True,
        preview=_preview_create,
        activity="Создаю событие в календаре",
    )
)

registry.register(
    ToolSpec(
        name="calendar_update_event",
        description=(
            "Изменяет существующее событие календаря (перенос, смена названия, места, описания). "
            "Выполняется только после подтверждения пользователя; участники получат уведомление."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Идентификатор события из calendar_list_events."},
                "calendar_id": {"type": "string", "description": "По умолчанию primary."},
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
        handler=_calendar_update_event,
        requires_confirmation=True,
        preview=_preview_update,
        activity="Изменяю событие в календаре",
    )
)

registry.register(
    ToolSpec(
        name="calendar_delete_event",
        description=(
            "Удаляет событие из календаря. Необратимое действие, выполняется только после "
            "подтверждения пользователя."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Идентификатор события."},
                "calendar_id": {"type": "string", "description": "По умолчанию primary."},
            },
            "required": ["event_id"],
        },
        handler=_calendar_delete_event,
        requires_confirmation=True,
        preview=_preview_delete,
        activity="Удаляю событие из календаря",
    )
)
