"""Авторизация Google: набор прав и разбор кода из адресной строки.

Состав scope'ов зафиксирован тестом намеренно. Каждое изменение набора — это
новый экран согласия и заново пройденный OAuth: старый токен новых прав не
получает. Такое решение должно приниматься осознанно, а не проскакивать
незамеченным в рефакторинге.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.integrations import google_oauth

DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"

DESKTOP_CLIENT = {
    "installed": {
        "client_id": "123.apps.googleusercontent.com",
        "project_id": "operon",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "GOCSPX-test",
        "redirect_uris": ["http://localhost"],
    }
}


@pytest.fixture
def client_secret(tmp_path, monkeypatch):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(DESKTOP_CLIENT), encoding="utf-8")
    monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(path))
    return path


class TestScopeSet:
    def test_exact_scope_set(self) -> None:
        assert set(settings.google_scopes) == {DRIVE_READONLY, DRIVE_FILE, CALENDAR_EVENTS}

    def test_no_full_drive_access(self) -> None:
        """drive.file даёт доступ только к файлам, созданным самим агентом."""
        assert "https://www.googleapis.com/auth/drive" not in settings.google_scopes

    def test_no_calendar_listing_scope(self) -> None:
        """Полный calendar не нужен: работаем с событиями основного календаря."""
        assert "https://www.googleapis.com/auth/calendar" not in settings.google_scopes

    def test_scopes_are_overridable(self, monkeypatch) -> None:
        from dataclasses import replace

        limited = replace(settings, google_scopes=(DRIVE_READONLY,))
        assert limited.google_scopes == (DRIVE_READONLY,)


class TestAuthorizationUrl:
    def test_url_requests_offline_access(self, client_secret) -> None:
        """Без refresh_token доступ отвалится через час и потребует ручной работы."""
        url = google_oauth.authorization_url()
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_url_carries_every_scope(self, client_secret) -> None:
        url = google_oauth.authorization_url()
        for scope in (DRIVE_READONLY, DRIVE_FILE, CALENDAR_EVENTS):
            assert scope.rsplit("/", 1)[-1] in url

    def test_missing_client_secret_explains_where_to_get_it(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(tmp_path / "нет.json"))
        with pytest.raises(google_oauth.OAuthError, match="Desktop app"):
            google_oauth.authorization_url()

    def test_web_client_with_foreign_redirect_is_refused(self, monkeypatch, tmp_path) -> None:
        path = tmp_path / "web.json"
        path.write_text(json.dumps({"web": {
            "client_id": "1", "client_secret": "2",
            "auth_uri": "https://a", "token_uri": "https://t",
            "redirect_uris": ["https://example.test/cb"],
        }}), encoding="utf-8")
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(path))
        with pytest.raises(google_oauth.OAuthError, match="redirect URIs"):
            google_oauth.authorization_url()


class TestCodeExtraction:
    """Пользователь копирует то, что видит, — разбирать надо все формы."""

    @pytest.mark.parametrize(
        ("pasted", "expected"),
        [
            ("http://localhost:8765/?code=4%2F0Axyz_abc-def&scope=x", "4/0Axyz_abc-def"),
            ("localhost:8765/?code=4/0Axyz_abc-def", "4/0Axyz_abc-def"),
            ("4/0Axyz_abcdefghijkl", "4/0Axyz_abcdefghijkl"),
            ("4%2F0Axyz_abcdefghijkl", "4/0Axyz_abcdefghijkl"),
            ("  4/0Axyz_abcdefghijkl  ", "4/0Axyz_abcdefghijkl"),
        ],
    )
    def test_code_is_extracted(self, pasted: str, expected: str) -> None:
        assert google_oauth.extract_code(pasted) == expected

    def test_denied_access_is_explained(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="отказ"):
            google_oauth.extract_code("http://localhost:8765/?error=access_denied")

    def test_url_without_code_is_rejected(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="code"):
            google_oauth.extract_code("http://localhost:8765/?state=abc&code=")

    @pytest.mark.parametrize(
        "text", ["какие встречи на завтра?", "привет", "", "подготовь КП для Иванова"]
    )
    def test_ordinary_text_is_not_a_code(self, text: str) -> None:
        assert google_oauth.looks_like_code(text) is False

    @pytest.mark.parametrize(
        "text", ["4/0Axyz_abcdefghijkl", "http://localhost:8765/?code=4/0A"]
    )
    def test_code_is_recognised(self, text: str) -> None:
        assert google_oauth.looks_like_code(text) is True


class TestInstructions:
    def test_instructions_warn_about_the_browser_error(self) -> None:
        """Ошибка соединения — ожидаемый шаг, иначе пользователь решит, что сломалось."""
        text = google_oauth.instructions()
        assert "ошибку соединения" in text
        assert settings.oauth_redirect_uri in text
