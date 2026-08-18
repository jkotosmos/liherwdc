"""Проверка подключения к шлюзу модели.

    python -m app.probe

Делает минимальные запросы и отвечает на три вопроса: принимает ли шлюз ключ,
на каком протоколе он говорит и умеет ли вызывать инструменты. Вызов
инструментов критичен: без него агент не сможет ни искать в базе знаний, ни
спрашивать подтверждение — то есть не будет работать вообще.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx

from .config import settings

PROBE_TOOL = {
    "name": "ping",
    "description": "Проверочный инструмент. Вызови его с параметром ok=true.",
    "input_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
}
PROMPT = "Вызови инструмент ping с параметром ok=true. Ничего не пиши в ответ."


def _try_openai(base_url: str, api_key: str, model: str) -> tuple[bool, bool, str]:
    """Возвращает (шлюз ответил, инструменты работают, пояснение)."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": PROBE_TOOL["name"],
                    "description": PROBE_TOOL["description"],
                    "parameters": PROBE_TOOL["input_schema"],
                },
            }
        ],
    }
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(90.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        return False, False, f"сеть: {exc}"

    if response.status_code >= 400:
        return False, False, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        message = response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError):
        return True, False, "ответ не похож на Chat Completions"
    return True, bool(message.get("tool_calls")), "ответ получен"


def _try_anthropic(base_url: str, api_key: str, model: str) -> tuple[bool, bool, str]:
    url = base_url.rstrip("/") + "/messages"
    body = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [PROBE_TOOL],
    }
    try:
        response = httpx.post(
            url,
            json=body,
            headers={
                "x-api-key": api_key,
                "authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(90.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        return False, False, f"сеть: {exc}"

    if response.status_code >= 400:
        return False, False, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        content = response.json()["content"]
    except (ValueError, KeyError):
        return True, False, "ответ не похож на Messages API"
    return True, any(b.get("type") == "tool_use" for b in content), "ответ получен"


def main() -> int:
    base_url = settings.base_url or "https://api.anthropic.com/v1"
    print("Проверка доступа к модели")
    print(f"  провайдер : {settings.provider}")
    print(f"  адрес     : {base_url}")
    print(f"  модель    : {settings.model or '(не задана)'}")
    print(f"  протокол  : {settings.llm_protocol} (настроенный)")
    print(f"  ключ      : {'задан' if settings.api_key else 'НЕ ЗАДАН'}\n")

    if not settings.api_key:
        print("Нет ключа. Задайте OPERON_LLM_API_KEY.", file=sys.stderr)
        return 1
    if not settings.model:
        print("Не задано имя модели. Задайте OPERON_MODEL.", file=sys.stderr)
        return 1

    checks: list[tuple[str, Any]] = [
        ("openai", _try_openai),
        ("anthropic", _try_anthropic),
    ]
    # Настроенный протокол проверяем первым.
    checks.sort(key=lambda item: item[0] != settings.llm_protocol)

    working: list[str] = []
    for name, probe in checks:
        reachable, tools_ok, detail = probe(base_url, settings.api_key, settings.model)
        mark = "OK " if reachable else "нет"
        print(f"[{mark}] протокол {name}: {detail}")
        if reachable:
            print(f"       вызов инструментов: {'работает' if tools_ok else 'НЕ РАБОТАЕТ'}")
            if tools_ok:
                working.append(name)

    print()
    if not working:
        print("Ни один протокол не ответил. Проверьте адрес, ключ и имя модели.")
        print("Адрес обычно заканчивается на /v1 — уточните в документации провайдера.")
        return 2

    chosen = working[0]
    print(f"Рабочий протокол: {chosen}")
    if chosen != settings.llm_protocol:
        print(f"Задайте в окружении: OPERON_LLM_PROTOCOL={chosen}")
    else:
        print("Настройка уже верна — можно запускать агента.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
