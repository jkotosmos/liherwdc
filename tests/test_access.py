"""Доступ к боту из чата: /adduser, /removeuser, /users.

Нужен, когда правка TELEGRAM_ALLOWED_USERS в панели хостинга не доезжает до
контейнера: владелец добавляет человека прямо в боте, без перезапуска.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import settings
from app.telegram import access
from app.telegram import bot as bot_module
from tests.test_telegram_bot import FakeAgent, FakeAPI, message

OWNER, TESTER, STRANGER = 8058569481, 159893732, 555


@pytest.fixture
def bot(monkeypatch, tmp_path):
    conf = replace(settings, telegram_token="t", data_dir=tmp_path)
    monkeypatch.setattr(bot_module, "settings", conf)
    monkeypatch.setattr(access, "settings", conf)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"1107365044,{OWNER}")
    api = FakeAPI()
    return bot_module.TelegramBot(api=api, agent=FakeAgent([{"type": "text", "text": "ок"}, {"type": "done"}])), api


def test_owner_adds_tester_and_tester_can_write(bot) -> None:
    b, api = bot
    b._handle_update(message("привет", user_id=TESTER, chat_id=TESTER))
    assert api.texts() == [], "до добавления бот молчит"

    b._handle_update(message(f"/adduser {TESTER}", user_id=OWNER, chat_id=OWNER))
    assert "добавлен" in api.texts()[-1]
    assert TESTER in access.allowed()

    b._handle_update(message("привет", user_id=TESTER, chat_id=TESTER))
    assert api.texts()[-1] != "", "теперь отвечает"
    b._handle_update(message("/users", user_id=OWNER, chat_id=OWNER))
    assert str(TESTER) in api.texts()[-1]


def test_added_user_cannot_hand_out_access(bot) -> None:
    b, api = bot
    access.add(TESTER, OWNER)
    b._handle_update(message(f"/adduser {STRANGER}", user_id=TESTER, chat_id=TESTER))
    assert "только владельцы" in api.texts()[-1] and STRANGER not in access.allowed()


def test_remove_and_owner_protection(bot) -> None:
    b, api = bot
    access.add(TESTER, OWNER)
    b._handle_update(message("/removeuser 1107365044", user_id=OWNER, chat_id=OWNER))
    assert "настроек сервера" in api.texts()[-1] and 1107365044 in access.allowed()
    b._handle_update(message(f"/removeuser {TESTER}", user_id=OWNER, chat_id=OWNER))
    assert "закрыт" in api.texts()[-1] and TESTER not in access.allowed()


def test_bad_id_is_explained(bot) -> None:
    b, api = bot
    b._handle_update(message("/adduser вася", user_id=OWNER, chat_id=OWNER))
    assert "@userinfobot" in api.texts()[-1]


def test_mini_app_accepts_added_user(bot, monkeypatch) -> None:
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    from app.telegram import webapp

    access.add(TESTER, OWNER)
    monkeypatch.setattr(webapp, "settings", replace(settings, telegram_token="123:abc"))
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": TESTER})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", b"123:abc", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    assert webapp.validate(urlencode(fields))["id"] == TESTER
