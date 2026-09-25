"""Изоляция тестов: подменяем каталоги данных до импорта приложения."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Каталоги задаются ДО импорта app.config — иначе настройки уже прочитаны.
_TMP = Path(tempfile.mkdtemp(prefix="operon-tests-"))
os.environ.setdefault("OPERON_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("OPERON_CREDENTIALS_DIR", str(_TMP / "credentials"))
os.environ.setdefault("OPERON_KB_DIR", str(_TMP / "kb"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
# Тесты не ходят в интернет: бесплатный поиск включается только там, где
# его источники подменены заглушками.
os.environ.setdefault("OPERON_SEARCH_PROVIDER", "none")

(_TMP / "kb").mkdir(parents=True, exist_ok=True)

DEMO_KB = ROOT / "examples" / "demo_kb"

import pytest  # noqa: E402


@pytest.fixture
def tmp_data_dir() -> Path:
    return _TMP / "data"
