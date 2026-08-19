"""Подготовка ответа агента к отправке в Telegram.

Telegram понимает узкий набор HTML: <b>, <i>, <code>, <pre>, <a>. Списков,
заголовков и таблиц нет, поэтому Markdown модели приводится к тому, что
Telegram отобразит без ошибок разметки.
"""

from __future__ import annotations

import re

MAX_MESSAGE = 4096
SAFE_CHUNK = 3900  # запас на закрывающие теги при разрезании


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def to_telegram_html(markdown: str) -> str:
    """Переводит ответ модели в HTML, который Telegram примет."""
    source = escape(markdown).replace("\r\n", "\n")

    # Блоки кода не размечаем — выносим и возвращаем в конце.
    fences: list[str] = []

    def stash(match: re.Match[str]) -> str:
        fences.append(match.group(1).rstrip("\n"))
        return f"\x00FENCE{len(fences) - 1}\x00"

    source = re.sub(r"```[^\n]*\n(.*?)```", stash, source, flags=re.DOTALL)

    lines: list[str] = []
    for line in source.split("\n"):
        stripped = line.strip()

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            lines.append(f"<b>{_inline(heading.group(2).strip())}</b>")
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            lines.append("—" * 12)
            continue

        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bullet:
            lines.append(f"• {_inline(bullet.group(1))}")
            continue

        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if numbered:
            lines.append(f"{numbered.group(1)}. {_inline(numbered.group(2))}")
            continue

        quote = re.match(r"^&gt;\s?(.*)$", stripped)
        if quote:
            lines.append(f"<i>{_inline(quote.group(1))}</i>")
            continue

        # Строка таблицы: убираем разделители, оставляем читаемый вид.
        if stripped.startswith("|") and stripped.endswith("|"):
            if re.match(r"^\|[\s:|-]+\|$", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            lines.append(" · ".join(_inline(c) for c in cells if c))
            continue

        lines.append(_inline(line))

    result = "\n".join(lines)
    for index, code in enumerate(fences):
        result = result.replace(f"\x00FENCE{index}\x00", f"<pre>{code}</pre>")
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def split_message(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Режет длинный ответ по границам абзацев и строк."""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks
