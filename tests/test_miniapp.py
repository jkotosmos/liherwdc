"""Mini App: вход по подписи Telegram, каталог и баланс шлюза, выбор модели."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import replace
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, model_choice, routerai, server
from app.config import settings
from app.telegram import webapp

BOT_TOKEN = "1234567890:AAFakeTokenForTests"
OWNER = 555000111


def signed_init_data(user_id: int = OWNER, token: str = BOT_TOKEN, age: int = 0, extra: dict | None = None) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Михаил"}, ensure_ascii=False),
        **(extra or {}),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def telegram_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(OWNER))
    conf = replace(settings, telegram_token=BOT_TOKEN)
    monkeypatch.setattr(webapp, "settings", conf)
    return conf


@pytest.fixture(autouse=True)
def clean_choice():
    path = settings.data_dir / "model.json"
    path.unlink(missing_ok=True)
    routerai.reset_cache()
    yield
    path.unlink(missing_ok=True)
    routerai.reset_cache()


class TestInitData:
    def test_valid_signature_returns_user(self, telegram_settings) -> None:
        assert webapp.validate(signed_init_data())["id"] == OWNER

    def test_signature_field_accepted_either_way(self, telegram_settings) -> None:
        """Поле signature входит в проверку по одной редакции документации и нет — по другой."""
        data = signed_init_data(extra={"signature": "abc"})
        assert webapp.validate(data)["id"] == OWNER

    def test_forged_hash_rejected(self, telegram_settings) -> None:
        data = signed_init_data(token="999:other-bot-token")
        with pytest.raises(webapp.InitDataError, match="Подпись"):
            webapp.validate(data)

    def test_tampered_user_rejected(self, telegram_settings) -> None:
        data = signed_init_data().replace(str(OWNER), "777")
        with pytest.raises(webapp.InitDataError):
            webapp.validate(data)

    def test_stranger_rejected_even_with_valid_signature(self, telegram_settings) -> None:
        with pytest.raises(webapp.InitDataError, match="TELEGRAM_ALLOWED_USERS"):
            webapp.validate(signed_init_data(user_id=42))

    def test_stale_data_rejected(self, telegram_settings) -> None:
        with pytest.raises(webapp.InitDataError, match="устарели"):
            webapp.validate(signed_init_data(age=webapp.MAX_AGE_SECONDS + 60))

    def test_missing_hash_rejected(self, telegram_settings) -> None:
        with pytest.raises(webapp.InitDataError):
            webapp.validate("auth_date=1&user=%7B%7D")


class TestMiniappUrl:
    def test_requires_https(self, monkeypatch) -> None:
        monkeypatch.setattr(webapp, "settings", replace(settings, public_url="http://localhost:8000"))
        assert webapp.miniapp_url() == ""

    def test_built_from_public_url(self, monkeypatch) -> None:
        monkeypatch.setattr(webapp, "settings", replace(settings, public_url="https://bot.example"))
        assert webapp.miniapp_url() == "https://bot.example/miniapp"


class TestCatalogParsing:
    def test_per_token_price_converted_to_million(self) -> None:
        assert routerai.per_million("0.000003") == pytest.approx(3.0)

    def test_per_million_price_kept(self) -> None:
        assert routerai.per_million(62) == 62

    def test_missing_price_stays_unknown(self) -> None:
        assert routerai.per_million(None) is None
        assert routerai.per_million("abc") is None

    def test_openrouter_shaped_model(self) -> None:
        model = routerai.normalize_model({
            "id": "anthropic/claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "context_length": 200000,
            "pricing": {"prompt": "0.0003", "completion": "0.0015"},
            "supported_parameters": ["tools", "temperature"],
        })
        assert model["price_in"] == pytest.approx(300)
        assert model["price_out"] == pytest.approx(1500)
        assert model["tools"] is True

    def test_tool_support_unknown_when_not_reported(self) -> None:
        assert routerai.normalize_model({"id": "x"})["tools"] is None


@pytest.fixture
def gateway(monkeypatch):
    """RouterAI с подменённым транспортом: ответы как у API в духе OpenRouter."""
    conf = replace(settings, provider="routerai", api_key="k", base_url="https://routerai.test/api/v1")
    monkeypatch.setattr(routerai, "settings", conf)
    responses = {
        "/models": {"data": [
            {"id": "openai/gpt-4.1-mini", "name": "GPT-4.1 Mini",
             "pricing": {"prompt": "0.00004", "completion": "0.00016"},
             "supported_parameters": ["tools"]},
            {"id": "text/only", "name": "Only Text", "supported_parameters": ["temperature"]},
        ]},
        "/credits": {"data": {"total_credits": 1000, "total_usage": 250.5}},
        "/key": {"usage": 250.5, "usage_monthly": 80, "limit": None, "limit_remaining": None},
    }

    def fake_get(path, client=None):
        return responses[path]

    monkeypatch.setattr(routerai, "_get", fake_get)
    return responses


class TestBilling:
    def test_balance_from_credits(self, gateway) -> None:
        data = routerai.billing()
        assert data["balance"] == pytest.approx(749.5)
        assert data["currency"] == "₽"
        assert data["key_usage_monthly"] == 80
        assert "key_limit" not in data, "пустой лимит не выдаём за ноль"

    def test_credits_unavailable_uses_key_limit(self, gateway, monkeypatch) -> None:
        def fake_get(path, client=None):
            if path == "/credits":
                raise routerai.BillingError("Шлюз ответил 403 на /credits.")
            return {"data": {"usage": 10, "limit": 500, "limit_remaining": 490}}

        monkeypatch.setattr(routerai, "_get", fake_get)
        data = routerai.billing()
        assert data["balance"] == 490
        assert data["balance_source"] == "лимит ключа"
        assert data["errors"]

    def test_real_http_shape(self, monkeypatch) -> None:
        conf = replace(settings, provider="routerai", api_key="k", base_url="https://routerai.test/api/v1")
        monkeypatch.setattr(routerai, "settings", conf)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer k"
            assert request.url.path == "/api/v1/models"
            return httpx.Response(200, json={"data": [{"id": "a/b", "name": "B"}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert routerai.list_models(force=True, client=client)[0]["id"] == "a/b"


class TestModelChoice:
    def test_default_is_env_model(self) -> None:
        assert model_choice.current_model() == settings.model

    def test_choice_persists(self) -> None:
        model_choice.set_model("openai/gpt-4.1-mini", changed_by="555")
        assert model_choice.current_model() == "openai/gpt-4.1-mini"
        assert model_choice.choice()["changed_by"] == "555"

    def test_agent_uses_choice_and_forgets_old_caps(self) -> None:
        from app.agent import OperonAgent, Session

        agent = OperonAgent()
        session = Session(session_id="m")
        agent._request_params(session)
        agent._max_tokens_cap = 4096
        agent._unsupported_params.add("output_config")

        model_choice.set_model("vendor/new-model")
        params = agent._request_params(session)
        assert params["model"] == "vendor/new-model"
        assert agent._max_tokens_cap is None
        assert not agent._unsupported_params


@pytest.fixture
def protected(monkeypatch, telegram_settings):
    guarded = replace(settings, access_password="секрет-123", telegram_token=BOT_TOKEN)
    monkeypatch.setattr(server, "settings", guarded)
    monkeypatch.setattr(auth, "settings", guarded)
    with TestClient(server.app) as client:
        yield client


class TestMiniappApi:
    def test_page_is_public(self, protected: TestClient) -> None:
        response = protected.get("/miniapp")
        assert response.status_code == 200
        assert "telegram-web-app.js" in response.text

    def test_api_closed_without_token(self, protected: TestClient) -> None:
        assert protected.get("/api/billing").status_code == 401

    def test_telegram_login_gives_bearer_token(self, protected: TestClient, gateway) -> None:
        response = protected.post("/api/telegram/auth", json={"init_data": signed_init_data()})
        assert response.status_code == 200
        token = response.json()["token"]

        billing = protected.get("/api/billing", headers={"Authorization": f"Bearer {token}"})
        assert billing.status_code == 200
        assert billing.json()["balance"] == pytest.approx(749.5)

    def test_stranger_gets_403(self, protected: TestClient) -> None:
        response = protected.post("/api/telegram/auth", json={"init_data": signed_init_data(user_id=42)})
        assert response.status_code == 403

    def test_forged_bearer_rejected(self, protected: TestClient) -> None:
        response = protected.get("/api/billing", headers={"Authorization": "Bearer 1.deadbeef"})
        assert response.status_code == 401

    def _token(self, client: TestClient) -> dict[str, str]:
        token = client.post("/api/telegram/auth", json={"init_data": signed_init_data()}).json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def test_models_hide_those_without_tools(self, protected: TestClient, gateway) -> None:
        payload = protected.get("/api/models", headers=self._token(protected)).json()
        assert [m["id"] for m in payload["models"]] == ["openai/gpt-4.1-mini"]
        assert payload["hidden_without_tools"] == 1

    def test_switch_model(self, protected: TestClient, gateway) -> None:
        headers = self._token(protected)
        response = protected.post("/api/model", json={"model": "openai/gpt-4.1-mini"}, headers=headers)
        assert response.status_code == 200
        assert model_choice.current_model() == "openai/gpt-4.1-mini"
        assert protected.get("/api/status", headers=headers).json()["model"] == "openai/gpt-4.1-mini"

    def test_unknown_model_refused(self, protected: TestClient, gateway) -> None:
        response = protected.post("/api/model", json={"model": "no/such"}, headers=self._token(protected))
        assert response.status_code == 404
        assert model_choice.current_model() == settings.model

    def test_model_without_tools_refused(self, protected: TestClient, gateway) -> None:
        response = protected.post("/api/model", json={"model": "text/only"}, headers=self._token(protected))
        assert response.status_code == 422


def test_web_chat_does_not_load_telegram_script(protected: TestClient) -> None:
    """Недоступный telegram.org не должен тормозить обычный веб-чат."""
    protected.post("/api/login", json={"password": "секрет-123"})
    assert "telegram-web-app.js" not in protected.get("/").text
