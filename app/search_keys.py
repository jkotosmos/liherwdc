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


# Какие переменные нужны каждому поставщику. Порядок — порядок выбора.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "tavily": ("TAVILY_API_KEY",),
    "brave": ("BRAVE_API_KEY",),
    "serper": ("SERPER_API_KEY",),
    "google": ("GOOGLE_CSE_KEY", "GOOGLE_CSE_ID"),
}


def key_problem(name: str) -> str:
    """Почему значение переменной испорчено, или пустая строка, если всё в порядке.

    Ключ, в котором появились не-ASCII знаки, скопирован неудачно: терминалы
    и консоли подменяют часть символов точками, мессенджеры — дефис тире.
    Сервис на такой ключ отвечает «API key not valid», и человек идёт
    перевыпускать исправный ключ вместо того, чтобы перевставить его.
    """
    value = (os.getenv(name) or "").strip()
    if not value:
        return ""
    if not value.isascii():
        bad = "".join(sorted({c for c in value if not c.isascii()}))
        return f"содержит посторонние знаки «{bad}» — испорчен при копировании"
    if " " in value:
        return "содержит пробел внутри значения"
    return ""


def damaged_keys(names: tuple[str, ...] = SEARCH_KEY_VARIABLES) -> list[tuple[str, str]]:
    """Испорченные ключи среди заданных. Возвращает (имя, причина)."""
    return [(name, why) for name in names if (why := key_problem(name))]


def provider_usable(provider: str) -> bool:
    """Все ключи поставщика заданы и ни один не испорчен.

    Испорченная строка, оставшаяся от старой настройки, не должна выбирать
    своего поставщика: иначе она выключила бы рабочий поиск.
    """
    names = PROVIDER_KEYS.get(provider, ())
    return bool(names) and all(os.getenv(n, "").strip() for n in names) and not damaged_keys(names)
