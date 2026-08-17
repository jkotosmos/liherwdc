"""Главный инвариант агента: ничего не создаётся и не меняется без подтверждения."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fakes import FakeClient, text_block, tool_use_block, turn

from app.agent import OperonAgent, Session
from app.config import settings
from app.storage import read_json


def build(script: list[Any]) -> tuple[OperonAgent, Session]:
    agent = OperonAgent()
    agent._client = FakeClient(script)
    return agent, Session(session_id="test")


def collect(events) -> list[dict[str, Any]]:
    return list(events)


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def stored_tasks() -> list[dict[str, Any]]:
    return read_json(settings.tasks_path, {"tasks": []}).get("tasks", [])


@pytest.fixture(autouse=True)
def clean_tasks():
    settings.tasks_path.unlink(missing_ok=True)
    yield
    settings.tasks_path.unlink(missing_ok=True)


class TestReadOnlyTools:
    def test_read_tool_runs_without_asking(self) -> None:
        agent, session = build(
            [
                turn(tool_use_block("t1", "tasks_list", {}), stop_reason="tool_use"),
                turn(text_block("Открытых поручений нет.")),
            ]
        )
        events = collect(agent.send_user_message(session, "Какие есть поручения?"))

        assert "confirmation_required" not in kinds(events)
        assert {"tool_start", "tool_end"} <= set(kinds(events))
        assert events[-1]["stop"] == "end_turn"
        assert session.pending is None


class TestWriteToolsAreGated:
    SCRIPT_CREATE = [
        turn(
            text_block("Зафиксирую поручение."),
            tool_use_block(
                "t1",
                "task_create",
                {"title": "Подготовить КП для партнёра", "assignee": "Иванов", "due_date": "2026-09-01"},
            ),
            stop_reason="tool_use",
        )
    ]

    def test_write_tool_suspends_turn_and_does_not_execute(self) -> None:
        agent, session = build(list(self.SCRIPT_CREATE))
        events = collect(agent.send_user_message(session, "Поставь задачу Иванову"))

        confirmations = [e for e in events if e["type"] == "confirmation_required"]
        assert len(confirmations) == 1
        assert events[-1]["stop"] == "awaiting_confirmation"

        # Ключевая проверка: побочного эффекта ещё нет.
        assert stored_tasks() == []
        assert session.awaiting_confirmation

    def test_confirmation_card_describes_the_action(self) -> None:
        agent, session = build(list(self.SCRIPT_CREATE))
        events = collect(agent.send_user_message(session, "Поставь задачу"))
        action = next(e for e in events if e["type"] == "confirmation_required")["actions"][0]

        assert action["name"] == "task_create"
        assert action["title"] == "Зафиксировать поручение"
        assert "Подготовить КП для партнёра" in action["summary"]
        assert action["details"]["Ответственный"] == "Иванов"
        assert action["tool_use_id"] == "t1"

    def test_approval_executes_the_action(self) -> None:
        agent, session = build([*self.SCRIPT_CREATE, turn(text_block("Готово, поручение записано."))])
        collect(agent.send_user_message(session, "Поставь задачу"))

        events = collect(agent.resume_with_decisions(session, {"t1": "approve"}))

        tasks = stored_tasks()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Подготовить КП для партнёра"
        assert tasks[0]["assignee"] == "Иванов"
        assert session.pending is None
        assert events[-1]["stop"] == "end_turn"

    def test_rejection_leaves_no_trace(self) -> None:
        agent, session = build([*self.SCRIPT_CREATE, turn(text_block("Понял, не записываю."))])
        collect(agent.send_user_message(session, "Поставь задачу"))

        events = collect(agent.resume_with_decisions(session, {"t1": "reject"}))

        assert stored_tasks() == []
        assert "tool_declined" in kinds(events)

    def test_rejection_is_reported_back_to_the_model(self) -> None:
        agent, session = build([*self.SCRIPT_CREATE, turn(text_block("Хорошо."))])
        collect(agent.send_user_message(session, "Поставь задачу"))
        collect(agent.resume_with_decisions(session, {"t1": "reject"}, {"t1": "срок неверный"}))

        results = session.messages[-2]["content"]
        assert results[0]["tool_use_id"] == "t1"
        assert "отклонил" in results[0]["content"]
        assert "срок неверный" in results[0]["content"]

    def test_missing_decision_defaults_to_reject(self) -> None:
        """Отсутствие ответа — не согласие."""
        agent, session = build([*self.SCRIPT_CREATE, turn(text_block("Хорошо."))])
        collect(agent.send_user_message(session, "Поставь задачу"))

        collect(agent.resume_with_decisions(session, {}))

        assert stored_tasks() == []

    def test_new_message_is_blocked_while_awaiting_confirmation(self) -> None:
        agent, session = build(list(self.SCRIPT_CREATE))
        collect(agent.send_user_message(session, "Поставь задачу"))

        events = collect(agent.send_user_message(session, "А ещё вопрос"))
        assert events[0]["type"] == "error"


class TestParallelToolCalls:
    def test_safe_tool_runs_while_write_tool_waits(self) -> None:
        agent, session = build(
            [
                turn(
                    tool_use_block("t1", "tasks_list", {}),
                    tool_use_block("t2", "task_create", {"title": "Новая задача"}),
                    stop_reason="tool_use",
                ),
                turn(text_block("Готово.")),
            ]
        )
        events = collect(agent.send_user_message(session, "Покажи задачи и добавь новую"))

        assert [e["name"] for e in events if e["type"] == "tool_end"] == ["tasks_list"]
        pending_ids = [a["tool_use_id"] for a in next(
            e for e in events if e["type"] == "confirmation_required")["actions"]]
        assert pending_ids == ["t2"]
        assert stored_tasks() == []

    def test_all_tool_results_return_in_one_message(self) -> None:
        """API требует, чтобы на каждый tool_use был tool_result в одном сообщении."""
        agent, session = build(
            [
                turn(
                    tool_use_block("t1", "tasks_list", {}),
                    tool_use_block("t2", "task_create", {"title": "Новая задача"}),
                    stop_reason="tool_use",
                ),
                turn(text_block("Готово.")),
            ]
        )
        collect(agent.send_user_message(session, "Покажи задачи и добавь новую"))
        collect(agent.resume_with_decisions(session, {"t2": "approve"}))

        results = session.messages[-2]["content"]
        assert {block["tool_use_id"] for block in results} == {"t1", "t2"}
        assert all(block["type"] == "tool_result" for block in results)


class TestLoopControl:
    def test_pause_turn_is_resent_without_extra_message(self) -> None:
        agent, session = build(
            [
                turn(text_block("Ищу…"), stop_reason="pause_turn"),
                turn(text_block("Нашёл.")),
            ]
        )
        events = collect(agent.send_user_message(session, "Поищи в интернете"))

        assert events[-1]["stop"] == "end_turn"
        # После pause_turn отправляется тот же диалог, без искусственного «продолжай».
        last_request = agent._client.requests[-1]
        assert last_request["messages"][-1]["role"] == "assistant"

    def test_iteration_limit_stops_the_loop(self, monkeypatch) -> None:
        # settings — frozen dataclass, поэтому подменяем ссылку копией.
        monkeypatch.setattr("app.agent.settings", replace(settings, max_tool_iterations=3))
        script = [turn(tool_use_block(f"t{i}", "tasks_list", {}), stop_reason="tool_use") for i in range(3)]
        agent, session = build(script)

        events = collect(agent.send_user_message(session, "Зациклись"))
        assert events[-1]["stop"] == "iteration_limit"
        assert any(e["type"] == "warning" for e in events)

    def test_refusal_is_surfaced_as_error(self) -> None:
        agent, session = build([turn(stop_reason="refusal")])
        events = collect(agent.send_user_message(session, "..."))
        assert events[-1]["type"] == "error"


class TestRequestShape:
    def test_runtime_context_goes_in_a_system_message(self) -> None:
        agent, session = build([turn(text_block("Ок."))])
        collect(agent.send_user_message(session, "Привет"))

        messages = agent._client.requests[0]["messages"]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "system"
        assert "Текущие дата и время" in messages[1]["content"]

    def test_static_prompt_is_cached_and_never_carries_the_date(self) -> None:
        agent, session = build([turn(text_block("Ок."))])
        collect(agent.send_user_message(session, "Привет"))

        request = agent._client.requests[0]
        system = request["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "Текущие дата" not in system[0]["text"]
        assert request["cache_control"] == {"type": "ephemeral"}

    def test_write_tools_are_declared_to_the_model(self) -> None:
        agent, session = build([turn(text_block("Ок."))])
        collect(agent.send_user_message(session, "Привет"))

        names = [tool.get("name") for tool in agent._client.requests[0]["tools"]]
        assert "task_create" in names
        assert "kb_search" in names

    def test_tool_order_is_stable_across_requests(self) -> None:
        """Нестабильный порядок инструментов ломает кэш промпта."""
        agent, session = build([turn(text_block("Раз.")), turn(text_block("Два."))])
        collect(agent.send_user_message(session, "Первый"))
        collect(agent.send_user_message(session, "Второй"))

        first, second = (json.dumps(r["tools"], sort_keys=True) for r in agent._client.requests[:2])
        assert first == second
