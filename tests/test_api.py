"""HTTP-слой: SSE-поток, подтверждения, статус."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import server
from app.sessions import store


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(server.app) as test_client:
        yield test_client


def sse_events(response) -> list[dict[str, Any]]:
    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class FakeAgent:
    """Заменяет реального агента, чтобы не ходить в API Anthropic."""

    def __init__(self, script: list[dict[str, Any]], *, fail: bool = False) -> None:
        self.script = script
        self.fail = fail
        self.resumed_with: dict[str, str] | None = None

    def send_user_message(self, session, text: str):
        if self.fail:
            raise RuntimeError("сломалось внутри агента")
        yield from self.script

    def resume_with_decisions(self, session, decisions, comments=None):
        self.resumed_with = decisions
        session.pending = None
        yield {"type": "done", "stop": "end_turn"}


class TestBasics:
    def test_health(self, client: TestClient) -> None:
        assert client.get("/api/health").json() == {"status": "ok"}

    def test_status_exposes_configuration(self, client: TestClient) -> None:
        payload = client.get("/api/status").json()
        assert payload["model"]
        assert payload["knowledge_base"]["documents"] == 0
        assert payload["google"]["connected"] is False
        assert any(tool["requires_confirmation"] for tool in payload["tools"])

    def test_index_page_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Ассистент OPERON" in response.text


class TestChat:
    def test_stream_returns_events_in_order(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(
            server,
            "agent",
            FakeAgent(
                [
                    {"type": "text_delta", "text": "Привет"},
                    {"type": "text_delta", "text": ", коллега"},
                    {"type": "done", "stop": "end_turn"},
                ]
            ),
        )
        response = client.post("/api/chat", json={"message": "Здравствуйте"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = sse_events(response)
        assert events[0]["type"] == "session"
        assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "Привет, коллега"

    def test_session_id_is_reused(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        first = sse_events(client.post("/api/chat", json={"message": "раз"}))[0]["session_id"]
        second = sse_events(
            client.post("/api/chat", json={"message": "два", "session_id": first})
        )[0]["session_id"]
        assert first == second

    def test_empty_message_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/chat", json={"message": ""}).status_code == 422

    def test_agent_crash_is_reported_in_the_stream(self, client: TestClient, monkeypatch) -> None:
        """Клиент не должен зависнуть на открытом соединении при внутренней ошибке."""
        monkeypatch.setattr(server, "agent", FakeAgent([], fail=True))
        events = sse_events(client.post("/api/chat", json={"message": "привет"}))
        assert events[-2]["type"] == "error"
        assert events[-1] == {"type": "done", "stop": "error"}


class TestConfirm:
    def test_unknown_session_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/confirm",
            json={"session_id": "нет-такой", "decisions": [{"tool_use_id": "t1", "decision": "approve"}]},
        )
        assert response.status_code == 404

    def test_confirm_without_pending_action_is_409(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]

        response = client.post(
            "/api/confirm",
            json={"session_id": session_id, "decisions": [{"tool_use_id": "t1", "decision": "approve"}]},
        )
        assert response.status_code == 409

    def test_invalid_decision_value_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/confirm",
            json={"session_id": "x", "decisions": [{"tool_use_id": "t1", "decision": "может быть"}]},
        )
        assert response.status_code == 422

    def test_decisions_reach_the_agent(self, client: TestClient, monkeypatch) -> None:
        fake = FakeAgent([{"type": "done", "stop": "awaiting_confirmation"}])
        monkeypatch.setattr(server, "agent", fake)
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]

        from app.agent import PendingTurn

        store.get(session_id).pending = PendingTurn()

        client.post(
            "/api/confirm",
            json={
                "session_id": session_id,
                "decisions": [{"tool_use_id": "t1", "decision": "reject", "comment": "не сейчас"}],
            },
        )
        assert fake.resumed_with == {"t1": "reject"}


class TestSessionReset:
    def test_reset_clears_history(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]
        store.get(session_id).messages.append({"role": "user", "content": "мусор"})

        client.post("/api/session/reset", json={"session_id": session_id})
        assert store.get(session_id).messages == []
