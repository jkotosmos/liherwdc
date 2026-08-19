"""Бот Telegram: интерфейс агента с кнопками подтверждения.

Единственная граница доступа — белый список Telegram ID. Бот работает с
Google-аккаунтом и бюджетом владельца, поэтому сообщения от посторонних
игнорируются молча: отвечать «вам сюда нельзя» — значит подтверждать, что
бот существует.

Запуск отдельным процессом:  python -m app.telegram.bot
Вместе с веб-интерфейсом бот стартует фоновым потоком (см. app/server.py).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..agent import agent as default_agent
from ..config import settings
from ..integrations import google_client
from ..kb import knowledge_base
from ..sessions import store
from .api import TelegramAPI, TelegramError
from .format import escape, split_message, to_telegram_html

logger = logging.getLogger(__name__)

GREETING = (
    "Деловой ассистент {org}.\n\n"
    "Отвечаю по базе знаний, Google Диску и календарю — со ссылкой на источник. "
    "Ничего не создаю и не меняю без вашего подтверждения.\n\n"
    "Команды:\n"
    "/new — начать диалог заново\n"
    "/status — что подключено\n"
    "/help — подсказка"
)

THINKING = "⏳ Думаю…"


@dataclass
class PendingCard:
    """Карточка подтверждения, ждущая нажатия кнопки."""

    chat_id: int
    message_id: int
    tool_use_id: str
    title: str


@dataclass
class ChatState:
    """Незакрытые подтверждения одного чата."""

    cards: dict[str, PendingCard] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)
    awaiting_comment_for: str | None = None


class TelegramBot:
    def __init__(self, api: TelegramAPI | None = None, agent: Any = None) -> None:
        self._api = api or TelegramAPI(settings.telegram_token)
        self._agent = agent or default_agent
        self._allowed = settings.telegram_allowed_users
        self._states: dict[int, ChatState] = {}
        self._offset: int | None = None
        self._stop = threading.Event()

        if not self._allowed:
            raise TelegramError(
                "Не задан TELEGRAM_ALLOWED_USERS. Без белого списка бот не запускается: "
                "иначе доступ к вашему Google-аккаунту получит любой, кто найдёт бота."
            )

    # --- жизненный цикл ---

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            me = self._api.get_me()
            logger.info(
                "Telegram-бот @%s запущен, разрешено пользователей: %s",
                me.get("username", "?"),
                len(self._allowed),
            )
        except TelegramError as exc:
            logger.error("Не удалось запустить бота: %s", exc)
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = self._api.get_updates(self._offset, settings.telegram_poll_timeout)
                backoff = 1.0
            except TelegramError as exc:
                logger.error("Ошибка получения обновлений: %s", exc)
                # 409 означает второй экземпляр — ждём дольше, чтобы не спорить с ним.
                self._stop.wait(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue

            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:  # noqa: BLE001 — один сбой не должен ронять бота
                    logger.exception("Сбой обработки обновления %s", update.get("update_id"))

        logger.info("Telegram-бот остановлен")

    # --- маршрутизация ---

    def _allowed_user(self, update: dict[str, Any]) -> int | None:
        source = update.get("message") or update.get("callback_query") or {}
        user = source.get("from") or {}
        user_id = user.get("id")
        if user_id in self._allowed:
            return user_id
        logger.info(
            "Проигнорировано сообщение от постороннего пользователя id=%s (%s)",
            user_id,
            user.get("username", ""),
        )
        return None

    def _handle_update(self, update: dict[str, Any]) -> None:
        if self._allowed_user(update) is None:
            return
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    # --- сообщения ---

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        if not text:
            self._api.send_message(
                chat_id,
                "Пока понимаю только текст. Голосовые сообщения и файлы — "
                "в следующей версии.",
            )
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return

        state = self._states.setdefault(chat_id, ChatState())

        # Ожидаем текст правок к отклонённому действию.
        if state.awaiting_comment_for:
            tool_use_id = state.awaiting_comment_for
            state.awaiting_comment_for = None
            state.comments[tool_use_id] = text
            self._maybe_resume(chat_id)
            return

        if state.cards:
            self._api.send_message(
                chat_id,
                "Сначала ответьте на запрос подтверждения кнопками выше.",
            )
            return

        session = store.get_or_create(f"tg-{chat_id}")
        self._run_turn(chat_id, self._agent.send_user_message(session, text))

    def _handle_command(self, chat_id: int, text: str) -> None:
        command = text.split()[0].lower().lstrip("/").split("@")[0]

        if command in {"start", "help"}:
            self._api.send_message(chat_id, escape(GREETING.format(org=settings.org_name)))
        elif command == "new":
            store.reset(f"tg-{chat_id}")
            self._states.pop(chat_id, None)
            self._api.send_message(chat_id, "Диалог очищен. Слушаю.")
        elif command == "status":
            self._api.send_message(chat_id, self._status_text(), parse_mode="HTML")
        else:
            self._api.send_message(chat_id, "Неизвестная команда. Есть /new, /status, /help.")

    def _status_text(self) -> str:
        kb = knowledge_base.stats
        google = google_client.status()
        lines = [
            f"<b>Ассистент {escape(settings.org_name)}</b>",
            f"Модель: {escape(settings.model)} ({escape(settings.provider)})",
            f"База знаний: {kb['documents']} документов"
            + (f" ({escape(', '.join(kb['categories']))})" if kb["categories"] else ""),
        ]
        if google.get("connected"):
            lines.append(f"Google: подключён {escape(google.get('account_hint', ''))}")
        else:
            lines.append(f"Google: не подключён — {escape(str(google.get('reason', '')))}")

        from ..tools import registry

        has_search = "internet_search" in registry.names()
        lines.append("Интернет: " + ("доступен" if has_search or settings.web_search_enabled else "нет ключа поиска"))
        return "\n".join(lines)

    # --- ход агента ---

    def _run_turn(self, chat_id: int, events: Any) -> None:
        self._api.send_chat_action(chat_id)
        placeholder = self._api.send_message(chat_id, THINKING, parse_mode=None)
        message_id = placeholder["message_id"]

        answer: list[str] = []
        activity: list[str] = []
        notices: list[str] = []
        pending_actions: list[dict[str, Any]] = []
        last_edit = 0.0

        for event in events:
            kind = event["type"]

            if kind == "text_delta":
                answer.append(event["text"])
            elif kind == "tool_start":
                activity.append(event.get("activity", event["name"]))
                # Показываем прогресс, но не чаще раза в две секунды: у Telegram
                # жёсткие лимиты на редактирование сообщений.
                if time.monotonic() - last_edit > 2:
                    last_edit = time.monotonic()
                    self._api.edit_message_text(
                        chat_id, message_id, f"⏳ {escape(activity[-1])}…", parse_mode="HTML"
                    )
            elif kind == "tool_declined":
                activity.append(event["summary"])
            elif kind in {"error", "warning"}:
                notices.append(("⚠️ " if kind == "warning" else "❌ ") + event["message"])
            elif kind == "confirmation_required":
                pending_actions = event["actions"]

        text = "".join(answer).strip()
        body = to_telegram_html(text) if text else ""
        if notices:
            body = (body + "\n\n" if body else "") + "\n".join(escape(n) for n in notices)
        if not body:
            body = escape("Готово.") if not pending_actions else escape("Требуется подтверждение:")

        chunks = split_message(body)
        self._api.edit_message_text(chat_id, message_id, chunks[0])
        for chunk in chunks[1:]:
            self._api.send_message(chat_id, chunk)

        if pending_actions:
            self._send_confirmations(chat_id, pending_actions)

    # --- подтверждения ---

    def _send_confirmations(self, chat_id: int, actions: list[dict[str, Any]]) -> None:
        state = self._states.setdefault(chat_id, ChatState())

        for action in actions:
            token = secrets.token_urlsafe(6)
            card = self._render_card(action)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Подтвердить", "callback_data": f"a:{token}"},
                        {"text": "✏️ Правки", "callback_data": f"e:{token}"},
                        {"text": "✕ Отмена", "callback_data": f"r:{token}"},
                    ]
                ]
            }
            sent = self._api.send_message(chat_id, card, reply_markup=keyboard)
            state.cards[token] = PendingCard(
                chat_id=chat_id,
                message_id=sent["message_id"],
                tool_use_id=action["tool_use_id"],
                title=action.get("title", action["name"]),
            )

    @staticmethod
    def _render_card(action: dict[str, Any]) -> str:
        lines = [
            f"<b>{escape(action.get('title', action['name']))}</b>",
            escape(action.get("summary", "")),
        ]
        details = action.get("details") or {}
        if details:
            lines.append("")
            for key, value in details.items():
                shown = value if isinstance(value, str) else str(value)
                if len(shown) > 600:
                    shown = shown[:600] + "…"
                lines.append(f"<b>{escape(str(key))}:</b> {escape(shown)}")
        lines.append("")
        lines.append("<i>Действие выполнится только после подтверждения.</i>")
        return "\n".join(line for line in lines if line is not None)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        state = self._states.get(chat_id)

        if not state or ":" not in data:
            self._api.answer_callback_query(callback["id"], "Запрос устарел.")
            return

        prefix, token = data.split(":", 1)
        card = state.cards.get(token)
        if card is None:
            self._api.answer_callback_query(callback["id"], "Это подтверждение уже обработано.")
            return

        verdicts = {
            "a": ("approve", "✅ Подтверждено", "Выполняю"),
            "r": ("reject", "✕ Отменено", "Действие не выполнено"),
            "e": ("reject", "✏️ Отправлено на правки", "Напишите, что изменить"),
        }
        if prefix not in verdicts:
            self._api.answer_callback_query(callback["id"], "Неизвестная кнопка.")
            return

        decision, mark, toast = verdicts[prefix]
        state.decisions[card.tool_use_id] = decision
        del state.cards[token]

        self._api.answer_callback_query(callback["id"], toast)
        self._api.edit_message_text(
            chat_id,
            card.message_id,
            f"<b>{escape(card.title)}</b>\n{escape(mark)}",
            reply_markup={"inline_keyboard": []},
        )

        if prefix == "e":
            state.awaiting_comment_for = card.tool_use_id
            self._api.send_message(chat_id, "Напишите, что нужно изменить.")
            return

        self._maybe_resume(chat_id)

    def _maybe_resume(self, chat_id: int) -> None:
        """Продолжает ход, когда решения приняты по всем карточкам."""
        state = self._states.get(chat_id)
        if state is None or state.cards or state.awaiting_comment_for:
            return

        decisions = dict(state.decisions)
        comments = dict(state.comments)
        state.decisions.clear()
        state.comments.clear()

        session = store.get(f"tg-{chat_id}")
        if session is None or not session.awaiting_confirmation:
            return
        self._run_turn(chat_id, self._agent.resume_with_decisions(session, decisions, comments))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if not settings.telegram_token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN")
        return 1
    try:
        TelegramBot().run()
    except TelegramError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
