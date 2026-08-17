"""Реестр инструментов агента.

Импорт модулей ниже регистрирует инструменты в общем реестре — порядок
импорта не влияет на порядок в запросе к API (реестр сортирует по имени,
чтобы не ломать кэш промпта).
"""

from . import calendar, drive, knowledge, tasks  # noqa: F401 — импорт ради регистрации
from .base import IntegrationUnavailable, Preview, ToolError, ToolRegistry, ToolSpec, registry

__all__ = [
    "IntegrationUnavailable",
    "Preview",
    "ToolError",
    "ToolRegistry",
    "ToolSpec",
    "registry",
]
