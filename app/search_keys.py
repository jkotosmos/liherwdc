"""Ключи поискового API: список, проверка на порчу, сроки жизни поставщиков.

Отдельный модуль без побочных эффектов: его импортирует и инструмент поиска,
и самопроверка, а импорт app.tools регистрирует все инструменты сразу.
"""

from __future__ import annotations

import os

# Google закрыл Custom Search JSON API для новых клиентов и отключает его
# 1 января 2027 года. Поддержка оставлена для тех, у кого ключ уже есть.
GOOGLE_CSE_SHUTDOWN = (
    "Google Custom Search JSON API закрыт для новых клиентов и отключается "
    "1 января 2027 года — переходите на Tavily (TAVILY_API_KEY)."
)

SEARCH_KEY_VARIABLES = (
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "SERPER_API_KEY",
    "GOOGLE_CSE_KEY",
    "GOOGLE_CSE_ID",
)


def damaged_keys() -> list[tuple[str, str]]:
    """Ключи, повреждённые при копировании. Возвращает (имя, причина).

    Ключ, в котором появились не-ASCII знаки, скопирован неудачно: терминалы
    и консоли подменяют часть символов точками, мессенджеры — дефис тире.
    Сервис на такой ключ отвечает «API key not valid», и человек идёт
    перевыпускать исправный ключ вместо того, чтобы перевставить его.
    """
    damaged = []
    for name in SEARCH_KEY_VARIABLES:
        value = (os.getenv(name) or "").strip()
        if not value:
            continue
        if not value.isascii():
            bad = "".join(sorted({c for c in value if not c.isascii()}))
            damaged.append((name, f"содержит посторонние знаки «{bad}» — испорчен при копировании"))
        elif " " in value:
            damaged.append((name, "содержит пробел внутри значения"))
    return damaged
