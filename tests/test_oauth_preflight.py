"""Проверка ссылки входа у Google до клика и порядок способов входа.

Значения authError взяты из настоящих ответов Google (октябрь 2026).
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.integrations import accounts, google_client, google_oauth, google_sa

# Настоящая проверка: conftest подменяет её в каждом тесте, а при импорте модуля она ещё своя.
REAL_PREFLIGHT = google_oauth.preflight
REAL_INVALID_CLIENT = "Cg5pbnZhbGlkX2NsaWVudBIfVGhlIE9BdXRoIGNsaWVudCB3YXMgbm90IGZvdW5kLiCRAw"
MISMATCH = base64.urlsafe_b64encode(b"\n\x15redirect_uri_mismatch\x12\x02ok").decode().rstrip("=")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "305-test.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "desktop")
    # Снимаем подмену из conftest — здесь проверяем саму проверку.
    monkeypatch.setattr(google_oauth, "preflight", REAL_PREFLIGHT)


def google_answers(monkeypatch, location: str):
    seen = {}

    def get(url, params=None, **kwargs):
        seen.update(params or {})
        return httpx.Response(302, headers={"location": location})

    monkeypatch.setattr(httpx, "get", get)
    return seen


def test_decode_real_google_error() -> None:
    url = f"https://accounts.google.com/signin/oauth/error?authError={REAL_INVALID_CLIENT}&flowName=x"
    assert google_oauth._decode_auth_error(url) == "invalid_client"


def test_mismatch_is_caught_before_click(client, monkeypatch) -> None:
    seen = google_answers(monkeypatch, f"https://accounts.google.com/signin/oauth/error?authError={MISMATCH}")
    ok, text = google_oauth.preflight()
    assert not ok and "http://localhost:8765/" in text and "Authorized redirect URIs" in text
    assert seen["redirect_uri"] == "http://localhost:8765/" and seen["client_id"].startswith("305-test")


def test_sign_in_page_means_ok(client, monkeypatch) -> None:
    google_answers(monkeypatch, "https://accounts.google.com/v3/signin/identifier?continue=...")
    assert google_oauth.preflight() == (True, "")


def test_network_trouble_does_not_block_login(client, monkeypatch) -> None:
    def broken(*a, **k):
        raise httpx.ConnectTimeout("нет сети")

    monkeypatch.setattr(httpx, "get", broken)
    assert google_oauth.preflight() == (True, "")


class TestAuthCommand:
    def test_broken_link_is_never_sent_and_service_account_is_offered(self, monkeypatch, tmp_path) -> None:
        from tests.test_google_service_account import KEY
        from tests.test_telegram_bot import FakeAgent, make_bot, message

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "305-test.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(KEY))
        monkeypatch.setattr(google_oauth, "preflight", lambda: (False, "Google не принимает адрес возврата X"))
        monkeypatch.setattr(google_oauth, "start", lambda label="": pytest.fail("ссылку выдавать нельзя"))
        bot, api = make_bot(FakeAgent([]), monkeypatch)
        bot._handle_update(message("/auth"))
        text = api.texts()[-1]
        assert "не сработает" in text and "адрес возврата X" in text and KEY["client_email"] in text

    def test_good_link_comes_with_button(self, monkeypatch) -> None:
        from tests.test_telegram_bot import FakeAgent, make_bot, message

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "305-test.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
        monkeypatch.setattr(google_oauth, "start", lambda label="": ("https://accounts.google.com/auth?x", "st"))
        bot, api = make_bot(FakeAgent([]), monkeypatch)
        bot._handle_update(message("/auth"))
        button = api.keyboards()[-1]["inline_keyboard"][0][0]
        assert button["text"].startswith("🔐") and button["url"].startswith("https://accounts.google.com")


def test_own_login_wins_over_service_account(monkeypatch, tmp_path) -> None:
    """У кого есть свой вход — работает он; у остальных — сервисный аккаунт."""
    from tests.test_google_service_account import KEY

    monkeypatch.setattr(google_sa, "settings", replace(settings, data_dir=tmp_path))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(KEY))
    monkeypatch.setattr(google_sa, "_try_delegated", lambda mail: False)
    monkeypatch.setattr(google_sa, "_shared_access", lambda mail: "writer")
    with accounts.use("159893732"):
        google_sa.connect("tester@gmail.com")
        monkeypatch.setattr(google_client.token_store, "token_exists", lambda: False)
        assert google_client.mode() == "shared" and google_client.default_calendar() == "tester@gmail.com"
        monkeypatch.setattr(google_client.token_store, "token_exists", lambda: True)
        assert google_client.mode() == "oauth" and google_client.default_calendar() == "primary"


class TestClientFile:
    """Клиент Google одним файлом в /data — без переменных в панели хостинга."""

    def _setup(self, monkeypatch, tmp_path, client: dict):
        from app import config

        monkeypatch.delenv("OPERON_GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OPERON_OAUTH_REDIRECT_URI", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "OLD-from-panel.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "old")
        root = tmp_path / "data"
        root.mkdir()
        (root / "client_secret_123-new.apps.googleusercontent.com.json").write_text(json.dumps(client))
        monkeypatch.setattr(config, "PERSIST_DIR", root)
        monkeypatch.setattr(google_oauth, "settings", replace(settings, credentials_dir=tmp_path / "cred"))

    def test_web_client_file_beats_old_panel_values(self, monkeypatch, tmp_path) -> None:
        self._setup(monkeypatch, tmp_path, {"web": {
            "client_id": "123-new.apps.googleusercontent.com", "client_secret": "s",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["https://operon-bot.amvera.io/oauth2/callback"],
        }})
        assert google_oauth.redirect_uri() == "https://operon-bot.amvera.io/oauth2/callback"
        assert google_oauth.callback_enabled() is True
        assert google_oauth._client_config()["web"]["client_id"].startswith("123-new")
        assert "файл client_secret_123-new" in google_oauth.describe_client()["source"]
        url, _state = google_oauth.start()
        assert "redirect_uri=https%3A%2F%2Foperon-bot.amvera.io%2Foauth2%2Fcallback" in url

    def test_desktop_client_file_uses_localhost(self, monkeypatch, tmp_path) -> None:
        self._setup(monkeypatch, tmp_path, {"installed": {
            "client_id": "123-new.apps.googleusercontent.com", "client_secret": "s",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }})
        assert google_oauth.redirect_uri() == "http://localhost:8765/"
        assert google_oauth.callback_enabled() is False

    def test_service_account_key_is_not_taken_for_a_client(self, monkeypatch, tmp_path) -> None:
        from tests.test_google_service_account import KEY

        self._setup(monkeypatch, tmp_path, KEY)
        assert google_oauth.client_file() is None
        assert "переменные окружения" in google_oauth.describe_client()["source"]
