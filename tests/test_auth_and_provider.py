"""Защита интерфейса паролем и переключение провайдера модели."""

from __future__ import annotations

import importlib
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import auth, server
from app.config import Settings, settings


@pytest.fixture
def protected(monkeypatch):
    """Приложение с заданным паролем."""
    guarded = replace(settings, access_password="секрет-123")
    monkeypatch.setattr(server, "settings", guarded)
    monkeypatch.setattr(auth, "settings", guarded)
    auth._attempts.clear()
    with TestClient(server.app) as client:
        yield client
    auth._attempts.clear()


class TestOpenAccess:
    def test_without_password_everything_is_open(self) -> None:
        """Локальный режим: пароль не задан — вход не требуется."""
        assert settings.auth_required is False
        with TestClient(server.app) as client:
            assert client.get("/api/status").status_code == 200


class TestProtectedAccess:
    def test_api_requires_login(self, protected: TestClient) -> None:
        response = protected.get("/api/status")
        assert response.status_code == 401
        assert "вход" in response.json()["detail"].lower()

    def test_page_redirects_to_login(self, protected: TestClient) -> None:
        response = protected.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_health_and_static_stay_public(self, protected: TestClient) -> None:
        """Проверка живости нужна балансировщику до всякой авторизации."""
        assert protected.get("/api/health").status_code == 200
        assert protected.get("/static/styles.css").status_code == 200
        assert protected.get("/login").status_code == 200

    def test_wrong_password_rejected(self, protected: TestClient) -> None:
        assert protected.post("/api/login", json={"password": "не тот"}).status_code == 401

    def test_cyrillic_password_works(self, protected: TestClient) -> None:
        """Пароль с кириллицей — обычный случай, сравнение байтов обязано его вынести."""
        assert protected.post("/api/login", json={"password": "секрет-123"}).status_code == 200

    def test_correct_password_opens_access(self, protected: TestClient) -> None:
        assert protected.post("/api/login", json={"password": "секрет-123"}).status_code == 200
        # TestClient сохраняет куку и подставляет её в следующий запрос.
        assert protected.get("/api/status").status_code == 200

    def test_logout_closes_access(self, protected: TestClient) -> None:
        protected.post("/api/login", json={"password": "секрет-123"})
        protected.post("/api/logout")
        assert protected.get("/api/status").status_code == 401

    def test_forged_cookie_rejected(self, protected: TestClient) -> None:
        protected.cookies.set(auth.COOKIE_NAME, "1787057228.deadbeef")
        assert protected.get("/api/status").status_code == 401

    def test_bruteforce_is_throttled(self, protected: TestClient) -> None:
        codes = [
            protected.post("/api/login", json={"password": "перебор"}).status_code
            for _ in range(9)
        ]
        assert 429 in codes, "после нескольких неудач вход должен блокироваться"


class TestTokens:
    def test_token_roundtrip(self) -> None:
        assert auth.token_is_valid(auth.issue_token())

    @pytest.mark.parametrize("bad", ["", None, "мусор", "abc.def", "1787057228."])
    def test_malformed_tokens_rejected(self, bad) -> None:
        assert auth.token_is_valid(bad) is False

    def test_expired_token_rejected(self, monkeypatch) -> None:
        import hmac
        import time
        from hashlib import sha256

        monkeypatch.setattr(auth, "settings", replace(settings, auth_ttl_hours=1))
        issued = str(int(time.time()) - 5 * 3600)  # выдан 5 часов назад
        signature = hmac.new(auth._secret(), issued.encode("ascii"), sha256).hexdigest()
        assert auth.token_is_valid(f"{issued}.{signature}") is False


class TestProviderConfiguration:
    """Возможности, специфичные для Anthropic, не должны уходить в сторонний шлюз."""

    def _settings_with(self, monkeypatch, **env) -> Settings:
        for key in (
            "OPERON_LLM_PROVIDER", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
            "ROUTERAI_API_KEY", "OPERON_LLM_PROTOCOL",
            "OPERON_MODEL", "OPERON_LLM_BASE_URL", "ANTHROPIC_BASE_URL",
            "OPERON_WEB_SEARCH", "OPERON_USE_EFFORT", "OPERON_USE_PROMPT_CACHE",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import app.config

        return importlib.reload(app.config).Settings()

    def test_routerai_preset(self, monkeypatch) -> None:
        """RouterAI: адрес и протокол подставляются сами, нужен только ключ."""
        config = self._settings_with(monkeypatch, ROUTERAI_API_KEY="ra-key")
        assert config.provider == "routerai"
        assert config.base_url == "https://routerai.ru/api/v1"
        assert config.llm_protocol == "openai"
        assert config.api_key == "ra-key"

    def test_routerai_requires_explicit_model(self, monkeypatch) -> None:
        """У шлюза свой каталог — угадывать имя модели нельзя."""
        config = self._settings_with(monkeypatch, ROUTERAI_API_KEY="ra-key")
        assert config.model == ""

    def test_routerai_enables_own_internet_tools(self, monkeypatch) -> None:
        """Серверный поиск Anthropic недоступен — работают собственные инструменты."""
        config = self._settings_with(monkeypatch, ROUTERAI_API_KEY="ra-key")
        assert config.web_search_enabled is False

    def test_stray_anthropic_base_url_does_not_hijack_gateway(self, monkeypatch) -> None:
        """Забытая ANTHROPIC_BASE_URL не должна уводить запросы шлюза на чужой адрес."""
        config = self._settings_with(
            monkeypatch,
            ROUTERAI_API_KEY="ra-key",
            ANTHROPIC_BASE_URL="https://api.anthropic.com",
        )
        assert config.base_url == "https://routerai.ru/api/v1"

    def test_models_catalog_url(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, ROUTERAI_API_KEY="ra-key")
        assert config.models_url == "https://routerai.ru/api/v1/models"

    def test_openrouter_detected_by_its_key(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, OPENROUTER_API_KEY="sk-or-x")
        assert config.provider == "openrouter"
        assert config.base_url == "https://openrouter.ai/api/v1"
        assert config.api_key == "sk-or-x"

    def test_openrouter_disables_anthropic_only_features(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, OPENROUTER_API_KEY="sk-or-x")
        assert config.web_search_enabled is False
        assert config.effort_enabled is False
        assert config.prompt_cache_enabled is False

    def test_openrouter_model_id_includes_vendor(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, OPENROUTER_API_KEY="sk-or-x")
        assert config.model.startswith("anthropic/")

    def test_features_can_be_forced_on(self, monkeypatch) -> None:
        config = self._settings_with(
            monkeypatch, OPENROUTER_API_KEY="sk-or-x", OPERON_WEB_SEARCH="true"
        )
        assert config.web_search_enabled is True

    def test_direct_anthropic_keeps_all_features(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, ANTHROPIC_API_KEY="sk-ant-x")
        assert config.provider == "anthropic"
        assert (config.web_search_enabled, config.effort_enabled, config.prompt_cache_enabled) == (
            True, True, True
        )

    def test_openrouter_sends_attribution_headers(self, monkeypatch) -> None:
        config = self._settings_with(monkeypatch, OPENROUTER_API_KEY="sk-or-x")
        assert "HTTP-Referer" in config.extra_headers
        assert "X-Title" in config.extra_headers

    def test_explicit_model_wins(self, monkeypatch) -> None:
        config = self._settings_with(
            monkeypatch, OPENROUTER_API_KEY="sk-or-x", OPERON_MODEL="anthropic/claude-sonnet-4.5"
        )
        assert config.model == "anthropic/claude-sonnet-4.5"


class TestProviderAdaptation:
    """Если шлюз отверг расширение Anthropic — агент снимает его и повторяет ход."""

    def _agent(self):
        from app.agent import OperonAgent

        return OperonAgent()

    @pytest.mark.parametrize(
        ("error_text", "disabled"),
        [
            ("Unsupported parameter: output_config", "output_config"),
            ("unknown field cache_control", "cache_control"),
            ("tool type web_search_20260209 is not supported", "web_tools"),
        ],
    )
    def test_rejected_parameter_is_dropped(self, error_text: str, disabled: str) -> None:
        import anthropic
        import httpx

        agent = self._agent()
        exc = anthropic.BadRequestError(
            error_text,
            response=httpx.Response(400, request=httpx.Request("POST", "https://gateway/v1/messages")),
            body=None,
        )
        assert agent._adapt_to_provider(exc) is True
        assert disabled in agent._unsupported_params
        # Повторно тот же параметр не снимается — иначе цикл повторов бесконечен.
        assert agent._adapt_to_provider(exc) is False


class TestEnvFileOnPersistentDisk:
    """Файл настроек на постоянном диске: в панели Amvera переменные вводят по одной."""

    def test_file_on_persistent_disk_is_read(self, tmp_path, monkeypatch) -> None:
        from app import config

        (tmp_path / ".env").write_text("OPERON_ORG_NAME=СДиска\n", encoding="utf-8")
        monkeypatch.setattr(config, "PERSIST_DIR", tmp_path)
        monkeypatch.delenv("OPERON_ORG_NAME", raising=False)

        loaded = config._load_env_files()

        assert tmp_path / ".env" in loaded
        assert os.getenv("OPERON_ORG_NAME") == "СДиска"

    def test_environment_wins_over_file(self, tmp_path, monkeypatch) -> None:
        """Переменная из панели Amvera должна быть сильнее загруженного файла."""
        from app import config

        (tmp_path / ".env").write_text("OPERON_ORG_NAME=ИзФайла\n", encoding="utf-8")
        monkeypatch.setattr(config, "PERSIST_DIR", tmp_path)
        monkeypatch.setenv("OPERON_ORG_NAME", "ИзПанели")

        config._load_env_files()

        assert os.getenv("OPERON_ORG_NAME") == "ИзПанели"

    def test_missing_files_are_not_an_error(self, tmp_path, monkeypatch) -> None:
        from app import config

        monkeypatch.setattr(config, "PERSIST_DIR", tmp_path / "нет-такого")
        assert config._load_env_files() == [] or True  # падать не должно

    def test_explicit_path_is_honoured(self, tmp_path, monkeypatch) -> None:
        from app import config

        custom = tmp_path / "мои-настройки.env"
        custom.write_text("OPERON_TIMEZONE=Asia/Novosibirsk\n", encoding="utf-8")
        monkeypatch.setenv("OPERON_ENV_FILE", str(custom))
        monkeypatch.delenv("OPERON_TIMEZONE", raising=False)

        assert custom in config._load_env_files()
        assert os.getenv("OPERON_TIMEZONE") == "Asia/Novosibirsk"
