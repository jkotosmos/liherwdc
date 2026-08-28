"""Реестр инструментов агента.

Импорт модулей ниже регистрирует инструменты в общем реестре — порядок
импорта не влияет на порядок в запросе к API (реестр сортирует по имени,
чтобы не ломать кэш промпта).
"""

from ..config import settings
from . import calendar, drive, knowledge, kpi, protocol, tasks, web  # noqa: F401 — импорт ради регистрации
from .base import IntegrationUnavailable, Preview, ToolError, ToolRegistry, ToolSpec, registry

# Серверный поиск Anthropic доступен только при прямом доступе к её API.
# В остальных случаях (сторонний шлюз) подключаем собственные инструменты,
# иначе требование ТЗ «использовать интернет» осталось бы невыполненным.
if not settings.web_search_enabled:
    web.register_web_tools()

__all__ = [
    "IntegrationUnavailable",
    "Preview",
    "ToolError",
    "ToolRegistry",
    "ToolSpec",
    "registry",
]
