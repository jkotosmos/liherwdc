"""Вход в Google без копирования адреса: клиент «Web application» + домен Amvera.

Человек нажимает /auth → ссылку → «Разрешить», Google возвращает код прямо на
сервер (/oauth2/callback), бот сам пишет «Google подключён». Проверяется
целиком, на настоящей библиотеке Google: ссылка, PKCE, обмен, сообщение в чат.
"""

from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from app.config import settings
from app.integrations import google_oauth
from app.telegram import bot as bot_module
from tests.test_telegram_bot import FakeAgent, make_bot, message

PUBLIC = "https://operon-bot.amvera.io"


@pytest.fixture
def web_client(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "305-test.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "web")
    monkeypatch.delenv("OPERON_OAUTH_REDIRECT_URI", raising=False)
    conf = replace(settings, public_url=PUBLIC)
    monkeypatch.setattr(google_oauth, "settings", conf)

    sent = {}

    def token_endpoint(session, method, url, data=None, **kwargs):
        sent["data"] = data if isinstance(data, dict) else dict(
            pair.split("=", 1) for pair in (data or "").split("&") if "=" in pair
        )
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({
            "access_token": "ya29.t", "refresh_token": "1//r", "expires_in": 3599, "token_type": "Bearer",
            "scope": " ".join(settings.google_scopes),
        }).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = url
        response.request = requests.Request(method, url).prepare()
        return response

    monkeypatch.setattr(requests.Session, "request", token_endpoint)
    monkeypatch.setattr(google_oauth.token_store, "save_token", lambda payload: "token-path")
    monkeypatch.setattr(google_oauth, "_account_email", lambda: "kirill@gmail.com")
    return sent


def test_auth_without_copying_anything(web_client, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.server import app

    bot, api = make_bot(FakeAgent([]), monkeypatch)
    bot._handle_update(message("/auth"))
    text = api.texts()[-1]
    assert "копировать ничего не нужно" in text

    link = text.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    query = parse_qs(urlparse(link).query)
    assert query["redirect_uri"] == [PUBLIC + "/oauth2/callback"]
    state = query["state"][0]

    # Google вернул браузер на сервер. Куки у браузера нет — путь публичный.
    page = TestClient(app).get("/oauth2/callback", params={"code": "4/0Axyz_abcdefghijkl", "state": state})
    assert page.status_code == 200 and "Google подключён" in page.text and "kirill@gmail.com" in page.text
    assert "code_verifier" in web_client["data"], "PKCE: секрет ссылки ушёл в Google"

    # Бот сам сообщает в чат — на очередном такте опроса.
    bot.sweep_expired()
    assert "Google подключён" in api.texts()[-1]
    assert bot._states[1].oauth_state == ""
