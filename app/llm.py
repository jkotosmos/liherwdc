"""Слой доступа к модели: один агент — любой шлюз.

ТЗ требует «агента на любой базе», и на практике шлюзы делятся на два
протокола:

* **Anthropic Messages API** — `/v1/messages` (сама Anthropic и совместимые
  прокси);
* **OpenAI Chat Completions** — `/chat/completions` (подавляющее большинство
  агрегаторов и подписочных «AI-роутеров»).

Оба бэкенда выдают наружу одинаковый поток событий и одинаковый объект
ответа, поэтому цикл агента, шлюз подтверждений и инструменты не знают,
через что именно идёт запрос.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=20.0)


# --- нормализованный ответ -------------------------------------------------
# Повторяет форму блоков Anthropic: остальной код работает с ними одинаково.


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class NormalizedMessage:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Any = field(default_factory=lambda: SimpleNamespace(input_tokens=0, output_tokens=0))


class LLMError(Exception):
    """Ошибка обращения к шлюзу с текстом, пригодным для показа пользователю."""


# --- бэкенд Anthropic ------------------------------------------------------


class AnthropicBackend:
    """Прямой путь: SDK сам умеет всё, что нужно."""

    protocol = "anthropic"

    def __init__(self) -> None:
        import anthropic

        options: dict[str, Any] = {"api_key": settings.api_key}
        if settings.base_url:
            options["base_url"] = settings.base_url
        if settings.extra_headers:
            options["default_headers"] = settings.extra_headers
        self._client = anthropic.Anthropic(**options)

    def stream(self, **params: Any) -> Any:
        return self._client.messages.stream(**params)


# --- бэкенд OpenAI Chat Completions ---------------------------------------


def _blocks_of(message: dict[str, Any]) -> list[Any]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _block_attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = [
        _block_attr(b, "text", "")
        for b in _blocks_of(message)
        if _block_attr(b, "type") == "text"
    ]
    return "\n".join(p for p in parts if p)


class OpenAICompatBackend:
    """Переводит запрос в формат Chat Completions и обратно.

    Поддерживает вызов инструментов и потоковую выдачу — этого достаточно
    для полного цикла агента, включая шлюз подтверждений.
    """

    protocol = "openai"

    def __init__(self, client: httpx.Client | None = None) -> None:
        base = settings.base_url or ""
        if not base:
            raise LLMError(
                "Для шлюза не задан адрес. Укажите OPERON_LLM_BASE_URL — его "
                "публикует ваш провайдер (обычно вида https://.../v1)."
            )
        self._url = base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            **settings.extra_headers,
        }
        # client передаётся в тестах, чтобы подставить транспорт-заглушку.
        self._client = client or httpx.Client(headers=headers, timeout=REQUEST_TIMEOUT)

    # --- преобразование запроса ---

    @staticmethod
    def _convert_messages(params: dict[str, Any]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []

        system = params.get("system")
        if isinstance(system, list):
            text = "\n\n".join(block.get("text", "") for block in system if isinstance(block, dict))
        else:
            text = system or ""
        if text:
            converted.append({"role": "system", "content": text})

        for message in params.get("messages", []):
            role = message.get("role")

            if role == "system":
                converted.append({"role": "system", "content": _text_of(message)})
                continue

            if role == "user":
                # Результаты инструментов уходят отдельными сообщениями role=tool.
                results = [b for b in _blocks_of(message) if _block_attr(b, "type") == "tool_result"]
                if results:
                    for block in results:
                        converted.append(
                            {
                                "role": "tool",
                                "tool_call_id": _block_attr(block, "tool_use_id", ""),
                                "content": str(_block_attr(block, "content", "")),
                            }
                        )
                    continue
                converted.append({"role": "user", "content": _text_of(message)})
                continue

            if role == "assistant":
                tool_calls = [
                    {
                        "id": _block_attr(b, "id", ""),
                        "type": "function",
                        "function": {
                            "name": _block_attr(b, "name", ""),
                            "arguments": json.dumps(_block_attr(b, "input", {}) or {}, ensure_ascii=False),
                        },
                    }
                    for b in _blocks_of(message)
                    if _block_attr(b, "type") == "tool_use"
                ]
                entry: dict[str, Any] = {"role": "assistant", "content": _text_of(message) or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                converted.append(entry)

        return converted

    @staticmethod
    def _convert_tools(params: dict[str, Any]) -> list[dict[str, Any]]:
        tools = []
        for tool in params.get("tools", []):
            # Серверные инструменты Anthropic (web_search и подобные) не имеют
            # схемы и на стороне чужого шлюза не выполняются — пропускаем.
            if "input_schema" not in tool:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"],
                    },
                }
            )
        return tools

    def _build_body(self, params: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": params["model"],
            "max_tokens": params.get("max_tokens", 4096),
            "messages": self._convert_messages(params),
            "stream": True,
            # Часть шлюзов отдаёт расход токенов только по явному запросу.
            "stream_options": {"include_usage": True},
        }
        tools = self._convert_tools(params)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    # --- выполнение ---

    def stream(self, **params: Any) -> "_OpenAIStream":
        return _OpenAIStream(self._client, self._url, self._build_body(params))


class _OpenAIStream:
    """Контекст-менеджер с тем же интерфейсом, что и поток Anthropic SDK."""

    def __init__(self, client: httpx.Client, url: str, body: dict[str, Any]) -> None:
        self._client = client
        self._url = url
        self._body = body
        self._response: httpx.Response | None = None
        self._message = NormalizedMessage()

    def __enter__(self) -> "_OpenAIStream":
        context = self._client.stream("POST", self._url, json=self._body)
        self._context = context
        response = context.__enter__()
        if response.status_code >= 400:
            response.read()
            context.__exit__(None, None, None)
            raise _http_error(response)
        self._response = response
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._context.__exit__(*exc_info)
        return False

    def __iter__(self) -> Iterator[Any]:
        """Отдаёт события в форме Anthropic, чтобы агент не различал бэкенды."""
        assert self._response is not None
        text_parts: list[str] = []
        # Аргументы инструмента приходят кусками строки — копим по индексу.
        calls: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        text_started = False

        for line in self._response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if chunk.get("usage"):
                usage = chunk["usage"]
                self._message.usage = SimpleNamespace(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                )

            for choice in chunk.get("choices", []):
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                piece = delta.get("content")
                if piece:
                    if not text_started:
                        text_started = True
                        yield SimpleNamespace(
                            type="content_block_start",
                            content_block=SimpleNamespace(type="text"),
                        )
                    text_parts.append(piece)
                    yield SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text=piece),
                    )

                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]

        self._message.content = self._assemble(text_parts, calls)
        self._message.stop_reason = {
            "tool_calls": "tool_use",
            "stop": "end_turn",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(finish_reason, "end_turn")

    @staticmethod
    def _assemble(text_parts: list[str], calls: dict[int, dict[str, str]]) -> list[Any]:
        blocks: list[Any] = []
        text = "".join(text_parts)
        if text.strip():
            blocks.append(TextBlock(text=text))
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                logger.warning("Шлюз прислал неразбираемые аргументы для %s", slot["name"])
                arguments = {}
            blocks.append(
                ToolUseBlock(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    input=arguments if isinstance(arguments, dict) else {},
                )
            )
        return blocks

    def get_final_message(self) -> NormalizedMessage:
        return self._message


def _http_error(response: httpx.Response) -> LLMError:
    detail = ""
    try:
        payload = response.json()
        detail = (payload.get("error") or {}).get("message") or payload.get("message") or ""
    except (ValueError, AttributeError):
        detail = (response.text or "")[:400]

    hints = {
        401: "Ключ не принят шлюзом. Проверьте OPERON_LLM_API_KEY.",
        403: "Шлюз отказал в доступе. Возможно, модель не входит в вашу подписку.",
        404: "Адрес или модель не найдены. Проверьте OPERON_LLM_BASE_URL и OPERON_MODEL.",
        429: "Превышен лимит подписки или частота запросов.",
    }
    hint = hints.get(response.status_code, "")
    return LLMError(f"Шлюз ответил {response.status_code}. {hint} {detail}".strip())


# --- выбор бэкенда ---------------------------------------------------------


def build_backend() -> Any:
    protocol = settings.llm_protocol
    if protocol == "openai":
        return OpenAICompatBackend()
    return AnthropicBackend()
