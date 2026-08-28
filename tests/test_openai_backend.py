"""Совместимость со шлюзами, говорящими на OpenAI Chat Completions.

Реального шлюза в тестах нет, поэтому HTTP подменён заглушкой: проверяем
перевод запроса туда и разбор потока обратно.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
from types import SimpleNamespace
import pytest

from app import llm
from app.config import settings
from app.llm import OpenAICompatBackend, TextBlock, ToolUseBlock


def sse(*chunks: dict | str) -> bytes:
    lines = []
    for chunk in chunks:
        payload = chunk if isinstance(chunk, str) else json.dumps(chunk, ensure_ascii=False)
        lines.append(f"data: {payload}\n\n")
    return "".join(lines).encode("utf-8")


def backend_with(body: bytes, status: int = 200, capture: list | None = None) -> OpenAICompatBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(json.loads(request.content))
        return httpx.Response(status, content=body, headers={"Content-Type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatBackend(client=client)


@pytest.fixture(autouse=True)
def gateway_settings(monkeypatch):
    monkeypatch.setattr(
        llm, "settings", replace(settings, base_url="https://gateway.test/v1", api_key="k")
    )


class TestRequestTranslation:
    def _params(self) -> dict:
        return {
            "model": "some-model",
            "max_tokens": 1000,
            "system": [{"type": "text", "text": "Ты ассистент."}],
            "messages": [
                {"role": "user", "content": "Найди тарифы"},
                {"role": "system", "content": "Сегодня 2026-08-18"},
                {
                    "role": "assistant",
                    "content": [
                        TextBlock(text="Ищу"),
                        ToolUseBlock(id="call_1", name="kb_search", input={"query": "тарифы"}),
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "{\"ok\":1}"}
                    ],
                },
            ],
            "tools": [
                {
                    "name": "kb_search",
                    "description": "Поиск",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
                # Серверный инструмент Anthropic — у него нет схемы.
                {"type": "web_search_20260209", "name": "web_search"},
            ],
        }

    def test_system_prompt_becomes_first_message(self) -> None:
        sent = OpenAICompatBackend._convert_messages(self._params())
        assert sent[0] == {"role": "system", "content": "Ты ассистент."}

    def test_assistant_tool_call_translated(self) -> None:
        sent = OpenAICompatBackend._convert_messages(self._params())
        assistant = next(m for m in sent if m["role"] == "assistant")
        assert assistant["content"] == "Ищу"
        call = assistant["tool_calls"][0]
        assert call["id"] == "call_1"
        assert call["function"]["name"] == "kb_search"
        assert json.loads(call["function"]["arguments"]) == {"query": "тарифы"}

    def test_tool_result_becomes_tool_role_message(self) -> None:
        sent = OpenAICompatBackend._convert_messages(self._params())
        tool_message = next(m for m in sent if m["role"] == "tool")
        assert tool_message["tool_call_id"] == "call_1"
        assert tool_message["content"] == '{"ok":1}'

    def test_tools_translated_and_server_tools_skipped(self) -> None:
        """Серверные инструменты Anthropic на чужом шлюзе не выполняются."""
        tools = OpenAICompatBackend._convert_tools(self._params())
        assert [t["function"]["name"] for t in tools] == ["kb_search"]
        assert tools[0]["function"]["parameters"]["type"] == "object"

    def test_streaming_is_requested(self) -> None:
        captured: list[dict] = []
        backend = backend_with(sse({"choices": [{"delta": {}}]}, "[DONE]"), capture=captured)
        with backend.stream(**self._params()) as stream:
            list(stream)
        assert captured[0]["stream"] is True
        assert captured[0]["model"] == "some-model"


class TestResponseParsing:
    PARAMS = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "привет"}]}

    def test_text_is_streamed_then_assembled(self) -> None:
        backend = backend_with(
            sse(
                {"choices": [{"delta": {"content": "Привет"}}]},
                {"choices": [{"delta": {"content": ", коллега"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                "[DONE]",
            )
        )
        with backend.stream(**self.PARAMS) as stream:
            deltas = [
                event.delta.text
                for event in stream
                if getattr(event, "type", "") == "content_block_delta"
            ]
            message = stream.get_final_message()

        assert "".join(deltas) == "Привет, коллега"
        assert message.content[0].text == "Привет, коллега"
        assert message.stop_reason == "end_turn"

    def test_tool_call_assembled_from_fragments(self) -> None:
        """Аргументы приходят кусками строки — их надо склеить и разобрать."""
        backend = backend_with(
            sse(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_9", "function": {"name": "task_create", "arguments": ""}}
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"title": "Под'}}
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": 'готовить КП"}'}}
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            )
        )
        with backend.stream(**self.PARAMS) as stream:
            list(stream)
            message = stream.get_final_message()

        block = message.content[0]
        assert block.type == "tool_use"
        assert block.id == "call_9"
        assert block.name == "task_create"
        assert block.input == {"title": "Подготовить КП"}
        assert message.stop_reason == "tool_use"

    def test_parallel_tool_calls_kept_separate(self) -> None:
        backend = backend_with(
            sse(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "tasks_list", "arguments": "{}"}},
                    {"index": 1, "id": "b", "function": {"name": "kb_search", "arguments": '{"query":"x"}'}},
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            )
        )
        with backend.stream(**self.PARAMS) as stream:
            list(stream)
            message = stream.get_final_message()

        assert [b.name for b in message.content] == ["tasks_list", "kb_search"]

    def test_broken_arguments_do_not_crash(self) -> None:
        backend = backend_with(
            sse(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "tasks_list", "arguments": "{не json"}}
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            )
        )
        with backend.stream(**self.PARAMS) as stream:
            list(stream)
            message = stream.get_final_message()
        assert message.content[0].input == {}

    def test_usage_is_collected(self) -> None:
        backend = backend_with(
            sse(
                {"choices": [{"delta": {"content": "ок"}}]},
                {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}},
                "[DONE]",
            )
        )
        with backend.stream(**self.PARAMS) as stream:
            list(stream)
            message = stream.get_final_message()
        assert message.usage.input_tokens == 120
        assert message.usage.output_tokens == 8

    @pytest.mark.parametrize(
        ("finish", "expected"),
        [("stop", "end_turn"), ("tool_calls", "tool_use"), ("length", "max_tokens")],
    )
    def test_finish_reason_mapping(self, finish: str, expected: str) -> None:
        backend = backend_with(sse({"choices": [{"delta": {}, "finish_reason": finish}]}, "[DONE]"))
        with backend.stream(**self.PARAMS) as stream:
            list(stream)
            assert stream.get_final_message().stop_reason == expected


class TestErrors:
    PARAMS = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "п"}]}

    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            (401, "Ключ не принят"),
            (403, "подписк"),
            (404, "OPERON_LLM_BASE_URL"),
            (429, "лимит"),
        ],
    )
    def test_http_errors_explain_the_cause(self, status: int, fragment: str) -> None:
        backend = backend_with(b'{"error":{"message":"detail"}}', status=status)
        with pytest.raises(llm.LLMError) as info:
            with backend.stream(**self.PARAMS):
                pass
        assert fragment.lower() in str(info.value).lower()

    def test_missing_base_url_is_reported(self, monkeypatch) -> None:
        for name in ("OPERON_LLM_BASE_URL", "ANTHROPIC_BASE_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(llm, "settings", replace(settings, base_url="", api_key="k"))
        with pytest.raises(llm.LLMError, match="OPERON_LLM_BASE_URL"):
            OpenAICompatBackend()


class TestGatewayLimits:
    """Каталог стороннего шлюза заранее неизвестен: потолок ответа у каждой модели свой."""

    def _agent_rejecting_max_tokens(self, monkeypatch, message: str):
        import anthropic

        from app.agent import OperonAgent, Session

        agent = OperonAgent()
        attempts: list[int] = []

        class DoneStream:
            """Минимальный успешный ответ: ходу достаточно, чтобы завершиться."""

            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def __iter__(self): return iter(())

            @staticmethod
            def get_final_message():
                return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

        class Backend:
            def stream(self, **params):
                attempts.append(params["max_tokens"])
                if len(attempts) == 1:
                    raise anthropic.BadRequestError(
                        message=message,
                        response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                        body=None,
                    )
                return DoneStream()

        agent._client = Backend()
        monkeypatch.setattr(agent, "_runtime_context", lambda: "к")
        return agent, attempts, Session(session_id="s")

    def test_too_large_max_tokens_is_reduced_and_retried(self, monkeypatch) -> None:
        agent, attempts, session = self._agent_rejecting_max_tokens(
            monkeypatch, "max_tokens must be less than or equal to 8192"
        )
        list(agent.send_user_message(session, "привет"))

        assert len(attempts) == 2, "запрос должен быть повторён"
        assert attempts[1] < attempts[0], "со сниженным лимитом"
        assert attempts[1] >= 1024, "но не до бессмысленно малого"

    def test_unrelated_error_is_not_retried_as_a_limit(self, monkeypatch) -> None:
        agent, attempts, session = self._agent_rejecting_max_tokens(
            monkeypatch, "model not found"
        )
        events = list(agent.send_user_message(session, "привет"))
        assert len(attempts) == 1, "чужая ошибка не должна трактоваться как лимит"
        assert any(e["type"] == "error" for e in events)

    def test_reduced_cap_is_remembered(self, monkeypatch) -> None:
        agent, attempts, session = self._agent_rejecting_max_tokens(
            monkeypatch, "max_tokens is too large for this model"
        )
        list(agent.send_user_message(session, "привет"))
        assert agent._max_tokens_cap == attempts[1], "чтобы не упираться в тот же отказ каждый ход"
