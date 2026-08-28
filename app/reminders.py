"""Напоминания, которые бот отправляет сам, без вопроса пользователя.

Пункт ТЗ «напоминать о задачах, обязательствах, отчётности, контрольных
точках» невозможно закрыть инструментом: инструмент вызывается моделью в
ответ на реплику, а напоминание должно прийти, когда пользователь молчит.
Поэтому здесь отдельный планировщик, работающий на пульсе опроса Telegram.

Три решения, определяющие поведение:

* **Сводка собирается без модели.** Это детерминированный отчёт по реестру
  поручений и календарю. Модель здесь не нужна: она стоит денег, отвечает
  небыстро и может присочинить срок, которого нет. Цифры должны быть цифрами.
* **Каждое напоминание отправляется один раз.** Ключ отправленного пишется
  на диск, поэтому передеплой или перезапуск бота не выльется в повтор.
* **Молчание по умолчанию.** Нет просрочек и близких сроков — сводка не
  приходит вовсе. Ежедневное «всё в порядке» перестают читать через неделю,
  а вместе с ним перестают читать и важное.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import settings
from .storage import read_json, write_json

logger = logging.getLogger(__name__)

# Ключи отправленного храним ограниченное время: иначе файл растёт вечно.
KEEP_KEYS_DAYS = 45


@dataclass
class Reminder:
    """Готовое сообщение и ключ, по которому видно, что оно уже уходило."""

    key: str
    text: str
    kind: str = "digest"


@dataclass
class _Plan:
    """Что бот отправит на этом тике."""

    reminders: list[Reminder] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(settings.tz)


def _sent_keys() -> dict[str, str]:
    data = read_json(settings.reminders_path, {"sent": {}})
    sent = data.get("sent") if isinstance(data, dict) else None
    return sent if isinstance(sent, dict) else {}


def _remember(keys: dict[str, str], new: list[Reminder], today: date) -> None:
    for reminder in new:
        keys[reminder.key] = today.isoformat()

    horizon = (today - timedelta(days=KEEP_KEYS_DAYS)).isoformat()
    fresh = {key: day for key, day in keys.items() if day >= horizon}
    write_json(settings.reminders_path, {"sent": fresh})


def in_quiet_hours(moment: datetime) -> bool:
    """Ночью бот молчит: напоминание в три часа ночи — это не забота."""
    start, end = settings.quiet_hours
    if start == end:
        return False
    hour = moment.hour
    if start < end:
        return start <= hour < end
    # Интервал через полночь, например 22–8.
    return hour >= start or hour < end


def _is_working_day(moment: datetime) -> bool:
    return moment.isoweekday() in settings.digest_weekdays


# --- сбор данных ------------------------------------------------------------


def _overdue_and_upcoming(today: date) -> tuple[list[dict], list[dict]]:
    """Просроченные и те, чей срок наступает в ближайшие дни."""
    from .tools import tasks as tasks_tools

    horizon = today + timedelta(days=settings.remind_before_days)
    overdue: list[dict] = []
    upcoming: list[dict] = []

    for task in tasks_tools.load_active():
        due = task.get("due_date")
        if not due:
            continue
        try:
            deadline = date.fromisoformat(due)
        except ValueError:
            continue
        if deadline < today:
            overdue.append(task)
        elif deadline <= horizon:
            upcoming.append(task)

    overdue.sort(key=lambda t: t.get("due_date") or "")
    upcoming.sort(key=lambda t: t.get("due_date") or "")
    return overdue, upcoming


def _today_events(today: date) -> list[dict[str, Any]]:
    """Встречи на сегодня. Google может быть не подключён — это не ошибка."""
    from .integrations import google_client

    if not google_client.status().get("connected"):
        return []
    try:
        from .tools.calendar import list_events_between

        return list_events_between(today, today)
    except Exception:  # noqa: BLE001 — напоминание не должно падать из-за календаря
        logger.warning("Не удалось прочитать календарь для сводки", exc_info=True)
        return []


# --- тексты -----------------------------------------------------------------


def _task_line(task: dict[str, Any], today: date) -> str:
    due = task.get("due_date") or ""
    title = task.get("title", "без названия")
    who = task.get("assignee") or ""
    tail = f" — {who}" if who else ""
    if not due:
        return f"• {title}{tail}"
    try:
        delta = (date.fromisoformat(due) - today).days
    except ValueError:
        return f"• {title}{tail}"
    if delta < 0:
        when = f"просрочено на {abs(delta)} дн."
    elif delta == 0:
        when = "срок сегодня"
    elif delta == 1:
        when = "срок завтра"
    else:
        when = f"срок через {delta} дн."
    return f"• {title}{tail} — {when} ({due})"


def _event_line(event: dict[str, Any]) -> str:
    start = (event.get("start") or "")[11:16]
    title = event.get("title", "(без названия)")
    return f"• {start} {title}" if start else f"• {title}"


def build_digest(today: date, events: list[dict], overdue: list[dict], upcoming: list[dict]) -> str:
    """Собирает текст утренней сводки. Пустую сводку не возвращает."""
    if not (events or overdue or upcoming):
        return ""

    blocks: list[str] = [f"<b>Сводка на {today.strftime('%d.%m.%Y')}</b>"]

    if overdue:
        blocks.append("")
        blocks.append(f"<b>Просрочено: {len(overdue)}</b>")
        blocks.extend(_task_line(t, today) for t in overdue[:10])
        if len(overdue) > 10:
            blocks.append(f"…и ещё {len(overdue) - 10}")

    if upcoming:
        blocks.append("")
        blocks.append("<b>Сроки на подходе</b>")
        blocks.extend(_task_line(t, today) for t in upcoming[:10])
        if len(upcoming) > 10:
            blocks.append(f"…и ещё {len(upcoming) - 10}")

    if events:
        blocks.append("")
        blocks.append(f"<b>Встречи сегодня: {len(events)}</b>")
        blocks.extend(_event_line(e) for e in events[:10])
        if len(events) > 10:
            blocks.append(f"…и ещё {len(events) - 10}")

    return "\n".join(blocks)


# --- планировщик ------------------------------------------------------------


def pending(moment: datetime | None = None) -> list[Reminder]:
    """Что нужно отправить прямо сейчас. Уже отправленное не повторяется."""
    if not settings.reminders_enabled:
        return []

    moment = moment or _now()
    today = moment.date()

    if in_quiet_hours(moment):
        return []

    sent = _sent_keys()
    plan: list[Reminder] = []

    # Утренняя сводка: один раз в день, начиная с назначенного часа. Если бот
    # в это время лежал, сводка уйдёт при первом же подъёме — но всё ещё
    # сегодня, а не задним числом.
    digest_key = f"digest:{today.isoformat()}"
    if (
        digest_key not in sent
        and moment.hour >= settings.digest_hour
        and _is_working_day(moment)
    ):
        overdue, upcoming = _overdue_and_upcoming(today)
        events = _today_events(today)
        text = build_digest(today, events, overdue, upcoming)
        if text:
            plan.append(Reminder(key=digest_key, text=text, kind="digest"))
        else:
            # Отмечаем даже пустую сводку: иначе будем пересобирать её
            # каждые полминуты до конца дня.
            plan.append(Reminder(key=digest_key, text="", kind="digest"))

    return plan


def mark_sent(reminders: list[Reminder], moment: datetime | None = None) -> None:
    moment = moment or _now()
    _remember(_sent_keys(), reminders, moment.date())


def describe() -> dict[str, Any]:
    """Настройки напоминаний — для /status и диагностики."""
    start, end = settings.quiet_hours
    return {
        "enabled": settings.reminders_enabled,
        "digest_hour": settings.digest_hour,
        "digest_weekdays": sorted(settings.digest_weekdays),
        "remind_before_days": settings.remind_before_days,
        "quiet_hours": f"{start}:00–{end}:00" if start != end else "нет",
    }
