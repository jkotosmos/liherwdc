"""Бот Telegram: белый список, кнопки подтверждения, форматирование."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.config import settings
from app.sessions import store
from app.telegram import bot as bot_module
from app.telegram.api import TelegramError
from app.telegram.bot import TelegramBot
from app.telegram.format import split_message, to_telegram_html

ALLOWED = 100500
STRANGER = 999


class FakeAPI:
    """Записывает вызовы вместо обращения к Telegram."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1

    def _record(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        self._next_id += 1
        return {"message_id": self._next_id}

    def get_me(self): return {"username": "test_bot"}
    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML"):
        return self._record("send_message", chat_id=chat_id, text=text, reply_markup=reply_markup)
    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        return self._record("edit", chat_id=chat_id, message_id=message_id, text=text)
    def answer_callback_query(self, callback_id, text=""):
        return self._record("answer_callback", text=text)
    def send_chat_action(self, chat_id, action="typing"):
        return self._record("chat_action", chat_id=chat_id)
    def delete_message(self, chat_id, message_id):
        return self._record("delete", chat_id=chat_id)

    def texts(self, method: str = "send_message") -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == method]

    def keyboards(self) -> list[dict]:
        return [p["reply_markup"] for m, p in self.calls
                if m == "send_message" and p.get("reply_markup")]


class FakeAgent:
    """Подменяет агента: сценарий задаётся списком событий."""

    def __init__(self, first: list[dict], after_resume: list[dict] | None = None) -> None:
        self.first = first
        self.after_resume = after_resume or [{"type": "text_delta", "text": "Готово."},
                                             {"type": "done", "stop": "end_turn"}]
        self.resumed: tuple[dict, dict] | None = None

    def send_user_message(self, session, text: str):
        for event in self.first:
            if event["type"] == "confirmation_required":
                # Настоящий агент замораживает ход — повторяем это поведение.
                from app.agent import PendingTurn
                session.pending = PendingTurn()
            yield event

    def resume_with_decisions(self, session, decisions, comments=None):
        self.resumed = (dict(decisions), dict(comments or {}))
        session.pending = None
        yield from self.after_resume


def make_bot(agent: FakeAgent, monkeypatch) -> tuple[TelegramBot, FakeAPI]:
    monkeypatch.setattr(
        bot_module, "settings", replace(settings, telegram_token="t"), raising=False
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(ALLOWED))
    api = FakeAPI()
    bot = TelegramBot(api=api, agent=agent)
    return bot, api


def message(text: str, user_id: int = ALLOWED, chat_id: int = 1) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}}


def callback(data: str, message_id: int = 2, user_id: int = ALLOWED, chat_id: int = 1) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


@pytest.fixture(autouse=True)
def clean_sessions():
    yield
    store.reset("tg-1")


class TestWhitelist:
    """Единственная граница доступа во всей системе."""

    def test_stranger_is_ignored_silently(self, monkeypatch) -> None:
        agent = FakeAgent([{"type": "text_delta", "text": "секрет"}])
        bot, api = make_bot(agent, monkeypatch)

        bot._handle_update(message("покажи договоры", user_id=STRANGER))

        assert api.calls == [], "постороннему не должно уходить вообще ничего"

    def test_stranger_callback_ignored(self, monkeypatch) -> None:
        bot, api = make_bot(FakeAgent([]), monkeypatch)
        bot._handle_update(callback("a:token", user_id=STRANGER))
        assert api.calls == []

    def test_allowed_user_is_served(self, monkeypatch) -> None:
        agent = FakeAgent([{"type": "text_delta", "text": "ответ"},
                           {"type": "done", "stop": "end_turn"}])
        bot, api = make_bot(agent, monkeypatch)

        bot._handle_update(message("вопрос"))
        assert any("ответ" in t for t in api.texts("edit"))

    def test_bot_refuses_to_start_without_whitelist(self, monkeypatch) -> None:
        monkeypatch.setattr(bot_module, "settings", replace(settings, telegram_token="t"))
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        with pytest.raises(TelegramError, match="TELEGRAM_ALLOWED_USERS"):
            TelegramBot(api=FakeAPI(), agent=FakeAgent([]))


class TestConfirmationButtons:
    ACTION = {
        "tool_use_id": "t1",
        "name": "task_create",
        "title": "Зафиксировать поручение",
        "summary": "Подготовить КП для Иванова, срок 2026-09-01",
        "details": {"Ответственный": "Иванов", "Срок": "2026-09-01"},
    }

    def _bot_with_pending(self, monkeypatch):
        agent = FakeAgent([
            {"type": "text_delta", "text": "Зафиксирую поручение."},
            {"type": "confirmation_required", "actions": [self.ACTION]},
            {"type": "done", "stop": "awaiting_confirmation"},
        ])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("поставь задачу Иванову"))
        return bot, api, agent

    def test_card_shows_three_buttons(self, monkeypatch) -> None:
        _, api, _ = self._bot_with_pending(monkeypatch)
        keyboard = api.keyboards()[0]["inline_keyboard"][0]
        assert [b["text"] for b in keyboard] == ["✅ Подтвердить", "✏️ Правки", "✕ Отмена"]

    def test_card_describes_the_action(self, monkeypatch) -> None:
        _, api, _ = self._bot_with_pending(monkeypatch)
        card = [t for t in api.texts() if "Зафиксировать поручение" in t][0]
        assert "Иванов" in card
        assert "2026-09-01" in card
        assert "только после подтверждения" in card

    def test_callback_data_fits_telegram_limit(self, monkeypatch) -> None:
        """Telegram отбрасывает callback_data длиннее 64 байт."""
        _, api, _ = self._bot_with_pending(monkeypatch)
        for button in api.keyboards()[0]["inline_keyboard"][0]:
            assert len(button["callback_data"].encode()) <= 64

    def test_approve_resumes_with_approval(self, monkeypatch) -> None:
        bot, api, agent = self._bot_with_pending(monkeypatch)
        token = api.keyboards()[0]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]

        bot._handle_update(callback(f"a:{token}"))

        assert agent.resumed is not None
        assert agent.resumed[0] == {"t1": "approve"}

    def test_cancel_resumes_with_rejection(self, monkeypatch) -> None:
        bot, api, agent = self._bot_with_pending(monkeypatch)
        token = api.keyboards()[0]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]

        bot._handle_update(callback(f"r:{token}"))

        assert agent.resumed[0] == {"t1": "reject"}

    def test_edits_button_collects_comment_before_resuming(self, monkeypatch) -> None:
        """«Правки» — это отказ с пояснением, что именно поменять."""
        bot, api, agent = self._bot_with_pending(monkeypatch)
        token = api.keyboards()[0]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]

        bot._handle_update(callback(f"e:{token}"))
        assert agent.resumed is None, "до получения правок ход возобновлять нельзя"
        assert any("что нужно изменить" in t for t in api.texts())

        bot._handle_update(message("срок не 1 сентября, а 15-е"))
        assert agent.resumed[0] == {"t1": "reject"}
        assert agent.resumed[1] == {"t1": "срок не 1 сентября, а 15-е"}

    def test_used_button_is_disabled(self, monkeypatch) -> None:
        bot, api, _ = self._bot_with_pending(monkeypatch)
        token = api.keyboards()[0]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
        bot._handle_update(callback(f"a:{token}"))

        bot._handle_update(callback(f"a:{token}"))
        assert any("уже обработано" in p.get("text", "")
                   for m, p in api.calls if m == "answer_callback")

    def test_two_actions_resume_only_after_both_decided(self, monkeypatch) -> None:
        second = {**self.ACTION, "tool_use_id": "t2", "title": "Создать событие"}
        agent = FakeAgent([
            {"type": "confirmation_required", "actions": [self.ACTION, second]},
            {"type": "done", "stop": "awaiting_confirmation"},
        ])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("сделай оба действия"))

        tokens = [k["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
                  for k in api.keyboards()]
        assert len(tokens) == 2

        bot._handle_update(callback(f"a:{tokens[0]}"))
        assert agent.resumed is None, "нельзя продолжать, пока не решены все карточки"

        bot._handle_update(callback(f"r:{tokens[1]}"))
        assert agent.resumed[0] == {"t1": "approve", "t2": "reject"}

    def test_text_is_blocked_while_confirmation_pending(self, monkeypatch) -> None:
        bot, api, agent = self._bot_with_pending(monkeypatch)
        bot._handle_update(message("а ещё вопрос"))
        assert any("кнопками" in t for t in api.texts())


class TestCommands:
    def _bot(self, monkeypatch):
        return make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)

    def test_start_explains_confirmation_rule(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/start"))
        assert "без вашего подтверждения" in api.texts()[0]

    def test_new_clears_dialogue(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        session = store.get_or_create("tg-1")
        session.messages.append({"role": "user", "content": "старое"})

        bot._handle_update(message("/new"))
        assert store.get("tg-1").messages == []

    def test_status_reports_connections(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/status"))
        text = api.texts()[0]
        assert "База знаний" in text and "Google" in text

    def test_unknown_command(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/чтото"))
        assert "Неизвестная команда" in api.texts()[0]

    def test_voice_message_says_what_is_accepted(self, monkeypatch) -> None:
        """Отказ должен называть, что бот принимает, а не только чего не умеет."""
        bot, api = self._bot(monkeypatch)
        bot._handle_update({"update_id": 1,
                            "message": {"chat": {"id": 1}, "from": {"id": ALLOWED},
                                        "voice": {"file_id": "x"}}})
        text = api.texts()[0]
        assert "Голосовые" in text
        assert ".docx" in text and ".pdf" in text


class TestFormatting:
    def test_markdown_becomes_telegram_html(self) -> None:
        html = to_telegram_html("## Итог\n\n- **важно**: 12,4 млн\n\n[ссылка](https://x.test)")
        assert "<b>Итог</b>" in html
        assert "• <b>важно</b>: 12,4 млн" in html
        assert '<a href="https://x.test">ссылка</a>' in html

    def test_html_from_model_is_escaped(self) -> None:
        assert "&lt;script&gt;" in to_telegram_html("<script>alert(1)</script>")

    def test_long_answer_is_split_under_limit(self) -> None:
        chunks = split_message("строка ответа\n\n" * 800)
        assert len(chunks) > 1
        assert all(len(c) <= 4096 for c in chunks)

    def test_long_answer_sent_in_parts(self, monkeypatch) -> None:
        agent = FakeAgent([{"type": "text_delta", "text": "абзац текста\n\n" * 700},
                           {"type": "done", "stop": "end_turn"}])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("длинный вопрос"))
        # Первая часть уходит правкой заглушки, остальные — отдельными сообщениями.
        assert len(api.texts("edit")) >= 1
        assert len(api.texts()) >= 1


class TestErrors:
    def test_agent_error_is_shown_to_user(self, monkeypatch) -> None:
        agent = FakeAgent([{"type": "error", "message": "Шлюз ответил 401"},
                           {"type": "done", "stop": "error"}])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("вопрос"))
        assert any("401" in t for t in api.texts("edit"))


class TestAuthCommand:
    """Переавторизация Google без переноса файлов на сервер."""

    def _bot(self, monkeypatch):
        return make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)

    def test_auth_sends_link_and_waits_for_code(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(
            bot_module.google_oauth,
            "start",
            lambda label="": ("https://accounts.google.com/o/oauth2/auth?x=1", "st1"),
        )
        bot._handle_update(message("/auth"))

        text = api.texts()[0]
        assert "accounts.google.com" in text
        assert "адрес из строки браузера" in text
        assert bot._states[1].oauth_started_at is not None

    def test_pasted_url_is_exchanged_for_a_token(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(bot_module.google_oauth, "start", lambda label="": ("https://auth", "st1"))
        exchanged: list[str] = []

        def fake_exchange(text: str) -> dict:
            exchanged.append(text)
            return {
                "path": "/data/credentials/google_token.json.enc",
                "encrypted": True,
                "scopes": ["https://www.googleapis.com/auth/drive.file"],
                "missing_scopes": [],
                "account": "boss@operon.ru",
            }

        monkeypatch.setattr(bot_module.google_oauth, "exchange_code", fake_exchange)

        bot._handle_update(message("/auth"))
        bot._handle_update(message("http://localhost:8765/?code=4%2F0Axyz_abcdefghijkl&scope=x"))

        assert exchanged, "код должен уйти на обмен"
        result = api.texts()[-1]
        assert "Google подключён" in result
        assert "boss@operon.ru" in result
        assert bot._states[1].oauth_started_at is None, "режим ожидания обязан сняться"

    def test_missing_scopes_are_reported(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(bot_module.google_oauth, "start", lambda label="": ("https://auth", "st1"))
        monkeypatch.setattr(
            bot_module.google_oauth,
            "exchange_code",
            lambda text: {
                "path": "p", "encrypted": False, "account": "",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "missing_scopes": ["https://www.googleapis.com/auth/calendar.events"],
            },
        )
        bot._handle_update(message("/auth"))
        bot._handle_update(message("4/0Axyz_abcdefghijkl"))

        text = api.texts()[-1]
        assert "НЕ выданы" in text
        assert "calendar.events" in text
        assert "БЕЗ шифрования" in text

    def test_exchange_error_is_shown_verbatim(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(bot_module.google_oauth, "start", lambda label="": ("https://auth", "st1"))

        def boom(text: str):
            raise bot_module.google_oauth.OAuthError("Google отклонил код (invalid_grant).")

        monkeypatch.setattr(bot_module.google_oauth, "exchange_code", boom)
        bot._handle_update(message("/auth"))
        bot._handle_update(message("4/0Axyz_abcdefghijkl"))
        assert "invalid_grant" in api.texts()[-1]

    def test_ordinary_question_does_not_get_swallowed_as_a_code(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(bot_module.google_oauth, "start", lambda label="": ("https://auth", "st1"))
        monkeypatch.setattr(
            bot_module.google_oauth,
            "exchange_code",
            lambda text: pytest.fail("вопрос не должен уходить на обмен"),
        )
        bot._handle_update(message("/auth"))
        bot._handle_update(message("какие встречи на завтра?"))

        assert "не похоже на код" in api.texts()[-1]
        assert bot._states[1].oauth_started_at is None

    def test_auth_refuses_while_confirmation_is_pending(self, monkeypatch) -> None:
        agent = FakeAgent([
            {"type": "confirmation_required", "actions": [TestConfirmationButtons.ACTION]},
            {"type": "done", "stop": "awaiting_confirmation"},
        ])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("поставь задачу"))
        bot._handle_update(message("/auth"))
        assert "Сначала закройте запрос подтверждения" in api.texts()[-1]

    def test_auth_appears_in_help(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/help"))
        assert "/auth" in api.texts()[0]


class TestConfirmationTimeout:
    """«Нет ответа = отказ» — по времени, а не только по логике."""

    def _pending_bot(self, monkeypatch):
        agent = FakeAgent([
            {"type": "confirmation_required", "actions": [TestConfirmationButtons.ACTION]},
            {"type": "done", "stop": "awaiting_confirmation"},
        ])
        bot, api = make_bot(agent, monkeypatch)
        bot._handle_update(message("поставь задачу Иванову"))
        return bot, api, agent

    def test_card_shows_the_deadline(self, monkeypatch) -> None:
        _, api, _ = self._pending_bot(monkeypatch)
        card = [t for t in api.texts() if "Зафиксировать поручение" in t][0]
        assert "Без ответа за" in card and "отмена" in card

    def test_sweep_cancels_the_card_and_closes_the_turn(self, monkeypatch) -> None:
        bot, api, agent = self._pending_bot(monkeypatch)
        expired: list = []
        agent.expire_pending = lambda session, decisions=None, comments=None: (
            expired.append((dict(decisions or {}), dict(comments or {}))),
            iter([{"type": "text_delta", "text": "Действие отменено."},
                  {"type": "done", "stop": "end_turn"}]),
        )[1]

        session = store.get("tg-1")
        from app.agent import PendingTurn
        session.pending = PendingTurn()
        session.pending.created_at -= 10_000  # срок заведомо вышел

        bot.sweep_expired()

        assert expired, "просроченный ход обязан закрыться сам"
        assert any("Время вышло" in t for t in api.texts("edit"))
        assert bot._states[1].cards == {}, "карточки должны быть сняты"

    def test_sweep_leaves_fresh_cards_alone(self, monkeypatch) -> None:
        bot, api, agent = self._pending_bot(monkeypatch)
        agent.expire_pending = lambda *a, **k: pytest.fail("свежую карточку трогать нельзя")

        session = store.get("tg-1")
        from app.agent import PendingTurn
        session.pending = PendingTurn()

        bot.sweep_expired()
        assert bot._states[1].cards, "карточка должна остаться на месте"

    def test_expired_oauth_wait_is_released(self, monkeypatch) -> None:
        bot, api = make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)
        monkeypatch.setattr(bot_module.google_oauth, "start", lambda label="": ("https://auth", "st1"))
        bot._handle_update(message("/auth"))
        bot._states[1].oauth_started_at -= 10_000

        bot.sweep_expired()

        assert bot._states[1].oauth_started_at is None
        assert "так и не пришёл" in api.texts()[-1]

    def test_silence_after_edits_button_does_not_hang_forever(self, monkeypatch) -> None:
        """Нажали «Правки» и пропали: ход всё равно обязан закрыться."""
        bot, api, agent = self._pending_bot(monkeypatch)
        closed: list = []
        agent.expire_pending = lambda session, decisions=None, comments=None: (
            closed.append(dict(decisions or {})),
            iter([{"type": "done", "stop": "end_turn"}]),
        )[1]

        token = api.keyboards()[0]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
        bot._handle_update(callback(f"e:{token}"))
        assert bot._states[1].awaiting_comment_for, "бот ждёт текст правок"

        session = store.get("tg-1")
        from app.agent import PendingTurn
        session.pending = PendingTurn()
        session.pending.created_at -= 10_000

        bot.sweep_expired()

        assert closed == [{"t1": "reject"}], "нажатая кнопка «Правки» — это отказ"
        assert bot._states[1].awaiting_comment_for is None


class TestAuthViaCallback:
    """Клиент типа Web: код приходит на сервер, вставлять в чат нечего."""

    def _bot(self, monkeypatch):
        bot, api = make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)
        monkeypatch.setattr(
            bot_module.google_oauth, "start", lambda label="": ("https://auth", "st-web")
        )
        return bot, api

    def test_bot_picks_up_the_result_by_itself(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/auth"))
        assert bot._states[1].oauth_state == "st-web"

        from app.integrations.google_oauth import _Pending

        ready = _Pending(created_at=0.0, label="telegram:1", done=True, result={
            "encrypted": True, "account": "boss@operon.ru",
            "scopes": ["https://www.googleapis.com/auth/drive.file"], "missing_scopes": [],
        })
        monkeypatch.setattr(
            bot_module.google_oauth, "take_result", lambda state: ready if state == "st-web" else None
        )

        bot.sweep_expired()

        assert "Google подключён" in api.texts()[-1]
        assert "boss@operon.ru" in api.texts()[-1]
        assert bot._states[1].oauth_state == "", "ожидание должно закрыться"

    def test_callback_error_reaches_the_user(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/auth"))

        from app.integrations.google_oauth import _Pending

        failed = _Pending(created_at=0.0, label="", done=True, error="Google отклонил код.")
        monkeypatch.setattr(bot_module.google_oauth, "take_result", lambda state: failed)

        bot.sweep_expired()
        assert "отклонил код" in api.texts()[-1]
        assert bot._states[1].oauth_state == ""

    def test_nothing_is_said_while_the_user_is_still_deciding(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/auth"))
        before = len(api.texts())

        monkeypatch.setattr(bot_module.google_oauth, "take_result", lambda state: None)
        bot.sweep_expired()

        assert len(api.texts()) == before, "пока Google молчит, боту сказать нечего"
        assert bot._states[1].oauth_state == "st-web"


class TestProactiveReminders:
    """Единственное место, где бот говорит первым."""

    def _bot(self, monkeypatch):
        return make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)

    def test_reminder_goes_to_the_whitelist(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        from app.reminders import Reminder

        monkeypatch.setattr(
            bot_module.reminders, "pending", lambda: [Reminder(key="d:1", text="<b>Сводка</b>")]
        )
        marked: list = []
        monkeypatch.setattr(bot_module.reminders, "mark_sent", lambda plan: marked.append(plan))

        bot.send_reminders()

        assert any("Сводка" in t for t in api.texts())
        assert [p["chat_id"] for m, p in api.calls if m == "send_message"] == [ALLOWED]
        assert marked, "отправленное должно помечаться, иначе придёт снова"

    def test_empty_reminder_is_marked_but_not_sent(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        from app.reminders import Reminder

        monkeypatch.setattr(bot_module.reminders, "pending", lambda: [Reminder(key="d:1", text="")])
        marked: list = []
        monkeypatch.setattr(bot_module.reminders, "mark_sent", lambda plan: marked.append(plan))

        bot.send_reminders()

        assert api.texts() == [], "молчать — значит не писать вообще"
        assert marked, "но пометить надо, иначе будем пересобирать каждые полминуты"

    def test_failed_delivery_is_not_marked_as_sent(self, monkeypatch) -> None:
        """Иначе сбой связи проглотил бы напоминание навсегда."""
        bot, api = self._bot(monkeypatch)
        from app.reminders import Reminder

        monkeypatch.setattr(
            bot_module.reminders, "pending", lambda: [Reminder(key="d:1", text="Сводка")]
        )
        marked: list = []
        monkeypatch.setattr(bot_module.reminders, "mark_sent", lambda plan: marked.append(plan))

        def refuse(*args, **kwargs):
            raise TelegramError("bot was blocked by the user")

        monkeypatch.setattr(api, "send_message", refuse)

        bot.send_reminders()
        assert marked == [], "недоставленное помечать нельзя"

    def test_nothing_pending_means_no_calls(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        monkeypatch.setattr(bot_module.reminders, "pending", lambda: [])
        bot.send_reminders()
        assert api.calls == []

    def test_status_shows_the_schedule(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update(message("/status"))
        assert "Напоминания" in api.texts()[0]


class TestDocumentUpload:
    """То, что клиент попробует первым: прислать боту документ."""

    def _bot(self, monkeypatch, tmp_path):
        from dataclasses import replace as _replace

        from app.config import settings as real
        from app.kb import intake
        from app.kb.store import KnowledgeBase

        monkeypatch.setattr(intake, "settings", _replace(real, kb_dir=tmp_path))
        kb = KnowledgeBase(root=tmp_path)
        monkeypatch.setattr("app.kb.knowledge_base", kb, raising=False)
        monkeypatch.setattr(bot_module, "knowledge_base", kb)

        bot, api = make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)
        return bot, api, kb

    @staticmethod
    def _document(name: str, size: int = 5000, caption: str = "") -> dict:
        msg = {
            "chat": {"id": 1},
            "from": {"id": ALLOWED},
            "document": {"file_id": "F1", "file_name": name, "file_size": size},
        }
        if caption:
            msg["caption"] = caption
        return {"update_id": 1, "message": msg}

    @staticmethod
    def _serve(api, payload: bytes):
        api.get_file = lambda file_id: {"file_path": "documents/f.bin"}
        api.download_file = lambda path: payload

    def test_docx_is_parsed_and_offered_for_saving(self, monkeypatch, tmp_path) -> None:
        from test_documents import make_docx

        bot, api, _ = self._bot(monkeypatch, tmp_path)
        self._serve(api, make_docx())

        bot._handle_update(self._document("договор.docx", caption="договоры"))

        card = api.texts()[-1]
        assert "Документ разобран" in card
        assert "договор.docx" in card
        assert "договоры/договор.docx" in card
        assert "Договор поставки" in card, "человек должен видеть, что именно распозналось"
        assert api.keyboards(), "решение принимает человек, а не бот"

    def test_approval_puts_it_into_the_knowledge_base(self, monkeypatch, tmp_path) -> None:
        from test_documents import make_docx

        bot, api, kb = self._bot(monkeypatch, tmp_path)
        self._serve(api, make_docx())
        bot._handle_update(self._document("поставка.docx", caption="договоры"))

        bot._handle_update(callback("f:add"))

        assert (tmp_path / "договоры" / "поставка.docx").exists()
        kb.ensure_fresh()
        assert kb.search("срок оплаты"), "принятый документ обязан находиться поиском"
        assert "В базе знаний" in api.texts("edit")[-1]

    def test_rejection_saves_nothing(self, monkeypatch, tmp_path) -> None:
        from test_documents import make_docx

        bot, api, _ = self._bot(monkeypatch, tmp_path)
        self._serve(api, make_docx())
        bot._handle_update(self._document("ненужный.docx"))

        bot._handle_update(callback("f:no"))

        assert list(tmp_path.rglob("*.docx")) == []
        assert "Не сохранён" in api.texts("edit")[-1]

    def test_no_answer_means_not_saved(self, monkeypatch, tmp_path) -> None:
        """Молчание — отказ и здесь: файл лежит в памяти, но не на диске."""
        from test_documents import make_docx

        bot, api, _ = self._bot(monkeypatch, tmp_path)
        self._serve(api, make_docx())
        bot._handle_update(self._document("висит.docx"))

        assert list(tmp_path.rglob("*.docx")) == []
        assert bot._states[1].pending_file is not None

    def test_unreadable_file_is_refused_with_reason(self, monkeypatch, tmp_path) -> None:
        bot, api, _ = self._bot(monkeypatch, tmp_path)
        self._serve(api, "не docx".encode())

        bot._handle_update(self._document("битый.docx"))

        assert "не читается" in api.texts()[-1]
        assert not api.keyboards(), "предлагать сохранить нечитаемое нельзя"

    def test_oversized_file_is_refused_before_download(self, monkeypatch, tmp_path) -> None:
        """Скачивать 25 МБ, чтобы потом отказать, — трата времени и трафика."""
        bot, api, _ = self._bot(monkeypatch, tmp_path)

        def refuse(*a, **k):
            pytest.fail("файл не должен скачиваться")

        api.get_file = refuse
        bot._handle_update(self._document("большой.pdf", size=25 * 1024 * 1024))

        assert "20 МБ" in api.texts()[-1]

    def test_unsupported_format_is_refused(self, monkeypatch, tmp_path) -> None:
        bot, api, _ = self._bot(monkeypatch, tmp_path)
        self._serve(api, b"PK\x03\x04")
        bot._handle_update(self._document("архив.zip"))
        assert "не принимается" in api.texts()[-1]
