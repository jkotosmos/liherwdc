"""Интернет-поиск, не зависящий от провайдера модели.

Серверные инструменты поиска Anthropic работают только при прямом доступе к
её API. Через сторонний шлюз («AI-роутер») их нет, поэтому требование ТЗ
«использовать интернет» закрывается собственными инструментами: поиск через
внешний поисковый API и чтение страницы по ссылке.

Провайдер определяется по тому, какой ключ задан в окружении.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import httpx

from ..config import settings
from .base import ToolError, ToolSpec, registry

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_PAGE_CHARS = 40_000

NOT_CONFIGURED = (
    "Интернет-поиск не настроен: нет ключа поискового API. Сообщи пользователю, "
    "что внешние данные сейчас недоступны, и отвечай только по внутренним "
    "источникам. Не выдумывай рыночные данные и не ссылайся на память. "
    "Чтобы включить поиск, задайте одну из переменных: TAVILY_API_KEY, "
    "BRAVE_API_KEY, SERPER_API_KEY или GOOGLE_CSE_KEY + GOOGLE_CSE_ID."
)


def _search_provider() -> str:
    explicit = (os.getenv("OPERON_SEARCH_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    if os.getenv("BRAVE_API_KEY"):
        return "brave"
    if os.getenv("SERPER_API_KEY"):
        return "serper"
    if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_ID"):
        return "google"
    return ""


def search_is_configured() -> bool:
    return bool(_search_provider())


def _normalize(title: str, url: str, snippet: str, published: str = "") -> dict[str, str]:
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "snippet": re.sub(r"\s+", " ", snippet or "").strip()[:600],
        "published": published or "",
    }


# --- поисковые провайдеры --------------------------------------------------


def _tavily(query: str, limit: int) -> list[dict[str, str]]:
    response = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": os.environ["TAVILY_API_KEY"],
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return [
        _normalize(item.get("title", ""), item.get("url", ""), item.get("content", ""),
                   item.get("published_date", ""))
        for item in response.json().get("results", [])
    ]


def _brave(query: str, limit: int) -> list[dict[str, str]]:
    response = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={
            "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])
    return [
        _normalize(item.get("title", ""), item.get("url", ""),
                   item.get("description", ""), item.get("age", ""))
        for item in results
    ]


def _serper(query: str, limit: int) -> list[dict[str, str]]:
    response = httpx.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": limit},
        headers={"X-API-KEY": os.environ["SERPER_API_KEY"]},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return [
        _normalize(item.get("title", ""), item.get("link", ""),
                   item.get("snippet", ""), item.get("date", ""))
        for item in response.json().get("organic", [])[:limit]
    ]


def _google_cse(query: str, limit: int) -> list[dict[str, str]]:
    response = httpx.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": os.environ["GOOGLE_CSE_KEY"],
            "cx": os.environ["GOOGLE_CSE_ID"],
            "q": query,
            "num": min(limit, 10),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return [
        _normalize(item.get("title", ""), item.get("link", ""), item.get("snippet", ""))
        for item in response.json().get("items", [])
    ]


PROVIDERS = {"tavily": _tavily, "brave": _brave, "serper": _serper, "google": _google_cse}


def _describe_search_error(provider: str, exc: httpx.HTTPStatusError) -> str:
    """Объясняет отказ поисковика словами самого поисковика.

    Прежний текст на любой код советовал «проверьте ключ и лимиты» — и был
    вреден в самом частом случае: у Google ключ верный, но не включён Custom
    Search API. Человек шёл перевыпускать ключ и не находил ничего.
    Поставщик обычно объясняет причину сам; наше дело — не заслонить её.
    """
    code = exc.response.status_code
    detail = ""
    try:
        payload = exc.response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            detail = detail or str(payload.get("message") or "")
    except ValueError:
        detail = exc.response.text[:300]

    if detail:
        return f"Поисковый сервис {provider} ответил {code}: {detail[:400]}"

    hint = {
        401: "ключ не принят — проверьте его.",
        403: "доступ запрещён: ключ, права или отключённый API.",
        429: "исчерпан лимит запросов тарифа.",
    }.get(code, "проверьте ключ и лимиты тарифа.")
    return f"Поисковый сервис {provider} ответил {code}: {hint}"


# --- инструменты -----------------------------------------------------------


def _internet_search(tool_input: dict[str, Any]) -> Any:
    query = (tool_input.get("query") or "").strip()
    if not query:
        raise ToolError("Не указан поисковый запрос (query).")

    provider = _search_provider()
    if not provider:
        return {"status": "not_configured", "hint": NOT_CONFIGURED}
    if provider not in PROVIDERS:
        raise ToolError(f"Неизвестный поисковый провайдер «{provider}». Доступно: {sorted(PROVIDERS)}")

    limit = min(max(int(tool_input.get("max_results") or 6), 1), 15)
    try:
        results = PROVIDERS[provider](query, limit)
    except KeyError as exc:
        raise ToolError(f"Для провайдера «{provider}» не задан ключ: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise ToolError(_describe_search_error(provider, exc)) from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Не удалось обратиться к поисковому сервису {provider}: {exc}") from exc

    retrieved = datetime.now(settings.tz).strftime("%Y-%m-%d %H:%M")
    if not results:
        return {
            "status": "not_found",
            "query": query,
            "retrieved_at": retrieved,
            "hint": "Поиск ничего не вернул. Скажи об этом прямо, не заменяй результат догадкой.",
        }
    return {
        "status": "ok",
        "source_type": "internet",
        "provider": provider,
        "query": query,
        "retrieved_at": retrieved,
        "note": (
            "Это ВНЕШНИЕ данные. В ответе указывай название источника, ссылку и дату "
            f"получения ({retrieved}); не смешивай их с внутренними данными OPERON."
        ),
        "results": results,
    }


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")


def _open_url(tool_input: dict[str, Any]) -> Any:
    url = (tool_input.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("Нужен полный адрес страницы, начинающийся с http:// или https://")

    try:
        response = httpx.get(
            url,
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": f"OperonAssistant/1.0 (+{settings.public_url or 'local'})"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"Страница ответила {exc.response.status_code}: {url}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Не удалось открыть страницу {url}: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        text = _HTML_RE.sub(" ", _TAG_RE.sub(" ", response.text))
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    elif "text" in content_type or "json" in content_type:
        text = response.text
    else:
        raise ToolError(
            f"Тип содержимого {content_type or 'неизвестен'} — текст извлечь нельзя. "
            "Сообщи об этом пользователю."
        )

    retrieved = datetime.now(settings.tz).strftime("%Y-%m-%d %H:%M")
    return {
        "status": "ok",
        "source_type": "internet",
        "url": str(response.url),
        "retrieved_at": retrieved,
        "truncated": len(text) > MAX_PAGE_CHARS,
        "citation": f"{response.url} (получено {retrieved})",
        "content": text.strip()[:MAX_PAGE_CHARS],
    }


def register_web_tools() -> None:
    """Регистрируется, только если поиск настроен либо явно разрешено чтение страниц."""
    registry.register(
        ToolSpec(
            name="internet_search",
            description=(
                "Ищет актуальные данные в интернете: рынок, конкуренты, законодательство, "
                "технологии, курсы и цены. Возвращает названия источников, ссылки и дату "
                "получения. Это ВНЕШНИЕ данные — в ответе всегда отделяй их от внутренних "
                "сведений OPERON и указывай ссылку с датой актуальности. Используй, когда "
                "вопрос выходит за пределы внутренних документов компании."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос."},
                    "max_results": {
                        "type": "integer",
                        "description": "Сколько результатов вернуть (1–15), по умолчанию 6.",
                    },
                },
                "required": ["query"],
            },
            handler=_internet_search,
            activity="Ищу в интернете",
        )
    )

    registry.register(
        ToolSpec(
            name="open_url",
            description=(
                "Открывает страницу по ссылке и возвращает её текст. Используй после "
                "internet_search, когда из краткого описания недостаточно данных, либо "
                "когда пользователь сам прислал ссылку. Всегда указывай ссылку и дату "
                "получения как источник."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Полный адрес страницы."}
                },
                "required": ["url"],
            },
            handler=_open_url,
            activity="Открываю страницу",
        )
    )
