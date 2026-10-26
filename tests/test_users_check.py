"""Проверка «работает ли у всех»: каждый из белого списка и добавленные /adduser."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import selfcheck, site_db, users_check
from app.config import settings
from app.integrations import accounts, google_client
from app.telegram import access
from app.telegram.api import TelegramError

OWNER, TESTER, ADDED = 1107365044, 8058569481, 159893732


class FakeTelegram:
    def __init__(self, open_chats: set[int]) -> None:
        self.open_chats = open_chats
        self.closed = False

    def get_chat(self, user_id: int) -> dict:
        if user_id in self.open_chats:
            return {"first_name": "Иван", "username": "ivan"}
        raise TelegramError("getChat: Bad Request: chat not found")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def env(monkeypatch, tmp_path):
    conf = replace(settings, telegram_token="t", data_dir=tmp_path)
    monkeypatch.setattr(access, "settings", conf)
    monkeypatch.setattr(users_check, "settings", conf)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},{TESTER}")
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.example")
    monkeypatch.setenv("OPERON_MONGODB_URI", "mongodb://site/operon")
    monkeypatch.delenv("QDRANT_ROLES", raising=False)
    access.add(ADDED, OWNER)

    # Google подключён только у владельца.
    def status():
        if accounts.current() == str(OWNER):
            return {"connected": True, "account_hint": "kirill@gmail.com"}
        return {"connected": False, "reason": "Нет OAuth-токена пользователя"}

    monkeypatch.setattr(google_client, "status", status)
    # Роль на сайте есть только у владельца.
    monkeypatch.setattr(site_db, "access", lambda user=None: (
        site_db.Access("role", role_id="r1", role_name="Застройщик") if str(user) == str(OWNER)
        else site_db.Access("no_user")
    ))
    # Общая часть — без сети.
    for name in ("check_telegram", "check_model", "check_quant"):
        monkeypatch.setattr(selfcheck, name, lambda: [selfcheck.Check("часть", selfcheck.OK, "работает")])
    monkeypatch.setattr(selfcheck, "check_google", lambda: [
        selfcheck.Check("Google: вход через Google", selfcheck.FAIL, "адрес возврата не добавлен"),
        selfcheck.Check("Google: доступ выдан", selfcheck.OK, "это про владельца — в общей части не нужно"),
    ])
    fake = FakeTelegram({OWNER, TESTER})
    monkeypatch.setattr("app.telegram.api.TelegramAPI", lambda token, timeout=20.0: fake)
    yield fake
    access.remove(ADDED)


def test_report_for_all_users(env, capsys) -> None:
    code = users_check.main()
    out = capsys.readouterr().out
    assert code == 1  # есть сбои — код выхода это показывает

    # Общая часть: статусы самопроверки сохранились, Google — только общий пункт.
    assert "[  OK  ] часть: работает" in out
    assert "[ СБОЙ ] Google: вход через Google: адрес возврата не добавлен" in out
    assert "это про владельца" not in out

    # Все трое — с источником доступа.
    assert f"{OWNER} (владелец)" in out
    assert f"{TESTER} (белый список)" in out
    assert f"{ADDED} (добавлен /adduser)" in out

    owner_block = out.split(f"{OWNER} (владелец)")[1].split(f"{TESTER} (")[0]
    assert "Telegram: чат открыт (Иван, @ivan)" in owner_block
    assert "Google: подключён: kirill@gmail.com (вход через Google)" in owner_block
    assert "Квант: роль на сайте: Застройщик" in owner_block

    tester_block = out.split(f"{TESTER} (белый список)")[1].split(f"{ADDED} (")[0]
    assert "[ СБОЙ ] Google: не подключён — пусть отправит боту /auth" in tester_block
    assert "нет среди пользователей агента" in tester_block

    added_block = out.split(f"{ADDED} (добавлен /adduser)")[1]
    assert "не нажимал «Старт»" in added_block
    assert env.closed


def test_default_role_counts_as_access(env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_ROLES", "r1")
    users_check.main()
    out = capsys.readouterr().out
    tester_block = out.split(f"{TESTER} (белый список)")[1].split(f"{ADDED} (")[0]
    assert "действует роль по умолчанию" in tester_block
