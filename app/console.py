"""Вывод в консоль, который не падает на Windows.

Русская консоль Windows по умолчанию работает в cp866, и `print("✓")` там
не портит вид, а бросает UnicodeEncodeError — то есть проверка обрывается
до первой полезной строки. Диагностический инструмент, падающий на выводе
диагностики, бесполезен вдвойне.

Порядок такой: пробуем перевести поток на UTF-8; если не вышло — проверяем,
что кодировка консоли вообще берёт нужные знаки, и при отказе переходим на
ASCII. Внешний вид дешевле работоспособности.
"""

from __future__ import annotations

import sys

# Знаки, которых нет в однобайтовых кодировках Windows.
FANCY = {"ok": "  ✓  ", "fail": "  ✗  ", "rule": "—", "arrow": "→"}
PLAIN = {"ok": " OK  ", "fail": " FAIL", "rule": "-", "arrow": "->"}


def setup() -> dict[str, str]:
    """Готовит поток вывода и возвращает набор безопасных знаков."""
    stream = sys.stdout

    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            # errors="replace" — последняя линия обороны: даже если консоль
            # не примет знак, программа продолжит работу, а не оборвётся.
            reconfigure(encoding="utf-8", errors="replace")
            return FANCY
        except (ValueError, OSError):  # pragma: no cover — зависит от окружения
            pass

    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "".join(FANCY.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return PLAIN
    return FANCY
