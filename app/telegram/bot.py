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
from ..integrations import google_client, google_oauth
from ..kb import knowledge_base
from ..sessions import store
from .. import reminders
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
    "/auth — подключить Google (или переподключить)\n"
    "/help — подсказка"
)

THINKING = "⏳ Думаю…"
EXPIRED_MARK = "⌛ Время вышло — действие отменено"


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
    # Момент выдачи ссылки /auth: следующее сообщение считаем кодом.
    oauth_started_at: float | None = None
    # Одноразовый state выданной ссылки — по нему забираем результат,
    # если код пришёл на сервер сам (клиент типа Web).
    oauth_state: str = ""

    @property
    def oauth_expired(self) -> bool:
        if self.oauth_started_at is None:
            return False
        limit = settings.oauth_wait_minutes
        return limit > 0 and time.monotonic() - self.oauth_started_at > limit * 60


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
        """Читает обновления, пока не позовут stop().

        Ошибку запуска наружу не гасим: перезапуском занимается надзор
        (app/telegram/supervisor.py), а молча вернувшийся поток выглядел бы
        как работающий бот.
        """
        me = self._api.get_me()
        logger.info(
            "Telegram-бот @%s запущен, разрешено пользователей: %s",
            me.get("username", "?"),
            len(self._allowed),
        )

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

            # Сроки идут своим ходом, даже когда пользователь молчит: длинный
            # опрос возвращается не реже раза в telegram_poll_timeout секунд.
            try:
                self.sweep_expired()
            except Exception:  # noqa: BLE001
                logger.exception("Сбой при снятии просроченных подтверждений")

            try:
                self.send_reminders()
            except Exception:  # noqa: BLE001 — напоминание не должно ронять бота
                logger.exception("Сбой при отправке напоминаний")

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

        # Ожидаем код авторизации Google после /auth.
        if state.oauth_started_at is not None:
            self._handle_oauth_code(chat_id, state, text)
            return

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
        elif command == "auth":
            self._handle_auth(chat_id)
        else:
            self._api.send_message(
                chat_id, "Неизвестная команда. Есть /new, /status, /auth, /help."
            )

    # --- подключение Google ---

    def _handle_auth(self, chat_id: int) -> None:
        """Выдаёт ссылку авторизации и переводит чат в ожидание кода.

        Без этой команды переавторизация означала бы ручной перенос файла
        токена на сервер — то есть в реальности не делалась бы никогда.
        """
        state = self._states.setdefault(chat_id, ChatState())
        if state.cards or state.awaiting_comment_for:
            self._api.send_message(
                chat_id, "Сначала закройте запрос подтверждения выше, потом /auth."
            )
            return

        try:
            url, oauth_state = google_oauth.start(label=f"telegram:{chat_id}")
        except google_oauth.OAuthError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        state.oauth_started_at = time.monotonic()
        state.oauth_state = oauth_state
        current = google_client.status()
        prefix = (
            f"Google уже подключён ({escape(current.get('account_hint', '') or 'учётная запись определена')}). "
            "Новая авторизация заменит текущий доступ.\n\n"
            if current.get("connected")
            else ""
        )
        self._api.send_message(
            chat_id,
            f"{prefix}<b>Подключение Google</b>\n\n"
            f'<a href="{escape(url)}">Открыть страницу доступа</a>\n\n'
            + escape(google_oauth.instructions()),
        )

    def _handle_oauth_code(self, chat_id: int, state: ChatState, text: str) -> None:
        if not google_oauth.looks_like_code(text):
            self._clear_oauth(state)
            self._api.send_message(
                chat_id,
                "Это не похоже на код авторизации — режим ожидания снят, "
                "сообщение не обработано. Повторите /auth, если хотели подключить Google.",
            )
            return

        self._clear_oauth(state)
        try:
            result = google_oauth.exchange_code(text)
        except google_oauth.OAuthError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        self._report_connected(chat_id, result)

    @staticmethod
    def _clear_oauth(state: ChatState) -> None:
        if state.oauth_state:
            google_oauth.forget(state.oauth_state)
        state.oauth_started_at = None
        state.oauth_state = ""

    def _report_connected(self, chat_id: int, result: dict[str, Any]) -> None:
        lines = ["✅ <b>Google подключён</b>"]
        if result.get("account"):
            lines.append(f"Учётная запись: {escape(result['account'])}")
        lines.append(
            "Токен сохранён "
            + (
                "в зашифрованном виде."
                if result["encrypted"]
                else "БЕЗ шифрования — задайте OPERON_TOKEN_KEY."
            )
        )
        lines.append("")
        lines.append("Выданные разрешения:")
        lines.extend(f"• {escape(s)}" for s in result["scopes"])
        if result["missing_scopes"]:
            lines.append("")
            lines.append("⚠️ НЕ выданы (часть функций не заработает):")
            lines.extend(f"• {escape(s)}" for s in result["missing_scopes"])
        self._api.send_message(chat_id, "\n".join(lines))

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
            client = google_oauth.describe_client()
            lines.append(f"OAuth-клиент: {escape(client['source'])}")
            if client["configured"]:
                lines.append("Подключить: /auth")

        from ..tools import registry

        has_search = "internet_search" in registry.names()
        lines.append("Интернет: " + ("доступен" if has_search or settings.web_search_enabled else "нет ключа поиска"))

        rem = reminders.describe()
        if rem["enabled"]:
            lines.append(
                f"Напоминания: сводка в {rem['digest_hour']}:00, "
                f"предупреждение за {rem['remind_before_days']} дн., "
                f"тишина {rem['quiet_hours']}"
            )
        else:
            lines.append("Напоминания: выключены")
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
        ttl = settings.confirmation_ttl_minutes
        lines.append(
            "<i>Действие выполнится только после подтверждения."
            + (f" Без ответа за {ttl} мин — отмена.</i>" if ttl > 0 else "</i>")
        )
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

    def sweep_expired(self) -> None:
        """Гасит просроченные карточки и ожидания кода.

        «Нет ответа = отказ» должно наступать само: пользователь может закрыть
        Telegram и не вернуться, а замороженный ход не отпустит диалог, пока
        кто-то его не закроет.
        """
        for chat_id in list(self._states):
            state = self._states.get(chat_id)
            if state is None:
                continue

            # Клиент типа Web возвращает код прямо на сервер: результат
            # появляется сам, пользователю писать в чат нечего.
            if state.oauth_state:
                entry = google_oauth.take_result(state.oauth_state)
                if entry is not None:
                    self._clear_oauth(state)
                    if entry.error:
                        self._api.send_message(chat_id, "❌ " + escape(entry.error))
                    elif entry.result is not None:
                        self._report_connected(chat_id, entry.result)
                    continue

            if state.oauth_expired:
                self._clear_oauth(state)
                self._api.send_message(
                    chat_id,
                    f"Ответ Google так и не пришёл за {settings.oauth_wait_minutes} мин — "
                    "ожидание снято. Начните заново: /auth",
                )

            session = store.get(f"tg-{chat_id}")
            if session is None or session.pending is None or not session.pending.expired:
                continue

            for card in state.cards.values():
                self._api.edit_message_text(
                    chat_id,
                    card.message_id,
                    f"<b>{escape(card.title)}</b>\n{escape(EXPIRED_MARK)}",
                    reply_markup={"inline_keyboard": []},
                )

            decisions = dict(state.decisions)
            comments = dict(state.comments)
            state.cards.clear()
            state.decisions.clear()
            state.comments.clear()
            state.awaiting_comment_for = None
            logger.info("Чат %s: подтверждение просрочено, действия отменены", chat_id)
            self._run_turn(chat_id, self._agent.expire_pending(session, decisions, comments))

    def send_reminders(self) -> None:
        """Отправляет то, что бот должен сказать сам, без вопроса пользователя.

        Получателями считаем весь белый список: он и задуман как «те, кому
        этот ассистент принадлежит». Отправленное помечается только после
        успешной отправки — иначе сбой связи проглотил бы напоминание молча.
        """
        plan = reminders.pending()
        if not plan:
            return

        delivered: list[reminders.Reminder] = []
        for reminder in plan:
            if not reminder.text:
                # Пустая сводка: отмечаем как обработанную, но не пишем.
                # Ежедневное «всё в порядке» перестают читать.
                delivered.append(reminder)
                continue
            sent_to_someone = False
            for user_id in sorted(self._allowed):
                try:
                    for chunk in split_message(reminder.text):
                        self._api.send_message(user_id, chunk)
                    sent_to_someone = True
                except TelegramError as exc:
                    # Обычная причина — пользователь не открывал диалог с ботом.
                    logger.warning("Напоминание не доставлено %s: %s", user_id, exc)
            if sent_to_someone:
                delivered.append(reminder)

        if delivered:
            reminders.mark_sent(delivered)
            logger.info("Напоминаний отправлено: %s", sum(1 for r in delivered if r.text))

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
        if session is None or session.pending is None:
            return
        if session.pending.expired:
            # Кнопку нажали, но срок уже вышел: закрываем ход отказом, а не
            # тишиной — иначе диалог останется замороженным навсегда.
            self._run_turn(chat_id, self._agent.expire_pending(session, decisions, comments))
            return
        self._run_turn(chat_id, self._agent.resume_with_decisions(session, decisions, comments))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    from .supervisor import TelegramSupervisor

    supervisor = TelegramSupervisor()
    if not settings.telegram_token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN")
        return 1
    if not settings.telegram_allowed_users:
        logger.error("Не задан TELEGRAM_ALLOWED_USERS — без белого списка бот не запускается")
        return 1
    try:
        # Отдельный процесс — тот же надзор: сбой не должен оставлять без бота.
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
