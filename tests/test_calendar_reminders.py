"""Встречи и напоминания: перенос, удаление, «напомни мне», напоминание перед встречей.

ТЗ: создавать события и напоминания только после подтверждения, напоминать о
задачах и сроках. Здесь проверяется, что бот пишет вовремя, один раз, только
своему человеку и что перенос встречи не ломает её длительность.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import reminders
from app.config import settings
from app.integrations import accounts
from app.tools import calendar as calendar_tools
from app.tools import remind
from app.tools.base import registry

OWNER, KIRILL = "8058569481", "1107365044"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=settings.tz)


@pytest.fixture
def state(tmp_path, monkeypatch):
    conf = replace(settings, data_dir=tmp_path, digest_hour=9, meeting_remind_minutes=30)
    monkeypatch.setattr(reminders, "settings", conf)
    monkeypatch.setattr(remind, "settings", conf)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},{KIRILL}")
    monkeypatch.setenv("OPERON_REMINDERS", "true")
    monkeypatch.setenv("OPERON_QUIET_HOURS", "22-8")
    monkeypatch.setattr(reminders, "_overdue_and_upcoming", lambda today: ([], []))
    monkeypatch.setattr(reminders, "_today_events", lambda today: [])
    monkeypatch.setattr(reminders, "_upcoming_meetings", lambda moment, minutes: [])
    reminders._last_meeting_check.clear()
    return conf


def call(name: str, payload: dict, account: str) -> tuple[dict, bool]:
    with accounts.use(account):
        content, is_error = registry.execute(name, payload)
    return (json.loads(content) if not is_error else {"error": content}), is_error


def future(minutes: int) -> str:
    return (datetime.now(settings.tz) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


class TestPersonalReminders:
    def test_create_list_cancel_only_own(self, state) -> None:
        created, err = call("reminder_create", {"text": "Позвонить Иванову", "at": future(120)}, KIRILL)
        assert not err and created["status"] == "created"
        rid = created["reminder"]["reminder_id"]

        mine, _ = call("reminder_list", {}, KIRILL)
        assert [r["reminder_id"] for r in mine["reminders"]] == [rid]
        # Владелец чужих напоминаний не видит и отменить не может.
        other, _ = call("reminder_list", {}, OWNER)
        assert other["status"] == "empty"
        _, err = call("reminder_cancel", {"reminder_id": rid}, OWNER)
        assert err

        cancelled, err = call("reminder_cancel", {"reminder_id": rid}, KIRILL)
        assert not err and cancelled["status"] == "cancelled"
        assert call("reminder_list", {}, KIRILL)[0]["status"] == "empty"

    def test_past_time_and_date_without_time_are_rejected(self, state) -> None:
        _, err = call("reminder_create", {"text": "x", "at": "2020-01-01T10:00"}, OWNER)
        assert err
        _, err = call("reminder_create", {"text": "x", "at": "2099-01-01"}, OWNER)
        assert err

    def test_owner_web_chat_and_telegram_are_one_person(self, state) -> None:
        call("reminder_create", {"text": "Отчёт", "at": future(60)}, "")
        listed, _ = call("reminder_list", {}, OWNER)
        assert listed["count"] == 1

    def test_delivered_on_time_once_even_at_night(self, state) -> None:
        call("reminder_create", {"text": "Выпить <таблетку>", "at": future(5)}, KIRILL)
        due_at = datetime.now(settings.tz) + timedelta(minutes=6)
        night = due_at.replace(hour=23)
        if night < due_at:
            night += timedelta(days=1)

        assert [r for r in reminders.pending(datetime.now(settings.tz), KIRILL) if r.kind == "personal"] == []
        assert [r for r in reminders.pending(due_at, OWNER) if r.kind == "personal"] == []  # чужое не приходит

        plan = [r for r in reminders.pending(night, KIRILL) if r.kind == "personal"]
        assert len(plan) == 1
        assert "&lt;таблетку&gt;" in plan[0].text  # HTML экранирован
        assert "Должно было прийти" in plan[0].text  # пришло с опозданием — сказано честно
        reminders.mark_sent(plan, night)
        assert [r for r in reminders.pending(night, KIRILL) if r.kind == "personal"] == []


def meeting(start: datetime, event_id: str = "ev1", **extra) -> dict:
    return {"event_id": event_id, "title": "Встреча с <Ромашкой>", "start": start.isoformat(),
            "all_day": False, "status": "confirmed", "location": "Офис", "link": "https://calendar/x", **extra}


class TestMeetingReminders:
    def test_reminds_once_before_meeting(self, state, monkeypatch) -> None:
        now = at("2026-09-01T23:40")  # ночь: встречам тихие часы не помеха
        events = [meeting(now + timedelta(minutes=20))]
        monkeypatch.setattr(reminders, "_upcoming_meetings", lambda moment, minutes: list(events))

        plan = [r for r in reminders.pending(now, KIRILL) if r.kind == "meeting"]
        assert len(plan) == 1
        assert "Через 20 мин" in plan[0].text and "&lt;Ромашкой&gt;" in plan[0].text
        reminders.mark_sent(plan, now)

        later = now + timedelta(minutes=5)
        assert [r for r in reminders.pending(later, KIRILL) if r.kind == "meeting"] == []

    def test_rescheduled_meeting_is_reminded_again(self, state, monkeypatch) -> None:
        now = at("2026-09-01T10:00")
        events = [meeting(now + timedelta(minutes=25))]
        monkeypatch.setattr(reminders, "_upcoming_meetings", lambda moment, minutes: list(events))
        reminders.mark_sent([r for r in reminders.pending(now, OWNER) if r.kind == "meeting"], now)

        events[:] = [meeting(now + timedelta(minutes=28))]
        later = now + timedelta(minutes=2)
        assert len([r for r in reminders.pending(later, OWNER) if r.kind == "meeting"]) == 1

    def test_skips_started_all_day_and_cancelled(self, state, monkeypatch) -> None:
        now = at("2026-09-01T10:00")
        events = [
            meeting(now - timedelta(minutes=1), "a"),
            meeting(now + timedelta(minutes=10), "b", all_day=True),
            meeting(now + timedelta(minutes=10), "c", status="cancelled"),
        ]
        monkeypatch.setattr(reminders, "_upcoming_meetings", lambda moment, minutes: list(events))
        assert [r for r in reminders.pending(now, OWNER) if r.kind == "meeting"] == []

    def test_calendar_is_not_asked_every_tick(self, state, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(reminders, "_upcoming_meetings", lambda moment, minutes: calls.append(moment) or [])
        now = at("2026-09-01T10:00")
        reminders.pending(now, OWNER)
        reminders.pending(now + timedelta(seconds=20), OWNER)
        reminders.pending(now + timedelta(seconds=61), OWNER)
        reminders.pending(now + timedelta(seconds=20), KIRILL)  # у другого человека свой счётчик
        assert len(calls) == 3

    def test_disabled_with_zero(self, state, monkeypatch) -> None:
        monkeypatch.setattr(reminders, "settings", replace(state, meeting_remind_minutes=0))
        monkeypatch.setattr(reminders, "_upcoming_meetings", lambda *a: pytest.fail("календарь не нужен"))
        reminders.pending(at("2026-09-01T10:00"), OWNER)


class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Events:
    def __init__(self, event):
        self.event = event
        self.patched = None

    def get(self, calendarId, eventId):
        return _Request(self.event)

    def patch(self, calendarId, eventId, body, sendUpdates):
        self.patched = body
        return _Request({**self.event, **body})


class _Calendar:
    def __init__(self, event):
        self._events = _Events(event)

    def events(self):
        return self._events


class TestCalendarEdits:
    EVENT = {
        "id": "ev1",
        "summary": "Планёрка",
        "start": {"dateTime": "2026-09-02T10:00:00+03:00"},
        "end": {"dateTime": "2026-09-02T11:30:00+03:00"},
        "attendees": [{"email": "a@x.ru"}],
    }

    def test_move_keeps_duration_and_adds_attendees(self, monkeypatch) -> None:
        fake = _Calendar(dict(self.EVENT))
        monkeypatch.setattr(calendar_tools, "_calendar", lambda: fake)
        content, is_error = registry.execute(
            "calendar_update_event",
            {"event_id": "ev1", "start": "2026-09-03T15:00", "reminder_minutes": 15,
             "add_attendees": ["A@x.ru", "b@x.ru"]},
        )
        assert not is_error, content
        body = fake.events().patched
        assert body["end"]["dateTime"].startswith("2026-09-03T16:30")
        assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 15}]
        assert [a["email"] for a in body["attendees"]] == ["a@x.ru", "b@x.ru"]

    def test_cards_name_the_meeting(self, monkeypatch) -> None:
        monkeypatch.setattr(calendar_tools, "_calendar", lambda: _Calendar(dict(self.EVENT)))
        update = registry.get("calendar_update_event").build_preview({"event_id": "ev1", "start": "2026-09-03T15:00"})
        delete = registry.get("calendar_delete_event").build_preview({"event_id": "ev1"})
        assert "Планёрка" in update.summary and "2026-09-02 10:00" in update.summary
        assert "Планёрка" in delete.summary

    def test_card_survives_calendar_failure(self, monkeypatch) -> None:
        def broken():
            raise RuntimeError("нет сети")

        monkeypatch.setattr(calendar_tools, "_calendar", broken)
        delete = registry.get("calendar_delete_event").build_preview({"event_id": "ev9"})
        assert "ev9" in delete.summary
