"""Ядро агента: цикл работы с Claude, вызов инструментов и шлюз подтверждений.

Цикл написан вручную, а не на tool_runner, по одной причине: действия,
меняющие данные, должны приостанавливать выполнение между HTTP-запросами —
агент отдаёт карточку подтверждения, процесс завершается, а после ответа
пользователя цикл возобновляется с сохранённого состояния.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .config import settings
from .integrations import google_client
from .llm import LLMError, build_backend
from .model_choice import current_model
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

# Молчание — это отказ. Но отказ, который никогда не наступает, оставляет ход
# висеть вечно, поэтому у него есть срок.
EXPIRED_TEMPLATE = (
    "Пользователь не ответил на запрос подтверждения за {minutes} мин — действие НЕ "
    "выполнено и считается отклонённым. Не повторяй вызов автоматически: коротко "
    "сообщи об отмене и спроси, выполнять ли действие сейчас."
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
    # monotonic, а не время суток: перевод часов не должен отменять действие.
    created_at: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def expired(self) -> bool:
        ttl = settings.confirmation_ttl_minutes
        return ttl > 0 and self.age_seconds > ttl * 60

    @property
    def minutes_left(self) -> int:
        """Сколько минут осталось на ответ (0, если срок не ограничен или вышел)."""
        ttl = settings.confirmation_ttl_minutes
        if ttl <= 0:
            return 0
        return max(0, int((ttl * 60 - self.age_seconds) // 60))


@dataclass
class Session:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingTurn | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})

    @property
    def awaiting_confirmation(self) -> bool:
        """Ход заморожен и ждёт живого решения пользователя.

        Просроченный ход сюда не относится: он уже отклонён по времени,
        и принимать по нему запоздалое «подтверждаю» нельзя.
        """
        return self.pending is not None and not self.pending.expired

    @property
    def pending_expired(self) -> bool:
        return self.pending is not None and self.pending.expired


class AgentError(Exception):
    pass


class OperonAgent:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None
        # Сообщения с role="system" внутри messages поддерживают не все модели.
        self._supports_system_messages = True
        # Параметры, которые шлюз отверг: повторяем запрос уже без них.
        self._unsupported_params: set[str] = set()
        # Потолок ответа, если шлюз сказал, что запрошенный слишком велик.
        # У каждой модели он свой, а каталог стороннего шлюза заранее неизвестен.
        self._max_tokens_cap: int | None = None
        self._caps_model: str = ""

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
            if not current_model():
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
        model = current_model()
        if model != self._caps_model:
            # Что шлюз не принял для прежней модели, к новой не относится.
            self._caps_model = model
            self._max_tokens_cap = None
            self._unsupported_params.clear()
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens_cap or settings.max_tokens,
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
        # Просроченный ход снимаем молча: пользователь уже начал говорить о другом,
        # а модель обязана узнать, что действие не выполнено.
        expired_results = self._discard_expired_pending(session)

        if session.awaiting_confirmation:
            left = session.pending.minutes_left if session.pending else 0
            yield {
                "type": "error",
                "message": (
                    "Ход приостановлен: сначала подтвердите или отклоните запрошенное действие."
                    + (f" На ответ осталось около {left} мин." if left else "")
                ),
            }
            return

        if expired_results is not None:
            yield {
                "type": "warning",
                "message": (
                    f"Предыдущее действие отменено: подтверждение не получено за "
                    f"{settings.confirmation_ttl_minutes} мин."
                ),
            }
            # Результаты инструментов и новая реплика идут одним сообщением:
            # API требует, чтобы tool_result шли сразу за вызовом инструмента.
            session.messages.append(
                {"role": "user", "content": [*expired_results, {"type": "text", "text": text}]}
            )
        else:
            session.messages.append({"role": "user", "content": text})

        session.messages.append({"role": "system", "content": self._runtime_context()})
        self._trim_history(session)
        yield from self._run_loop(session)

    def resume_with_decisions(
        self,
        session: Session,
        decisions: dict[str, str],
        comments: dict[str, str] | None = None,
        *,
        timed_out: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Продолжает ход с решениями пользователя.

        Решение по действию должно быть явным: всё, кроме «approve», — отказ,
        отсутствие решения — тоже отказ. При ``timed_out`` отказом считается
        именно молчание, и модель получает об этом отдельную формулировку.
        """
        pending = session.pending
        if pending is None:
            yield {"type": "error", "message": "Нет действий, ожидающих подтверждения."}
            return

        comments = comments or {}
        results = list(pending.completed_results)

        for action in pending.actions:
            raw = decisions.get(action.tool_use_id)
            decision = (raw or "reject").lower()
            title = action.preview.get("title", action.name)

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
            elif timed_out and raw is None:
                content = EXPIRED_TEMPLATE.format(minutes=settings.confirmation_ttl_minutes)
                is_error = False
                yield {
                    "type": "tool_declined",
                    "name": action.name,
                    "summary": f"Отменено по времени (нет ответа): {title}",
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
                    "summary": f"Действие отклонено пользователем: {title}",
                }
            results.append(self._tool_result(action.tool_use_id, content, is_error))

        session.pending = None
        session.messages.append({"role": "user", "content": results})
        yield from self._run_loop(session)

    def expire_pending(
        self,
        session: Session,
        decisions: dict[str, str] | None = None,
        comments: dict[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Закрывает просроченный ход: неотвеченные карточки становятся отказом.

        Уже нажатые кнопки уважаем — пользователь по ним высказался. Молчание
        по остальным трактуем как отказ и сообщаем об этом вслух: иначе
        «нет ответа = отказ» остаётся правилом на бумаге.
        """
        if session.pending is None or not session.pending.expired:
            return
        yield {
            "type": "warning",
            "message": (
                f"Подтверждение не получено за {settings.confirmation_ttl_minutes} мин — "
                "неподтверждённые действия отменены."
            ),
        }
        yield from self.resume_with_decisions(
            session, decisions or {}, comments or {}, timed_out=True
        )

    def _discard_expired_pending(self, session: Session) -> list[dict[str, Any]] | None:
        """Снимает просроченный ход, возвращая tool_result-блоки с отказом.

        Отдельный запрос к модели ради этого не делаем: блоки уедут вместе со
        следующей репликой пользователя.
        """
        pending = session.pending
        if pending is None or not pending.expired:
            return None

        results = list(pending.completed_results)
        content = EXPIRED_TEMPLATE.format(minutes=settings.confirmation_ttl_minutes)
        for action in pending.actions:
            results.append(self._tool_result(action.tool_use_id, content, False))
        session.pending = None
        logger.info(
            "Сессия %s: %s действий отменено по истечении срока подтверждения",
            session.session_id,
            len(pending.actions),
        )
        return results

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
                    # Срок показываем сразу: пользователь должен знать, что
                    # молчание — это отказ, а не бесконечное ожидание.
                    "expires_in_minutes": settings.confirmation_ttl_minutes,
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

        # Лимит ответа больше, чем позволяет модель. Встречается при смене
        # модели на шлюзе: у каждой свой потолок, и узнать его заранее нельзя.
        if ("max_tokens" in message or "max output" in message or "max_completion" in message) and (
            "less than" in message
            or "too large" in message
            or "maximum" in message
            or "exceed" in message
            or "not supported" in message
            or "больше" in message
        ):
            current = self._max_tokens_cap or settings.max_tokens
            reduced = max(current // 2, 1024)
            if reduced < current:
                logger.warning(
                    "Шлюз не принял max_tokens=%s — повторяю с %s", current, reduced
                )
                self._max_tokens_cap = reduced
                return True

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
            return f"Модель «{current_model()}» недоступна для этого ключа. Выберите другую в Mini App или проверьте OPERON_MODEL."
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
