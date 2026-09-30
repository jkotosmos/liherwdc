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

Кроме сводки бот пишет ещё в двух случаях — и они не ждут ни рабочего дня,
ни конца «тихих часов»: время выбрал сам человек.

* **Встреча скоро.** За OPERON_MEETING_REMIND_MINUTES минут до начала
  события в календаре. Ключ включает время начала: перенесли встречу —
  напомним о новом времени.
* **Личное напоминание** («напомни в 15:00 позвонить…», tools/remind.py) —
  в назначенную минуту.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
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
    # Для личного напоминания — его id: после доставки оно закрывается.
    ref: str = ""


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


# Календарь спрашиваем не на каждом тике опроса (он бывает раз в пару секунд),
# а не чаще раза в минуту на человека: напоминанию за 30 минут этого хватает.
MEETING_CHECK_SECONDS = 60
_last_meeting_check: dict[str, datetime] = {}


def _upcoming_meetings(moment: datetime, minutes: int) -> list[dict[str, Any]]:
    """Встречи с началом в ближайшие minutes минут. Без Google — пусто."""
    from .integrations import google_client

    if not google_client.status().get("connected"):
        return []
    try:
        from .tools.calendar import list_events_window

        return list_events_window(moment, moment + timedelta(minutes=minutes))
    except Exception:  # noqa: BLE001 — напоминание не должно падать из-за календаря
        logger.warning("Не удалось прочитать календарь для напоминаний о встречах", exc_info=True)
        return []


def _meeting_reminders(moment: datetime, account: str, sent: dict[str, str]) -> list[Reminder]:
    minutes = settings.meeting_remind_minutes
    if minutes <= 0:
        return []
    last = _last_meeting_check.get(account)
    if last is not None and timedelta(0) <= moment - last < timedelta(seconds=MEETING_CHECK_SECONDS):
        return []
    _last_meeting_check[account] = moment

    result = []
    for event in _upcoming_meetings(moment, minutes):
        if event.get("all_day") or event.get("status") == "cancelled":
            continue
        start_raw = event.get("start") or ""
        try:
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=settings.tz)
        if start <= moment:
            continue  # уже началась — поздно напоминать
        key = f"meeting:{account}:{event.get('event_id')}:{start_raw}"
        if key in sent:
            continue
        result.append(Reminder(key=key, text=meeting_text(event, start, moment), kind="meeting"))
    return result


def _personal_reminders(moment: datetime, account: str, sent: dict[str, str]) -> list[Reminder]:
    from .tools import remind

    result = []
    for item in remind.due(account, moment):
        if f"personal:{item['id']}" in sent:
            continue  # уже ушло, но статус в файле не успел обновиться
        when = datetime.fromisoformat(item["at"])
        text = f"🔔 <b>Напоминание</b>\n{escape(item['text'])}"
        if moment - when > timedelta(minutes=10):
            # Бот был выключен в назначенное время — честно говорим, что с опозданием.
            text += f"\n<i>Должно было прийти {when.strftime('%d.%m %H:%M')}.</i>"
        result.append(Reminder(key=f"personal:{item['id']}", text=text, kind="personal", ref=item["id"]))
    return result


# --- тексты -----------------------------------------------------------------


def meeting_text(event: dict[str, Any], start: datetime, moment: datetime) -> str:
    left = max(1, round((start - moment).total_seconds() / 60))
    local = start.astimezone(settings.tz)
    lines = [
        f"⏰ <b>Через {left} мин — встреча</b>",
        f"{local.strftime('%H:%M')} {escape(str(event.get('title') or '(без названия)'))}",
    ]
    if event.get("location"):
        lines.append(f"Место: {escape(str(event['location']))}")
    if event.get("link"):
        lines.append(f'<a href="{escape(str(event["link"]), quote=True)}">Открыть в календаре</a>')
    return "\n".join(lines)



def _task_line(task: dict[str, Any], today: date) -> str:
    due = task.get("due_date") or ""
    title = escape(str(task.get("title", "без названия")))
    who = escape(str(task.get("assignee") or ""))
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
    title = escape(str(event.get("title", "(без названия)")))
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


def pending(moment: datetime | None = None, account: str = "") -> list[Reminder]:
    """Что нужно отправить прямо сейчас. Уже отправленное не повторяется."""
    if not settings.reminders_enabled:
        return []

    moment = moment or _now()
    today = moment.date()

    sent = _sent_keys()
    # Встречи и личные напоминания — в любое время: их время выбрал человек.
    plan: list[Reminder] = _personal_reminders(moment, account, sent)
    plan.extend(_meeting_reminders(moment, account, sent))

    if in_quiet_hours(moment):
        return plan

    # Утренняя сводка: один раз в день, начиная с назначенного часа. Если бот
    # в это время лежал, сводка уйдёт при первом же подъёме — но всё ещё
    # сегодня, а не задним числом.
    # Ключ отправки — свой у каждого получателя: один получил, другой ещё нет.
    digest_key = f"digest:{today.isoformat()}" + (f":{account}" if account else "")
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
    personal = [r.ref for r in reminders if r.kind == "personal" and r.ref]
    if personal:
        from .tools import remind

        remind.mark_delivered(personal, moment)


def describe() -> dict[str, Any]:
    """Настройки напоминаний — для /status и диагностики."""
    start, end = settings.quiet_hours
    return {
        "enabled": settings.reminders_enabled,
        "digest_hour": settings.digest_hour,
        "digest_weekdays": sorted(settings.digest_weekdays),
        "remind_before_days": settings.remind_before_days,
        "meeting_remind_minutes": settings.meeting_remind_minutes,
        "quiet_hours": f"{start}:00–{end}:00" if start != end else "нет",
    }
