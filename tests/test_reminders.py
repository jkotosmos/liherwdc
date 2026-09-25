"""Напоминания: единственная часть системы, которая говорит первой.

Здесь проверяется не «красиво ли написан текст», а три вещи, от которых
зависит, будут ли напоминания читать: приходят ли они вовремя, приходят ли
ровно один раз и молчат ли, когда сказать нечего.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from app import reminders
from app.config import settings


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Свежий файл отправленного и предсказуемые настройки."""
    conf = replace(settings, data_dir=tmp_path, digest_hour=9, remind_before_days=2)
    monkeypatch.setattr(reminders, "settings", conf)
    monkeypatch.setenv("OPERON_REMINDERS", "true")
    monkeypatch.setenv("OPERON_QUIET_HOURS", "22-8")
    monkeypatch.setenv("OPERON_DIGEST_WEEKDAYS", "1-5")
    return conf


def at(day: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:30:00").replace(tzinfo=settings.tz)


def task(title: str, due: str, assignee: str = "Иванов") -> dict:
    return {"id": "T-0001", "title": title, "due_date": due, "assignee": assignee}


def stub_data(monkeypatch, overdue=(), upcoming=(), events=()):
    monkeypatch.setattr(reminders, "_overdue_and_upcoming", lambda today: (list(overdue), list(upcoming)))
    monkeypatch.setattr(reminders, "_today_events", lambda today: list(events))


# 2026-08-31 — понедельник, 2026-09-05 — суббота.
MONDAY = "2026-08-31"
SATURDAY = "2026-09-05"


class TestTiming:
    def test_nothing_before_the_digest_hour(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        assert reminders.pending(at(MONDAY, 8)) == []

    def test_digest_arrives_at_the_appointed_hour(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        plan = reminders.pending(at(MONDAY, 9))
        assert len(plan) == 1
        assert "Отчёт" in plan[0].text

    def test_late_start_still_delivers_today(self, clean_state, monkeypatch) -> None:
        """Бот лежал утром — сводка уйдёт при подъёме, но сегодняшняя."""
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        plan = reminders.pending(at(MONDAY, 15))
        assert len(plan) == 1

    def test_silence_at_night(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        assert reminders.pending(at(MONDAY, 3)) == []
        assert reminders.pending(at(MONDAY, 23)) == []

    def test_no_digest_on_weekends(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        assert reminders.pending(at(SATURDAY, 10)) == []

    def test_weekends_can_be_enabled(self, clean_state, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_DIGEST_WEEKDAYS", "1-7")
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        assert len(reminders.pending(at(SATURDAY, 10))) == 1

    def test_quiet_hours_wrap_midnight(self, clean_state) -> None:
        """22–8 — это интервал через полночь, а не пустой промежуток."""
        assert reminders.in_quiet_hours(at(MONDAY, 23)) is True
        assert reminders.in_quiet_hours(at(MONDAY, 3)) is True
        assert reminders.in_quiet_hours(at(MONDAY, 7)) is True
        assert reminders.in_quiet_hours(at(MONDAY, 9)) is False
        assert reminders.in_quiet_hours(at(MONDAY, 21)) is False

    def test_quiet_hours_can_be_disabled(self, clean_state, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_QUIET_HOURS", "0-0")
        assert reminders.in_quiet_hours(at(MONDAY, 3)) is False

    def test_daytime_quiet_interval(self, clean_state, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_QUIET_HOURS", "13-14")
        assert reminders.in_quiet_hours(at(MONDAY, 13)) is True
        assert reminders.in_quiet_hours(at(MONDAY, 14)) is False

    def test_switch_off_means_off(self, clean_state, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_REMINDERS", "false")
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        assert reminders.pending(at(MONDAY, 10)) == []


class TestExactlyOnce:
    """Повторяющееся напоминание раздражает сильнее, чем отсутствующее."""

    def test_second_call_the_same_day_is_silent(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        first = reminders.pending(at(MONDAY, 9))
        assert first
        reminders.mark_sent(first, at(MONDAY, 9))

        assert reminders.pending(at(MONDAY, 10)) == []
        assert reminders.pending(at(MONDAY, 18)) == []

    def test_next_day_it_comes_again(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        first = reminders.pending(at(MONDAY, 9))
        reminders.mark_sent(first, at(MONDAY, 9))

        assert len(reminders.pending(at("2026-09-01", 9))) == 1

    def test_restart_does_not_resend(self, clean_state, monkeypatch) -> None:
        """Ключи лежат на диске: передеплой не должен обернуться дублем."""
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        plan = reminders.pending(at(MONDAY, 9))
        reminders.mark_sent(plan, at(MONDAY, 9))

        # Имитируем перезапуск: состояние читается из файла заново.
        assert reminders.pending(at(MONDAY, 9)) == []
        assert settings.reminders_path.name == "reminders.json"

    def test_old_keys_are_pruned(self, clean_state, monkeypatch) -> None:
        stub_data(monkeypatch, overdue=[task("Отчёт", "2026-08-01")])
        long_ago = date(2026, 1, 1)
        reminders._remember({"digest:2026-01-01": long_ago.isoformat()}, [], date(2026, 8, 31))
        assert "digest:2026-01-01" not in reminders._sent_keys()


class TestSilenceWhenNothingToSay:
    def test_empty_digest_is_not_sent(self, clean_state, monkeypatch) -> None:
        """«Всё в порядке» каждый день — верный способ, чтобы бота перестали читать."""
        stub_data(monkeypatch)
        plan = reminders.pending(at(MONDAY, 9))
        assert len(plan) == 1
        assert plan[0].text == "", "текста нет — бот промолчит"

    def test_empty_digest_is_still_marked(self, clean_state, monkeypatch) -> None:
        """Иначе пустая сводка пересобиралась бы каждые полминуты до вечера."""
        stub_data(monkeypatch)
        plan = reminders.pending(at(MONDAY, 9))
        reminders.mark_sent(plan, at(MONDAY, 9))
        assert reminders.pending(at(MONDAY, 12)) == []


class TestDigestContent:
    def test_overdue_is_first_and_counted(self, clean_state, monkeypatch) -> None:
        text = reminders.build_digest(
            date(2026, 8, 31),
            events=[],
            overdue=[task("Акт сверки", "2026-08-20")],
            upcoming=[task("КП для Петрова", "2026-09-01")],
        )
        assert text.index("Просрочено") < text.index("Сроки на подходе")
        assert "Просрочено: 1" in text

    def test_days_are_spelled_out(self, clean_state) -> None:
        today = date(2026, 8, 31)
        assert "просрочено на 11 дн." in reminders._task_line(task("х", "2026-08-20"), today)
        assert "срок сегодня" in reminders._task_line(task("х", "2026-08-31"), today)
        assert "срок завтра" in reminders._task_line(task("х", "2026-09-01"), today)
        assert "срок через 3 дн." in reminders._task_line(task("х", "2026-09-03"), today)

    def test_assignee_is_shown(self, clean_state) -> None:
        line = reminders._task_line(task("Акт", "2026-09-01", assignee="Сидоров"), date(2026, 8, 31))
        assert "Сидоров" in line

    def test_meetings_are_listed_with_time(self, clean_state) -> None:
        text = reminders.build_digest(
            date(2026, 8, 31),
            events=[{"start": "2026-08-31T10:00:00+03:00", "title": "Планёрка"}],
            overdue=[],
            upcoming=[],
        )
        assert "10:00 Планёрка" in text

    def test_long_lists_are_truncated(self, clean_state) -> None:
        many = [task(f"Задача {i}", "2026-08-01") for i in range(25)]
        text = reminders.build_digest(date(2026, 8, 31), events=[], overdue=many, upcoming=[])
        assert "и ещё 15" in text

    def test_nothing_at_all_gives_empty_text(self, clean_state) -> None:
        assert reminders.build_digest(date(2026, 8, 31), [], [], []) == ""


class TestResilience:
    def test_calendar_failure_does_not_break_the_digest(self, clean_state, monkeypatch) -> None:
        """Отвалившийся Google не должен отменять напоминание о просрочках."""
        from app.integrations import google_client

        monkeypatch.setattr(google_client, "status", lambda: {"connected": True})
        monkeypatch.setattr(
            reminders,
            "_overdue_and_upcoming",
            lambda today: ([task("Отчёт", "2026-08-01")], []),
        )

        def broken(*args, **kwargs):
            raise RuntimeError("Google недоступен")

        monkeypatch.setattr("app.tools.calendar.list_events_between", broken)

        plan = reminders.pending(at(MONDAY, 9))
        assert len(plan) == 1
        assert "Отчёт" in plan[0].text

    def test_calendar_is_skipped_when_google_is_off(self, clean_state, monkeypatch) -> None:
        from app.integrations import google_client

        monkeypatch.setattr(google_client, "status", lambda: {"connected": False})
        assert reminders._today_events(date(2026, 8, 31)) == []

    def test_broken_due_date_does_not_crash(self, clean_state, monkeypatch) -> None:
        line = reminders._task_line({"title": "х", "due_date": "не дата"}, date(2026, 8, 31))
        assert "х" in line
