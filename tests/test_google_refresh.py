"""Обновление токена Google: что видит человек, когда доступ отвалился.

Отозванный токен лечится повторным /auth, сбой сети — ожиданием. Перепутать
их значит отправить человека заново авторизоваться из-за VPN.
"""

from __future__ import annotations

from app.integrations import accounts, google_client


def test_revoked_token_asks_for_auth_and_names_testing_mode() -> None:
    text = google_client.refresh_error_message(
        Exception("('invalid_grant: Token has been expired or revoked.', {...})")
    )
    assert "/auth" in text and "Testing" in text and "7 дней" in text


def test_network_failure_does_not_ask_for_auth() -> None:
    text = google_client.refresh_error_message(TimeoutError("handshake timed out"))
    assert "/auth" not in text and "сеть" in text


def test_wrong_client_is_named() -> None:
    assert "GOOGLE_CLIENT_ID" in google_client.refresh_error_message(Exception("invalid_client"))


def test_account_email_is_cached_per_user(monkeypatch) -> None:
    calls = []

    class About:
        def __init__(self, email):
            self.email = email

        def about(self):
            return self

        def get(self, fields):
            return self

        def execute(self):
            calls.append(self.email)
            return {"user": {"emailAddress": self.email}}

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1107365044,8058569481")
    monkeypatch.setattr(
        google_client, "get_service",
        lambda api, version: About("kirill@gmail.com" if accounts.resolve() == "" else "michael@gmail.com"),
    )
    google_client.reset_cache()
    try:
        with accounts.use("1107365044"):
            assert google_client._account_email() == "kirill@gmail.com"
            assert google_client._account_email() == "kirill@gmail.com"
        with accounts.use("8058569481"):
            assert google_client._account_email() == "michael@gmail.com"
        assert calls == ["kirill@gmail.com", "michael@gmail.com"]
    finally:
        google_client.reset_cache()
