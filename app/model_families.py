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


# --- короткий список для выбора ---------------------------------------------
#
# В каталоге десятки версий одной модели (DeepSeek V3, V3 0324, V3.1…), а
# человеку нужен выбор из нескольких актуальных. Список собирается правилом,
# а не перечнем ID: номер версии берётся из идентификатора, и когда в
# каталоге появится новая версия, она сама вытеснит старую.

import re

# Сколько моделей показывать в семействе и сколько — одной линейки
# (например, не больше двух «mini» у GPT).
LIMITS = {"deepseek": 2, "gpt": 5, "claude": 4}
PER_LINE = {"deepseek": 2, "gpt": 2, "claude": 1}

# Служебные и узкие варианты: бесплатные копии с лимитами (":free"),
# эксперименты, «дистилляты», модели для доказательств, распознавания и т. п.
_JUNK = re.compile(r":|distill|prover|ocr|-exp|preview|speciale|math|coder")

_LINE_ORDER = {"claude": {"opus": 0, "sonnet": 1, "haiku": 2}}

_NUM = r"(\d+(?:\.\d+)?)"
_GPT = re.compile(rf"^openai/gpt-{_NUM}(?:-(mini|nano))?$")
_CLAUDE = re.compile(rf"^anthropic/claude-(?:(opus|sonnet|haiku)-{_NUM}|{_NUM}-(opus|sonnet|haiku))$")
_DEEPSEEK = re.compile(r"^deepseek/deepseek-(?:chat|v\d)")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _rank(model_id: str) -> tuple[str, tuple[int, ...], str] | None:
    """(семейство, версия, линейка) или None — модель в короткий список не идёт."""
    mid = (model_id or "").strip().lower()
    if _JUNK.search(mid):
        return None
    match = _GPT.match(mid)
    if match:
        return "gpt", _version(match.group(1)), match.group(2) or "main"
    match = _CLAUDE.match(mid)
    if match:
        line = match.group(1) or match.group(4)
        return "claude", _version(match.group(2) or match.group(3)), line
    if _DEEPSEEK.match(mid) and "r1" not in mid:
        # deepseek-chat — это V3; в остальных номер стоит после «v»:
        # deepseek-chat-v3.1, deepseek-v3.2, deepseek-v4-flash.
        found = re.findall(r"v(\d+(?:\.\d+)?)", mid.split("/", 1)[1])
        version = max((_version(v) for v in found), default=(3,))
        # Даты сборок (v3-0324) — старые снимки той же версии.
        if re.search(r"-\d{4}$", mid):
            return None
        return "deepseek", version, mid
    return None


def curate(models: list[dict], current: str = "") -> list[dict]:
    """Несколько актуальных моделей каждого семейства, новые — первыми.

    Текущая модель остаётся в списке, даже если она старая: иначе не видно,
    что выбрано. Если правило не узнало ни одной модели семейства (шлюз
    переименовал модели), показываем семейство целиком — лучше длинный
    список, чем пустой.
    """
    picked: list[dict] = []
    for key, _label in FAMILIES:
        members = [m for m in models if family(m["id"]) == key]
        ranked = [(r, m) for m in members if (r := _rank(m["id"])) and r[0] == key]
        if not ranked:
            picked.extend(members)
            continue
        if key == "deepseek":
            # У DeepSeek важна только последняя версия (их может быть две:
            # например, быстрая и полная).
            newest = max(r[1] for r, _ in ranked)
            ranked = [(r, m) for r, m in ranked if r[1] == newest]
        ranked.sort(key=lambda pair: (pair[0][1], -len(pair[1]["id"])), reverse=True)
        lines: dict[str, int] = {}
        chosen: list[dict] = []
        for (_fam, _ver, line), model in ranked:
            if len(chosen) >= LIMITS[key]:
                break
            if lines.get(line, 0) >= PER_LINE[key] and key != "deepseek":
                continue
            lines[line] = lines.get(line, 0) + 1
            chosen.append(model)
        if key == "claude":
            # По одной модели каждой линейки: Opus, Sonnet, Haiku.
            chosen.sort(key=lambda m: _LINE_ORDER["claude"].get(_rank(m["id"])[2], 9))
        picked.extend(chosen)
    if current and all(m["id"] != current for m in picked):
        picked.extend(m for m in models if m["id"] == current)
    return picked
