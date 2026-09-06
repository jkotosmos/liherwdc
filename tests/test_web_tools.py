"""Собственный интернет-поиск: провайдеры, деградация, разделение источников."""

from __future__ import annotations

import httpx
import pytest

from app.errors import ToolError
from app.tools import web


@pytest.fixture(autouse=True)
def clean_search_env(monkeypatch):
    for name in (
        "OPERON_SEARCH_PROVIDER", "TAVILY_API_KEY", "BRAVE_API_KEY",
        "SERPER_API_KEY", "GOOGLE_CSE_KEY", "GOOGLE_CSE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


class TestProviderDetection:
    def test_no_keys_means_not_configured(self) -> None:
        assert web.search_is_configured() is False

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"TAVILY_API_KEY": "k"}, "tavily"),
            ({"BRAVE_API_KEY": "k"}, "brave"),
            ({"SERPER_API_KEY": "k"}, "serper"),
            ({"GOOGLE_CSE_KEY": "k", "GOOGLE_CSE_ID": "i"}, "google"),
        ],
    )
    def test_provider_detected_by_key(self, monkeypatch, env: dict, expected: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert web._search_provider() == expected

    def test_explicit_choice_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        monkeypatch.setenv("OPERON_SEARCH_PROVIDER", "brave")
        assert web._search_provider() == "brave"


class TestDegradation:
    def test_without_key_tells_model_not_to_invent(self) -> None:
        """Ключевое требование ТЗ: нет данных — не выдумывать."""
        result = web._internet_search({"query": "рынок СЭД 2026"})
        assert result["status"] == "not_configured"
        assert "не выдумывай" in result["hint"].lower()

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ToolError):
            web._internet_search({"query": "   "})

    def test_unknown_provider_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_SEARCH_PROVIDER", "яндекс")
        with pytest.raises(ToolError, match="Неизвестный поисковый провайдер"):
            web._internet_search({"query": "тест"})


class TestSearchResults:
    def _tavily_response(self, monkeypatch, payload: dict, status: int = 200) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "k")

        def fake_post(url, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(status, json=payload, request=request)

        monkeypatch.setattr(web.httpx, "post", fake_post)

    def test_results_carry_links_and_retrieval_date(self, monkeypatch) -> None:
        self._tavily_response(
            monkeypatch,
            {"results": [
                {"title": "Обзор рынка", "url": "https://example.test/a",
                 "content": "Рынок вырос на 12%", "published_date": "2026-07-01"}
            ]},
        )
        result = web._internet_search({"query": "рынок"})

        assert result["status"] == "ok"
        assert result["source_type"] == "internet"
        assert result["results"][0]["url"] == "https://example.test/a"
        assert result["retrieved_at"]
        # Модели прямо сказано не смешивать внешние данные с внутренними.
        assert "не смешивай" in result["note"].lower()

    def test_empty_results_are_reported_honestly(self, monkeypatch) -> None:
        self._tavily_response(monkeypatch, {"results": []})
        result = web._internet_search({"query": "несуществующее"})
        assert result["status"] == "not_found"
        assert "догадкой" in result["hint"]

    def test_http_error_explains_cause(self, monkeypatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "k")

        def fake_post(url, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(401, json={}, request=request)

        monkeypatch.setattr(web.httpx, "post", fake_post)
        with pytest.raises(ToolError, match="401"):
            web._internet_search({"query": "тест"})

    def test_result_limit_is_bounded(self, monkeypatch) -> None:
        captured = {}
        monkeypatch.setenv("TAVILY_API_KEY", "k")

        def fake_post(url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return httpx.Response(200, json={"results": []}, request=httpx.Request("POST", url))

        monkeypatch.setattr(web.httpx, "post", fake_post)
        web._internet_search({"query": "x", "max_results": 999})
        assert captured["max_results"] == 15


class TestOpenUrl:
    def _page(self, monkeypatch, body: str, content_type: str = "text/html", status: int = 200) -> None:
        def fake_get(url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(
                status, text=body, headers={"content-type": content_type}, request=request
            )

        monkeypatch.setattr(web.httpx, "get", fake_get)

    def test_html_is_stripped_to_text(self, monkeypatch) -> None:
        self._page(
            monkeypatch,
            "<html><head><style>a{}</style><script>alert(1)</script></head>"
            "<body><h1>Заголовок</h1><p>Текст статьи</p></body></html>",
        )
        result = web._open_url({"url": "https://example.test/page"})
        assert "Заголовок" in result["content"]
        assert "Текст статьи" in result["content"]
        assert "alert(1)" not in result["content"]
        assert "<p>" not in result["content"]

    def test_citation_includes_url_and_date(self, monkeypatch) -> None:
        self._page(monkeypatch, "<p>ок</p>")
        result = web._open_url({"url": "https://example.test/page"})
        assert "example.test" in result["citation"]
        assert result["retrieved_at"] in result["citation"]

    def test_binary_content_is_refused_clearly(self, monkeypatch) -> None:
        self._page(monkeypatch, "%PDF-1.4", content_type="application/pdf")
        with pytest.raises(ToolError, match="application/pdf"):
            web._open_url({"url": "https://example.test/doc.pdf"})

    @pytest.mark.parametrize("bad", ["example.test", "ftp://x/y", ""])
    def test_non_http_urls_rejected(self, bad: str) -> None:
        with pytest.raises(ToolError, match="http"):
            web._open_url({"url": bad})

    def test_http_error_reported(self, monkeypatch) -> None:
        self._page(monkeypatch, "not found", status=404)
        with pytest.raises(ToolError, match="404"):
            web._open_url({"url": "https://example.test/missing"})


class TestRegistration:
    def test_tools_are_registered_with_schemas(self) -> None:
        from app.tools.base import ToolRegistry

        probe = ToolRegistry()
        original = web.registry
        web.registry = probe
        try:
            web.register_web_tools()
        finally:
            web.registry = original

        assert probe.names() == ["internet_search", "open_url"]
        # Чтение интернета данные не меняет — подтверждение не требуется.
        assert all(spec.requires_confirmation is False for spec in probe.all())


class TestSearchErrorsExplainThemselves:
    """Совет «проверьте ключ» вреден, когда ключ верен, а выключен API."""

    @staticmethod
    def _http_error(status: int, payload=None, text: str = ""):
        import httpx

        response = httpx.Response(
            status,
            json=payload if payload is not None else None,
            text=text if payload is None else None,
            request=httpx.Request("GET", "https://example.test"),
        )
        return httpx.HTTPStatusError("ошибка", request=response.request, response=response)

    def test_google_disabled_api_is_quoted_verbatim(self) -> None:
        from app.tools.web import _describe_search_error

        message = _describe_search_error(
            "google",
            self._http_error(403, {"error": {"message": "Custom Search API has not been used in project 307680726974 before or it is disabled."}}),
        )
        assert "Custom Search API" in message
        assert "307680726974" in message, "номер проекта нужен, чтобы найти нужную страницу"
        assert "проверьте ключ" not in message.lower(), "ключ здесь ни при чём"

    def test_plain_string_error_is_shown(self) -> None:
        from app.tools.web import _describe_search_error

        message = _describe_search_error("tavily", self._http_error(401, {"error": "Invalid API key"}))
        assert "Invalid API key" in message

    def test_bodyless_error_still_hints(self) -> None:
        """Без объяснения от поставщика подсказка нужна, но по коду ответа."""
        from app.tools.web import _describe_search_error

        assert "лимит" in _describe_search_error("brave", self._http_error(429, text=""))
        assert "ключ" in _describe_search_error("brave", self._http_error(401, text=""))

    def test_provider_name_is_named(self) -> None:
        from app.tools.web import _describe_search_error

        assert "serper" in _describe_search_error("serper", self._http_error(500, text="oops"))
