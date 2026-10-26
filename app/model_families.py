"""Какие модели предлагать в выборе: DeepSeek, GPT и Claude.

Каталог шлюза очень большой, и искать в нём нужную модель неудобно. Людям
нужны три семейства; остальные модели в выборе не показываются. Задать их
можно по-прежнему вручную: OPERON_MODEL принимает любую модель каталога.
"""

from __future__ import annotations

FAMILIES: tuple[tuple[str, str], ...] = (
    ("deepseek", "DeepSeek"),
    ("gpt", "GPT (OpenAI)"),
    ("claude", "Claude (Anthropic)"),
)
_LABELS = dict(FAMILIES)


def family(model_id: str) -> str | None:
    """Семейство модели по её идентификатору шлюза; None — не из трёх."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    if "deepseek" in mid:
        return "deepseek"
    if "gpt" in mid and ("openai/" in mid or mid.startswith("gpt")):
        return "gpt"
    if "claude" in mid and ("anthropic/" in mid or mid.startswith("claude")):
        return "claude"
    return None


def is_allowed(model_id: str) -> bool:
    return family(model_id) is not None


def label(key: str | None) -> str:
    return _LABELS.get(key or "", "")
