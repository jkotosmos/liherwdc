"""Каркас инструментов агента.

Ключевая идея: инструмент сам объявляет, меняет ли он что-то во внешнем мире.
Инструменты с ``requires_confirmation=True`` агент никогда не выполняет сам —
рантайм останавливает цикл и спрашивает пользователя.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import IntegrationUnavailable, ToolError

__all__ = [
    "IntegrationUnavailable",
    "Preview",
    "ToolError",
    "ToolRegistry",
    "ToolSpec",
    "registry",
    "serialize_result",
]


@dataclass(frozen=True)
class Preview:
    """Человекочитаемое описание действия для окна подтверждения."""

    title: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "summary": self.summary, "details": self.details}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    # Действие изменяет данные или отправляет их вовне -> только после подтверждения.
    requires_confirmation: bool = False
    # Строка для индикатора активности в чате («Ищу в базе знаний…»).
    activity: str = "Выполняю действие"
    # Формирует карточку подтверждения; по умолчанию показываем сырые аргументы.
    preview: Callable[[dict[str, Any]], Preview] | None = None

    def build_preview(self, tool_input: dict[str, Any]) -> Preview:
        if self.preview is not None:
            return self.preview(tool_input)
        return Preview(
            title=self.name,
            summary="Агент запрашивает разрешение на выполнение действия.",
            details=dict(tool_input),
        )

    def as_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"Инструмент {spec.name} уже зарегистрирован")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolSpec]:
        # Порядок стабилен — иначе ломается кэш промпта на стороне API.
        return [self._tools[name] for name in sorted(self._tools)]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [spec.as_anthropic_tool() for spec in self.all()]

    def execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Выполняет инструмент и возвращает (текст результата, признак ошибки)."""
        spec = self.get(name)
        if spec is None:
            return f"Неизвестный инструмент: {name}", True
        try:
            result = spec.handler(tool_input or {})
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 — модель должна увидеть текст сбоя
            return f"Сбой инструмента {name}: {exc.__class__.__name__}: {exc}", True
        return serialize_result(result), False


def serialize_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


registry = ToolRegistry()
