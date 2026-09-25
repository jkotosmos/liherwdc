"""Бесплатный интернет-поиск без ключей и регистрации.

Платных поисковых API не нужно: результаты берутся с открытых страниц.

* **DuckDuckGo** (html-версия) — основной веб-поиск;
* **Bing** — запасной, если DuckDuckGo не ответил или показал проверку
  «вы не робот» (так бывает с адресами дата-центров);
* **Google News** (RSS) — свежие новости: рынок, законодательство, конкуренты;
* **Википедия** — справка, если оба поисковика недоступны.

Разметка чужих страниц может меняться, поэтому каждый источник изолирован:
отказ одного не роняет остальные, а что именно не ответило — видно в
результате и в /check.
"""

from __future__ import annotations

import base64
import html
import logging
import re
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(12.0, connect=6.0)
# Обычный браузерный заголовок: с «ботовым» поисковики отдают пустую страницу.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}

Result = dict[str, str]


class SourceError(Exception):
    """Источник не ответил или ответил не тем."""


def _text(fragment: str) -> str:
    """HTML-фрагмент → чистый текст."""
    no_tags = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _get(url: str, client: httpx.Client | None, **params: Any) -> httpx.Response:
    try:
        if client is not None:
            response = client.get(url, params=params, headers=HEADERS)
        else:
            response = httpx.get(url, params=params, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"нет связи ({exc.__class__.__name__})") from exc
    if response.status_code >= 400:
        raise SourceError(f"ответ {response.status_code}")
    return response


# --- DuckDuckGo --------------------------------------------------------------

_DDG_LINK = re.compile(r'<a([^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*)>(.*?)</a>', re.S)
_HREF = re.compile(r'href="([^"]+)"')
_DDG_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', re.S)


def _ddg_target(href: str) -> str:
    """Ссылка DuckDuckGo ведёт через редирект; настоящий адрес — в параметре uddg."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target
    return href


def parse_duckduckgo(page: str, limit: int) -> list[Result]:
    results: list[Result] = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b', page)[1:]
    for block in blocks:
        if "result--ad" in block[:200]:
            continue  # реклама
        link = _DDG_LINK.search(block)
        href = _HREF.search(link.group(1)) if link else None
        if not href:
            continue
        url = _ddg_target(href.group(1))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue
        snippet = _DDG_SNIPPET.search(block)
        results.append({
            "title": _text(link.group(2)),
            "url": url,
            "snippet": _text(snippet.group(1)) if snippet else "",
            "published": "",
            "engine": "DuckDuckGo",
        })
        if len(results) >= limit:
            break
    return results


def duckduckgo(query: str, limit: int, client: httpx.Client | None = None) -> list[Result]:
    page = _get("https://html.duckduckgo.com/html/", client, q=query, kl="ru-ru").text
    results = parse_duckduckgo(page, limit)
    if not results and ("anomaly" in page or "captcha" in page.lower()):
        raise SourceError("показал проверку «вы не робот»")
    return results


# --- Bing --------------------------------------------------------------------

_BING_BLOCK = re.compile(r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>', re.S)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a([^>]*)>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _bing_target(href: str) -> str:
    """Ссылки Bing бывают обёрнуты: /ck/a?...&u=a1<base64 адреса>."""
    href = html.unescape(href)
    parsed = urlparse(href)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
            try:
                return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
    return href


def parse_bing(page: str, limit: int) -> list[Result]:
    results: list[Result] = []
    for block in _BING_BLOCK.findall(page):
        link = _BING_LINK.search(block)
        href = _HREF.search(link.group(1)) if link else None
        if not href:
            continue
        url = _bing_target(href.group(1))
        if not url.startswith("http"):
            continue
        snippet = _BING_SNIPPET.search(block)
        results.append({
            "title": _text(link.group(2)),
            "url": url,
            "snippet": _text(snippet.group(1)) if snippet else "",
            "published": "",
            "engine": "Bing",
        })
        if len(results) >= limit:
            break
    return results


def bing(query: str, limit: int, client: httpx.Client | None = None) -> list[Result]:
    page = _get("https://www.bing.com/search", client, q=query, setlang="ru", cc="RU").text
    return parse_bing(page, limit)


# --- Google News RSS --------------------------------------------------------


def parse_news_rss(xml_text: str, limit: int) -> list[Result]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise SourceError("лента новостей не разобралась") from exc
    results: list[Result] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = item.find("source")
        publisher = (source.text or "").strip() if source is not None else ""
        published = ""
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).date().isoformat()
            except (TypeError, ValueError):
                published = raw_date
        if not (title and link):
            continue
        results.append({
            "title": title,
            "url": link,
            "snippet": f"Новость, источник: {publisher}" if publisher else "Новость",
            "published": published,
            "engine": "Google News",
        })
        if len(results) >= limit:
            break
    return results


def google_news(query: str, limit: int, client: httpx.Client | None = None) -> list[Result]:
    response = _get("https://news.google.com/rss/search", client, q=query, hl="ru", gl="RU", ceid="RU:ru")
    return parse_news_rss(response.text, limit)


# --- Википедия ---------------------------------------------------------------


def wikipedia(query: str, limit: int, client: httpx.Client | None = None) -> list[Result]:
    response = _get(
        "https://ru.wikipedia.org/w/api.php", client,
        action="query", list="search", srsearch=query, srlimit=str(limit), format="json",
    )
    try:
        items = response.json().get("query", {}).get("search", [])
    except ValueError as exc:
        raise SourceError("ответ не JSON") from exc
    return [
        {
            "title": item.get("title", ""),
            "url": "https://ru.wikipedia.org/wiki/" + quote(item.get("title", "").replace(" ", "_")),
            "snippet": _text(item.get("snippet", "")),
            "published": (item.get("timestamp") or "")[:10],
            "engine": "Википедия",
        }
        for item in items
        if item.get("title")
    ]


# --- сборка ------------------------------------------------------------------

Source = Callable[[str, int, "httpx.Client | None"], list[Result]]


def search(query: str, limit: int, client: httpx.Client | None = None) -> tuple[list[Result], list[str]]:
    """Веб-результаты плюс свежие новости. Возвращает (результаты, отказы источников)."""
    failures: list[str] = []
    web: list[Result] = []

    def attempt(name: str, source: Source, count: int) -> list[Result]:
        try:
            return source(query, count, client)
        except SourceError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — чужая разметка не должна ронять поиск
            logger.warning("Источник %s сломался: %s", name, exc)
            failures.append(f"{name}: {exc.__class__.__name__}")
        return []

    for name, source in (("DuckDuckGo", duckduckgo), ("Bing", bing)):
        web = attempt(name, source, limit)
        if web:
            break

    news = attempt("Google News", google_news, max(2, limit // 2))
    if not web and not news:
        web = attempt("Википедия", wikipedia, limit)

    seen: set[str] = set()
    combined: list[Result] = []
    for item in [*web, *news]:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        combined.append(item)
    return combined, failures
