"""Ядро агента: цикл работы с Claude, вызов инструментов и шлюз подтверждений.

Цикл написан вручную, а не на tool_runner, по одной причине: действия,
меняющие данные, должны приостанавливать выполнение между HTTP-запросами —
агент отдаёт карточку подтверждения, процесс завершается, а после ответа
пользователя цикл возобновляется с сохранённого состояния.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .config import settings
from .integrations import google_client
from .llm import LLMError, build_backend
from .kb import knowledge_base
from .prompts import SYSTEM_PROMPT, runtime_context
from .tools import registry

logger = logging.getLogger(__name__)

# Серверные инструменты Anthropic: выполняются на стороне API, локального кода не требуют.
WEB_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": settings.web_search_max_uses},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": settings.web_search_max_uses},
]

DECLINED_TEMPLATE = (
    "Пользователь отклонил это действие — оно НЕ выполнено. Не повторяй вызов этого "
    "инструмента. Спроси, что нужно изменить, либо предложи альтернативу."
    "{comment}"
)


@dataclass
class PendingAction:
    """Вызов инструмента, ожидающий решения пользователя."""

    tool_use_id: str
    name: str
    tool_input: dict[str, Any]
    preview: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "input": self.tool_input,
            **self.preview,
        }


@dataclass
class PendingTurn:
    """Замороженный ход: уже посчитанные результаты + действия на подтверждении."""

    completed_results: list[dict[str, Any]] = field(default_factory=list)
    actions: list[PendingAction] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingTurn | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})

    @property
    def awaiting_confirmation(self) -> bool:
        return self.pending is not None


class AgentError(Exception):
    pass


class OperonAgent:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None
        # Сообщения с role="system" внутри messages поддерживают не все модели.
        self._supports_system_messages = True
        # Параметры, которые шлюз отверг: повторяем запрос уже без них.
        self._unsupported_params: set[str] = set()

    # --- клиент -------------------------------------------------------------

    @property
    def client(self) -> Any:
        """Бэкенд выбранного протокола. Интерфейс одинаков для всех шлюзов."""
        if self._client is None:
            if not settings.api_key:
                raise AgentError(
                    "Не задан ключ доступа к модели. Укажите OPERON_LLM_API_KEY "
                    "(или ANTHROPIC_API_KEY / OPENROUTER_API_KEY) в переменных окружения."
                )
            if not settings.model:
                raise AgentError(
                    "Не задано имя модели. Укажите OPERON_MODEL — точное название "
                    "берётся из каталога вашего провайдера."
                )
            try:
                self._client = build_backend()
            except LLMError as exc:
                raise AgentError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise AgentError(
                    f"Не удалось подключиться к шлюзу ({settings.provider}): {exc}"
                ) from exc
        return self._client

    # --- сборка запроса -----------------------------------------------------

    def _tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if settings.web_search_enabled and self._param_enabled("web_tools"):
            tools.extend(WEB_TOOLS)
        tools.extend(registry.anthropic_tools())
        return tools

    def _system(self) -> list[dict[str, Any]]:
        # Единственный блок и единственная точка кэширования: промпт неизменен,
        # поэтому вместе с ним кэшируются и определения инструментов.
        block: dict[str, Any] = {"type": "text", "text": SYSTEM_PROMPT}
        if self._param_enabled("cache_control"):
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _api_messages(self, session: Session) -> list[dict[str, Any]]:
        if self._supports_system_messages:
            return session.messages
        # Фолбэк для моделей без system-сообщений в истории: вкладываем контекст
        # в предшествующую реплику пользователя.
        converted: list[dict[str, Any]] = []
        for message in session.messages:
            if message["role"] != "system":
                converted.append(message)
                continue
            block = {"type": "text", "text": f"<контекст_системы>\n{message['content']}\n</контекст_системы>"}
            if converted and converted[-1]["role"] == "user":
                content = converted[-1]["content"]
                if isinstance(content, str):
                    converted[-1] = {"role": "user", "content": [{"type": "text", "text": content}, block]}
                else:
                    converted[-1] = {"role": "user", "content": [*content, block]}
            else:
                converted.append({"role": "user", "content": [block]})
        return converted

    def _request_params(self, session: Session) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "system": self._system(),
            "messages": self._api_messages(session),
            "tools": self._tools(),
        }
        if self._param_enabled("output_config"):
            params["output_config"] = {"effort": settings.effort}
        if self._param_enabled("cache_control"):
            # Кэшируем хвост истории — на длинных диалогах это основная экономия.
            params["cache_control"] = {"type": "ephemeral"}
        return params

    def _param_enabled(self, name: str) -> bool:
        """Параметр включён настройками и ещё не отклонён провайдером."""
        if name in self._unsupported_params:
            return False
        if name == "output_config":
            return settings.effort_enabled
        if name == "cache_control":
            return settings.prompt_cache_enabled
        return True

    # --- публичный API ------------------------------------------------------

    def send_user_message(self, session: Session, text: str) -> Iterator[dict[str, Any]]:
        if session.awaiting_confirmation:
            yield {
                "type": "error",
                "message": "Ход приостановлен: сначала подтвердите или отклоните запрошенное действие.",
            }
            return

        session.messages.append({"role": "user", "content": text})
        session.messages.append({"role": "system", "content": self._runtime_context()})
        self._trim_history(session)
        yield from self._run_loop(session)

    def resume_with_decisions(
        self, session: Session, decisions: dict[str, str], comments: dict[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        pending = session.pending
        if pending is None:
            yield {"type": "error", "message": "Нет действий, ожидающих подтверждения."}
            return

        comments = comments or {}
        results = list(pending.completed_results)

        for action in pending.actions:
            decision = (decisions.get(action.tool_use_id) or "reject").lower()
            if decision in {"approve", "approved", "yes", "confirm", "да"}:
                yield {"type": "tool_start", "name": action.name, "activity": self._activity(action.name)}
                content, is_error = registry.execute(action.name, action.tool_input)
                yield {
                    "type": "tool_end",
                    "name": action.name,
                    "ok": not is_error,
                    "summary": self._summarize(action.name, content, is_error),
                    "confirmed": True,
                }
            else:
                comment = comments.get(action.tool_use_id, "").strip()
                content = DECLINED_TEMPLATE.format(
                    comment=f" Комментарий пользователя: «{comment}»." if comment else ""
                )
                is_error = False
                yield {
                    "type": "tool_declined",
                    "name": action.name,
                    "summary": f"Действие отклонено пользователем: {action.preview.get('title', action.name)}",
                }
            results.append(self._tool_result(action.tool_use_id, content, is_error))

        session.pending = None
        session.messages.append({"role": "user", "content": results})
        yield from self._run_loop(session)

    # --- основной цикл ------------------------------------------------------

    def _run_loop(self, session: Session) -> Iterator[dict[str, Any]]:
        for _ in range(settings.max_tool_iterations):
            try:
                response = yield from self._stream_turn(session)
            except anthropic.APIStatusError as exc:
                yield {"type": "error", "message": self._api_error_text(exc)}
                return
            except LLMError as exc:
                yield {"type": "error", "message": str(exc)}
                return
            except anthropic.APIConnectionError as exc:
                yield {
                    "type": "error",
                    "message": f"Нет связи с API Anthropic: {exc}. Проверьте сеть и повторите запрос.",
                }
                return
            except AgentError as exc:
                yield {"type": "error", "message": str(exc)}
                return

            session.messages.append({"role": "assistant", "content": response.content})
            self._track_usage(session, response)

            if response.stop_reason == "refusal":
                yield {
                    "type": "error",
                    "message": (
                        "Модель отклонила запрос по соображениям безопасности. "
                        "Переформулируйте задачу или разбейте её на части."
                    ),
                }
                return

            # Серверный инструмент исчерпал лимит итераций — повторяем запрос,
            # API продолжит с того же места. Дополнительное сообщение слать нельзя.
            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason == "max_tokens":
                yield {
                    "type": "warning",
                    "message": "Ответ обрезан по лимиту токенов. Попросите продолжить или сузьте вопрос.",
                }

            tool_uses = [block for block in response.content if getattr(block, "type", "") == "tool_use"]
            if not tool_uses:
                yield {"type": "done", "stop": "end_turn", "usage": dict(session.usage)}
                return

            results: list[dict[str, Any]] = []
            pending_actions: list[PendingAction] = []

            for block in tool_uses:
                spec = registry.get(block.name)
                tool_input = dict(block.input or {})

                if spec is not None and spec.requires_confirmation:
                    preview = spec.build_preview(tool_input).as_dict()
                    pending_actions.append(
                        PendingAction(
                            tool_use_id=block.id,
                            name=block.name,
                            tool_input=tool_input,
                            preview=preview,
                        )
                    )
                    continue

                yield {"type": "tool_start", "name": block.name, "activity": self._activity(block.name)}
                content, is_error = registry.execute(block.name, tool_input)
                yield {
                    "type": "tool_end",
                    "name": block.name,
                    "ok": not is_error,
                    "summary": self._summarize(block.name, content, is_error),
                }
                results.append(self._tool_result(block.id, content, is_error))

            if pending_actions:
                # Результаты уже выполненных инструментов придётся отдать вместе
                # с решениями пользователя — API требует все tool_result одним сообщением.
                session.pending = PendingTurn(completed_results=results, actions=pending_actions)
                yield {
                    "type": "confirmation_required",
                    "actions": [action.as_dict() for action in pending_actions],
                }
                yield {"type": "done", "stop": "awaiting_confirmation", "usage": dict(session.usage)}
                return

            session.messages.append({"role": "user", "content": results})

        yield {
            "type": "warning",
            "message": (
                f"Достигнут лимит в {settings.max_tool_iterations} обращений к инструментам за один ход. "
                "Задача может быть выполнена не полностью — уточните запрос или разбейте его."
            ),
        }
        yield {"type": "done", "stop": "iteration_limit", "usage": dict(session.usage)}

    def _stream_turn(self, session: Session) -> Iterator[dict[str, Any]]:
        """Один запрос к модели со стримингом текста. Возвращает финальное сообщение."""
        params = self._request_params(session)
        try:
            stream_ctx = self.client.stream(**params)
        except anthropic.BadRequestError as exc:
            if self._adapt_to_provider(exc):
                stream_ctx = self.client.stream(**self._request_params(session))
            else:
                raise

        try:
            with stream_ctx as stream:
                yield from self._consume_stream(stream)
                return stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if self._adapt_to_provider(exc):
                with self.client.stream(**self._request_params(session)) as stream:
                    yield from self._consume_stream(stream)
                    return stream.get_final_message()
            raise

    def _consume_stream(self, stream: Any) -> Iterator[dict[str, Any]]:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "content_block_start":
                block_type = getattr(event.content_block, "type", "")
                if block_type == "thinking":
                    yield {"type": "status", "state": "thinking", "message": "Анализирую"}
                elif block_type == "server_tool_use":
                    name = getattr(event.content_block, "name", "")
                    yield {
                        "type": "tool_start",
                        "name": name,
                        "activity": "Ищу в интернете" if name == "web_search" else "Открываю страницу",
                    }
                elif block_type in {"web_search_tool_result", "web_fetch_tool_result"}:
                    yield {"type": "tool_end", "name": block_type, "ok": True, "summary": "Данные из интернета получены"}
            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", "") == "text_delta":
                    yield {"type": "text_delta", "text": delta.text}

    def _adapt_to_provider(self, exc: anthropic.BadRequestError) -> bool:
        """Отключает то, что не принял шлюз, и сообщает, стоит ли повторить запрос.

        Прокси вроде OpenRouter принимают формат Anthropic, но не обязаны
        поддерживать все её расширения. Вместо падения снимаем спорный параметр
        и повторяем ход — один раз на каждый параметр.
        """
        message = str(exc).lower()

        if self._supports_system_messages and "system" in message and (
            "role" in message or "not supported" in message or "unsupported" in message
        ):
            logger.warning("Провайдер не принял системные сообщения в истории, включаю фолбэк")
            self._supports_system_messages = False
            return True

        # Ключ — фрагмент текста ошибки, значение — что именно отключаем.
        markers = {
            "output_config": "output_config",
            "effort": "output_config",
            "cache_control": "cache_control",
            "web_search": "web_tools",
            "web_fetch": "web_tools",
            "server_tool": "web_tools",
        }
        for marker, param in markers.items():
            if marker in message and param not in self._unsupported_params:
                logger.warning(
                    "Провайдер %s не принял «%s» — отключаю и повторяю запрос",
                    settings.provider,
                    param,
                )
                self._unsupported_params.add(param)
                return True
        return False

    # --- вспомогательное ----------------------------------------------------

    @staticmethod
    def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content or "(пустой результат)",
        }
        if is_error:
            block["is_error"] = True
        return block

    @staticmethod
    def _activity(name: str) -> str:
        spec = registry.get(name)
        return spec.activity if spec else "Выполняю действие"

    @staticmethod
    def _summarize(name: str, content: str, is_error: bool) -> str:
        if is_error:
            return content[:300]
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return f"{name}: готово"
        if not isinstance(payload, dict):
            return f"{name}: готово"

        status = payload.get("status", "")
        if status in {"empty_knowledge_base", "empty", "not_found"}:
            return "Данных не найдено"
        for key, label in (
            ("results_count", "фрагментов"),
            ("found", "файлов"),
            ("events_count", "событий"),
            ("count", "записей"),
            ("documents_count", "документов"),
            ("revisions_count", "версий"),
        ):
            if key in payload:
                return f"Найдено: {payload[key]} {label}"
        if status == "created":
            return "Создано"
        if status == "updated":
            return "Обновлено"
        if status == "deleted":
            return "Удалено"
        return "Готово"

    def _runtime_context(self) -> str:
        return runtime_context(
            kb_stats=knowledge_base.stats,
            integrations={
                "google": google_client.status(),
                "web_search": settings.web_search_enabled,
            },
        )

    @staticmethod
    def _track_usage(session: Session, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        session.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        session.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    @staticmethod
    def _api_error_text(exc: anthropic.APIStatusError) -> str:
        if isinstance(exc, anthropic.AuthenticationError):
            return "API-ключ Anthropic не принят. Проверьте ANTHROPIC_API_KEY."
        if isinstance(exc, anthropic.RateLimitError):
            return "Превышен лимит запросов к API. Подождите немного и повторите."
        if isinstance(exc, anthropic.NotFoundError):
            return f"Модель «{settings.model}» недоступна для этого ключа. Проверьте OPERON_MODEL."
        if exc.status_code and exc.status_code >= 500:
            return f"Сбой на стороне API Anthropic ({exc.status_code}). Повторите запрос позже."
        return f"Ошибка API Anthropic ({exc.status_code}): {getattr(exc, 'message', str(exc))}"

    @staticmethod
    def _trim_history(session: Session) -> None:
        """Обрезает историю, не разрывая пары tool_use / tool_result."""
        limit = settings.max_history_messages
        if len(session.messages) <= limit:
            return
        cut = len(session.messages) - limit
        while cut < len(session.messages):
            message = session.messages[cut]
            content = message.get("content")
            starts_with_tool_result = isinstance(content, list) and any(
                (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "tool_result"
                for block in content
            )
            if message["role"] == "user" and not starts_with_tool_result:
                break
            cut += 1
        if cut < len(session.messages):
            session.messages[:] = session.messages[cut:]


agent = OperonAgent()
