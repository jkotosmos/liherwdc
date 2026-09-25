"""Каталог моделей и баланс шлюза: то, что видит пользователь в Mini App.

RouterAI повторяет устройство API OpenRouter: каталог — GET /models, сведения
о ключе (расход, лимит) — GET /key, баланс — GET /credits. Формы ответов
разбираются терпимо: поле может лежать в обёртке ``data`` или без неё, цена —
строкой или числом. Чего в ответе нет, того не показываем — цифры не
придумываются.

Проверить, что именно отдаёт ваш шлюз: ``python -m app.routerai``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

from .config import settings

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CATALOG_TTL_SECONDS = 600

# Шлюзы с API в духе OpenRouter. У прочих каталога и баланса может не быть.
SUPPORTED_PROVIDERS = {"routerai", "openrouter"}

_lock = threading.Lock()
_catalog: tuple[float, list[dict[str, Any]]] | None = None


class BillingError(Exception):
    """Шлюз не ответил или ответил не тем — текст пригоден для показа."""


def available() -> bool:
    return settings.provider in SUPPORTED_PROVIDERS and bool(settings.api_key and settings.base_url)


def currency() -> str:
    """Валюта счёта. RouterAI считает в рублях, OpenRouter — в долларах."""
    explicit = (os.getenv("OPERON_BILLING_CURRENCY") or "").strip()
    if explicit:
        return explicit
    return "$" if settings.provider == "openrouter" else "₽"


def _get(path: str, client: httpx.Client | None = None) -> Any:
    if not available():
        raise BillingError("Каталог и баланс доступны только для RouterAI и OpenRouter.")
    url = settings.base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {settings.api_key}", **settings.extra_headers}
    try:
        if client is not None:
            response = client.get(url, headers=headers)
        else:
            response = httpx.get(url, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise BillingError(f"Шлюз недоступен ({path}): {exc}") from exc
    if response.status_code == 401:
        raise BillingError("Шлюз не принял ключ (401). Проверьте ROUTERAI_API_KEY.")
    if response.status_code >= 400:
        raise BillingError(f"Шлюз ответил {response.status_code} на {path}.")
    try:
        return response.json()
    except ValueError as exc:
        raise BillingError(f"Шлюз вернул не JSON на {path}.") from exc


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def per_million(value: Any) -> float | None:
    """Цена за 1 млн токенов.

    OpenRouter отдаёт цену за один токен (0.000003), витрины — за миллион
    (62). Цена за миллион меньше сотой доли рубля не встречается, а цена за
    токен больше сотой — тоже, поэтому порог разделяет единицы надёжно.
    """
    number = _number(value)
    if number is None or number < 0:
        return None
    return number * 1_000_000 if number < 0.01 else number


def normalize_model(raw: dict[str, Any]) -> dict[str, Any]:
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    parameters = raw.get("supported_parameters")
    tools: bool | None = None
    if isinstance(parameters, list):
        tools = "tools" in parameters or "tool_choice" in parameters
    top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or raw.get("id") or ""),
        "context_length": raw.get("context_length") or top.get("context_length"),
        "max_output": top.get("max_completion_tokens"),
        "price_in": per_million(pricing.get("prompt", pricing.get("input"))),
        "price_out": per_million(pricing.get("completion", pricing.get("output"))),
        "tools": tools,
    }


def list_models(force: bool = False, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Каталог моделей, кэш на 10 минут: он меняется редко, а Mini App открывают часто."""
    global _catalog
    with _lock:
        if not force and _catalog and time.monotonic() - _catalog[0] < CATALOG_TTL_SECONDS:
            return _catalog[1]
    data = _unwrap(_get("/models", client))
    if not isinstance(data, list):
        raise BillingError("Каталог моделей пришёл в неожиданном виде.")
    models = [normalize_model(item) for item in data if isinstance(item, dict) and item.get("id")]
    models.sort(key=lambda m: m["name"].lower())
    with _lock:
        _catalog = (time.monotonic(), models)
    return models


def find_model(model_id: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    for model in list_models(client=client):
        if model["id"] == model_id:
            return model
    return None


def billing(client: httpx.Client | None = None) -> dict[str, Any]:
    """Баланс и расход. Каждое поле — только если шлюз его прислал."""
    result: dict[str, Any] = {"currency": currency(), "errors": []}

    try:
        credits = _unwrap(_get("/credits", client))
        if isinstance(credits, dict):
            total = _number(credits.get("total_credits"))
            used = _number(credits.get("total_usage"))
            balance = _number(credits.get("balance"))
            if balance is None and total is not None and used is not None:
                balance = total - used
            result.update(
                {k: v for k, v in {"balance": balance, "total_credits": total, "total_usage": used}.items() if v is not None}
            )
    except BillingError as exc:
        result["errors"].append(str(exc))

    try:
        key = _unwrap(_get("/key", client))
        if isinstance(key, dict):
            for field in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining"):
                value = _number(key.get(field))
                if value is not None:
                    result[f"key_{field}"] = value
            if key.get("limit_reset"):
                result["key_limit_reset"] = str(key["limit_reset"])
    except BillingError as exc:
        result["errors"].append(str(exc))

    # Если /credits недоступен, остаток лимита ключа — лучшее, что есть.
    if "balance" not in result and "key_limit_remaining" in result:
        result["balance"] = result["key_limit_remaining"]
        result["balance_source"] = "лимит ключа"
    return result


def reset_cache() -> None:
    global _catalog
    with _lock:
        _catalog = None


def main() -> int:  # pragma: no cover — ручная диагностика
    """Показывает сырые ответы шлюза: как они выглядят на вашем ключе."""
    from . import console

    console.setup()
    if not available():
        print("Задайте ROUTERAI_API_KEY (или OPENROUTER_API_KEY).")
        return 1
    for path in ("/key", "/credits"):
        print(f"\n=== GET {path}")
        try:
            print(json.dumps(_get(path), ensure_ascii=False, indent=2)[:3000])
        except BillingError as exc:
            print(f"ошибка: {exc}")
    print("\n=== GET /models (первые 2)")
    try:
        data = _unwrap(_get("/models"))
        print(json.dumps(data[:2] if isinstance(data, list) else data, ensure_ascii=False, indent=2)[:4000])
        print(f"\nВсего моделей: {len(data) if isinstance(data, list) else '?'}")
    except BillingError as exc:
        print(f"ошибка: {exc}")
    print("\n=== Как это увидит Mini App")
    print(json.dumps(billing(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
