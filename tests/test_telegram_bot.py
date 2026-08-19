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

    def test_non_text_message_is_answered_politely(self, monkeypatch) -> None:
        bot, api = self._bot(monkeypatch)
        bot._handle_update({"update_id": 1,
                            "message": {"chat": {"id": 1}, "from": {"id": ALLOWED},
                                        "voice": {"file_id": "x"}}})
        assert "только текст" in api.texts()[0]


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
