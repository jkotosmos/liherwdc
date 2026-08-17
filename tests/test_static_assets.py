"""Проверки статики, которые дешевле поймать тестом, чем в браузере."""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
ASSETS = ["index.html", "app.js", "styles.css"]


@pytest.mark.parametrize("name", ASSETS)
def test_asset_exists_and_is_valid_utf8(name: str) -> None:
    (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ASSETS)
def test_no_control_bytes(name: str) -> None:
    """Нулевые байты ломают JS-регулярки молча — браузер не сообщит об ошибке."""
    raw = (STATIC / name).read_bytes()
    assert b"\x00" not in raw


def test_markdown_renderer_handles_escaped_markup() -> None:
    """Цитаты и блоки кода разбираются уже после экранирования HTML."""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    # «>» к моменту разбора превращён в &gt;, поэтому шаблон должен искать именно его.
    assert "/^\\s*&gt;\\s?/" in source
    assert "/^ FENCE(\\d+) $/" in source


def test_page_references_its_assets() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/app.js" in html
    assert "/static/styles.css" in html
