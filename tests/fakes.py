"""Поддельный клиент Anthropic: проигрывает заранее заданный сценарий ходов."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(tool_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def turn(
    *blocks: SimpleNamespace,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeStream:
    def __init__(self, message: SimpleNamespace) -> None:
        self._message = message

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def __iter__(self):
        for block in self._message.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=block.text),
                )

    def get_final_message(self) -> SimpleNamespace:
        return self._message


class FakeClient:
    """Подставляется в OperonAgent вместо бэкенда модели.

    Интерфейс тот же, что у AnthropicBackend и OpenAICompatBackend: stream(**params).
    """

    def __init__(self, script: list[SimpleNamespace]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    def stream(self, **params: Any) -> _FakeStream:
        self.requests.append(params)
        if not self._script:
            raise AssertionError("Модель вызвана больше раз, чем задано в сценарии")
        return _FakeStream(self._script.pop(0))
