"""Бесплатный поиск: разбор страниц поисковиков и запасные источники."""

from __future__ import annotations

import httpx
import pytest

from app.tools import free_search, web

DDG_PAGE = """
<div class="result results_links results_links_deep result--ad ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a"
      href="https://duckduckgo.com/y.js?ad_domain=shop.test">Реклама</a></h2>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcbr.ru%2Fhd_base%2FKeyRate%2F&amp;rut=abc">Ключевая <b>ставка</b> Банка России</a>
    </h2>
    <div class="result__extras"><a class="result__url" href="//duckduckgo.com/l/?uddg=x">cbr.ru</a></div>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Ключевая <b>ставка</b> &mdash; решение совета директоров.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a href="https://www.rbc.ru/finances/1" class="result__a" rel="nofollow">РБК: ставка</a></h2>
    <a class="result__snippet" href="https://www.rbc.ru/finances/1">Прямая ссылка, атрибуты в другом порядке.</a>
  </div>
</div>
"""

BING_PAGE = """
<ol id="b_results">
<li class="b_algo" data-id=""><div class="b_tpcn"></div>
  <h2 class=""><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1aHR0cHM6Ly9jYnIucnUv&amp;ntb=1" h="ID=SERP">Банк России</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">Официальный сайт <strong>ЦБ</strong></p></div>
</li>
<li class="b_algo"><h2><a href="https://www.consultant.ru/law/">КонсультантПлюс</a></h2><p>Законодательство</p></li>
</ol>
"""

NEWS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>ставка</title>
<item><title>ЦБ сохранил ставку - Интерфакс</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <pubDate>Fri, 12 Sep 2026 10:30:00 GMT</pubDate>
  <source url="https://www.interfax.ru">Интерфакс</source></item>
</channel></rss>"""


class TestParsers:
    def test_duckduckgo_skips_ads_and_unwraps_links(self) -> None:
        results = free_search.parse_duckduckgo(DDG_PAGE, 10)
        assert [r["url"] for r in results] == ["https://cbr.ru/hd_base/KeyRate/", "https://www.rbc.ru/finances/1"]
        assert results[0]["title"] == "Ключевая ставка Банка России"
        assert results[0]["snippet"].startswith("Ключевая ставка — решение")

    def test_bing_decodes_wrapped_links(self) -> None:
        results = free_search.parse_bing(BING_PAGE, 10)
        assert [r["url"] for r in results] == ["https://cbr.ru/", "https://www.consultant.ru/law/"]
        assert results[0]["snippet"] == "Официальный сайт ЦБ"

    def test_news_feed_has_publisher_and_date(self) -> None:
        item = free_search.parse_news_rss(NEWS_RSS, 5)[0]
        assert item["published"] == "2026-09-12"
        assert "Интерфакс" in item["snippet"]

    def test_limit_respected(self) -> None:
        assert len(free_search.parse_duckduckgo(DDG_PAGE, 1)) == 1

    def test_changed_markup_gives_empty_not_crash(self) -> None:
        assert free_search.parse_duckduckgo("<html>новая вёрстка</html>", 5) == []
        assert free_search.parse_bing("<html></html>", 5) == []


def fake_web(routes: dict[str, tuple[int, str]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for host, (status, body) in routes.items():
            if request.url.host == host:
                return httpx.Response(status, text=body)
        return httpx.Response(503, text="нет")

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestFallbacks:
    def test_web_and_news_combined(self) -> None:
        client = fake_web({
            "html.duckduckgo.com": (200, DDG_PAGE),
            "news.google.com": (200, NEWS_RSS),
        })
        results, failures = free_search.search("ставка", 6, client)
        engines = {r["engine"] for r in results}
        assert engines == {"DuckDuckGo", "Google News"}
        assert failures == []

    def test_captcha_on_duckduckgo_falls_back_to_bing(self) -> None:
        client = fake_web({
            "html.duckduckgo.com": (200, "<div class='anomaly-modal'>captcha</div>"),
            "www.bing.com": (200, BING_PAGE),
            "news.google.com": (500, ""),
        })
        results, failures = free_search.search("ставка", 6, client)
        assert results[0]["engine"] == "Bing"
        assert any("DuckDuckGo" in f and "робот" in f for f in failures)
        assert any("Google News" in f for f in failures)

    def test_wikipedia_when_everything_else_down(self) -> None:
        client = fake_web({
            "ru.wikipedia.org": (200, '{"query": {"search": [{"title": "Ключевая ставка", "snippet": "<span>ставка</span> ЦБ", "timestamp": "2026-01-02T00:00:00Z"}]}}'),
        })
        results, failures = free_search.search("ставка", 6, client)
        assert results[0]["url"] == "https://ru.wikipedia.org/wiki/%D0%9A%D0%BB%D1%8E%D1%87%D0%B5%D0%B2%D0%B0%D1%8F_%D1%81%D1%82%D0%B0%D0%B2%D0%BA%D0%B0"
        assert len(failures) == 3


class TestToolIntegration:
    @pytest.fixture(autouse=True)
    def free_mode(self, monkeypatch):
        for name in ("OPERON_SEARCH_PROVIDER", "TAVILY_API_KEY", "BRAVE_API_KEY",
                     "SERPER_API_KEY", "GOOGLE_CSE_KEY", "GOOGLE_CSE_ID"):
            monkeypatch.delenv(name, raising=False)

    def test_results_marked_external_with_engine(self, monkeypatch) -> None:
        monkeypatch.setattr(free_search, "search", lambda q, n: ([
            {"title": "t", "url": "https://a.test", "snippet": "s", "published": "", "engine": "Bing"}
        ], ["DuckDuckGo: ответ 403"]))
        result = web._internet_search({"query": "рынок"})
        assert result["provider"] == "free"
        assert result["source_type"] == "internet"
        assert result["results"][0]["engine"] == "Bing"
        assert result["sources_failed"] == ["DuckDuckGo: ответ 403"]

    def test_all_sources_down_is_named_not_invented(self, monkeypatch) -> None:
        from app.errors import ToolError

        monkeypatch.setattr(free_search, "search", lambda q, n: ([], ["DuckDuckGo: нет связи"]))
        with pytest.raises(ToolError, match="не заменяй"):
            web._internet_search({"query": "рынок"})
