"""Срок подтверждения: «нет ответа = отказ» должно наступать по времени.

Логически правило работало и раньше — отсутствие решения трактовалось как
отказ. Но отказ, который никогда не наступает сам, оставляет ход замороженным
навсегда: пользователь закрыл Telegram, а диалог ждёт его вечно.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest

from app import agent as agent_module
from app.agent import EXPIRED_TEMPLATE, OperonAgent, PendingAction, PendingTurn, Session
from app.config import settings
from app.tools.base import Preview, registry


@pytest.fixture
def short_ttl(monkeypatch):
    """Срок в «одну минуту», который мы двигаем вручную через created_at."""
    monkeypatch.setattr(agent_module, "settings", replace(settings, confirmation_ttl_minutes=1))
    return 1


def make_action(tool_use_id: str = "t1", name: str = "calendar_create_event") -> PendingAction:
    return PendingAction(
        tool_use_id=tool_use_id,
        name=name,
        tool_input={"title": "Планёрка", "start": "2026-09-01T10:00"},
        preview=Preview(title="Создать событие", summary="Планёрка").as_dict(),
    )


def frozen_session(*actions: PendingAction, age_seconds: float = 0.0) -> Session:
    session = Session(session_id="s1")
    session.messages.append({"role": "user", "content": "поставь встречу"})
    session.messages.append({"role": "assistant", "content": []})
    session.pending = PendingTurn(actions=list(actions or [make_action()]))
    session.pending.created_at = time.monotonic() - age_seconds
    return session


class TestExpiry:
    def test_fresh_pending_is_awaited(self, short_ttl) -> None:
        session = frozen_session(age_seconds=5)
        assert session.awaiting_confirmation is True
        assert session.pending_expired is False

    def test_expired_pending_stops_being_awaited(self, short_ttl) -> None:
        """Иначе запоздалое «подтверждаю» выполнило бы отменённое действие."""
        session = frozen_session(age_seconds=120)
        assert session.pending_expired is True
        assert session.awaiting_confirmation is False

    def test_zero_ttl_means_no_deadline(self, monkeypatch) -> None:
        monkeypatch.setattr(
            agent_module, "settings", replace(settings, confirmation_ttl_minutes=0)
        )
        session = frozen_session(age_seconds=10_000)
        assert session.pending_expired is False

    def test_minutes_left_counts_down(self, monkeypatch) -> None:
        monkeypatch.setattr(
            agent_module, "settings", replace(settings, confirmation_ttl_minutes=30)
        )
        session = frozen_session(age_seconds=600)
        assert 19 <= session.pending.minutes_left <= 20


class TestExpiryClosesTheTurn:
    """Ключевое: действие не выполняется, а модель узнаёт, что оно отменено."""

    def test_expired_action_is_not_executed(self, short_ttl, monkeypatch) -> None:
        executed: list[str] = []
        monkeypatch.setattr(
            registry,
            "execute",
            lambda name, payload: (executed.append(name), ("{}", False))[1],
        )
        agent = OperonAgent()
        monkeypatch.setattr(agent, "_run_loop", lambda session: iter(()))

        session = frozen_session(age_seconds=120)
        events = list(agent.expire_pending(session))

        assert executed == [], "просроченное действие выполнять нельзя"
        assert any(e["type"] == "tool_declined" for e in events)
        assert any("не получено" in e.get("message", "") for e in events if e["type"] == "warning")
        assert session.pending is None, "ход обязан разморозиться"

    def test_model_is_told_about_the_timeout(self, short_ttl, monkeypatch) -> None:
        agent = OperonAgent()
        monkeypatch.setattr(agent, "_run_loop", lambda session: iter(()))
        session = frozen_session(age_seconds=120)

        list(agent.expire_pending(session))

        results = session.messages[-1]["content"]
        assert results[0]["type"] == "tool_result"
        assert "не ответил" in results[0]["content"]
        assert "НЕ выполнено" in results[0]["content"]

    def test_explicit_approval_survives_the_timeout(self, short_ttl, monkeypatch) -> None:
        """Нажатую кнопку уважаем: по ней пользователь высказался, молчания не было."""
        executed: list[str] = []

        def fake_execute(name: str, payload: dict[str, Any]) -> tuple[str, bool]:
            executed.append(name)
            return '{"status": "created"}', False

        monkeypatch.setattr(registry, "execute", fake_execute)
        agent = OperonAgent()
        monkeypatch.setattr(agent, "_run_loop", lambda session: iter(()))

        session = frozen_session(
            make_action("t1"), make_action("t2", "task_create"), age_seconds=120
        )
        list(agent.expire_pending(session, {"t1": "approve"}))

        assert executed == ["calendar_create_event"], "решённое — выполняем, молчание — отклоняем"
        results = session.messages[-1]["content"]
        assert results[1]["content"].startswith("Пользователь не ответил")

    def test_late_decision_cannot_revive_the_action(self, short_ttl, monkeypatch) -> None:
        """resume_with_decisions на просроченном ходе не должен вызываться из UI."""
        session = frozen_session(age_seconds=120)
        assert session.awaiting_confirmation is False


class TestNextMessageClosesExpiredTurn:
    """Веб-интерфейс никто не опрашивает: срок закрывается следующей репликой."""

    def test_new_message_folds_in_the_rejection(self, short_ttl, monkeypatch) -> None:
        agent = OperonAgent()
        monkeypatch.setattr(agent, "_run_loop", lambda session: iter(()))
        monkeypatch.setattr(agent, "_runtime_context", lambda: "контекст")

        session = frozen_session(age_seconds=120)
        events = list(agent.send_user_message(session, "а что по отчёту?"))

        assert any(e["type"] == "warning" for e in events)
        # tool_result и новая реплика уходят одним сообщением: API требует,
        # чтобы результат инструмента шёл сразу за его вызовом.
        user_message = session.messages[2]
        assert user_message["role"] == "user"
        assert user_message["content"][0]["type"] == "tool_result"
        assert user_message["content"][-1]["text"] == "а что по отчёту?"

    def test_live_pending_still_blocks_new_messages(self, short_ttl) -> None:
        agent = OperonAgent()
        session = frozen_session(age_seconds=5)
        events = list(agent.send_user_message(session, "а что по отчёту?"))
        assert events[0]["type"] == "error"
        assert session.pending is not None, "живое подтверждение снимать нельзя"


class TestTemplate:
    def test_timeout_wording_forbids_silent_retry(self) -> None:
        text = EXPIRED_TEMPLATE.format(minutes=30)
        assert "НЕ" in text and "выполнено" in text
        assert "не повторяй" in text.lower()
