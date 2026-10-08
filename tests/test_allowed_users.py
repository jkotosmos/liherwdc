"""Белый список Telegram: устойчивый разбор и понятная диагностика.

Случай из жизни: ID добавили в панели, а /check по-прежнему «3 чел.» —
нужно видеть, откуда взят список и что из него отброшено.
"""

from __future__ import annotations

from app import config


def test_separators_are_forgiving() -> None:
    ids, rejected = config.parse_telegram_ids("1107365044, 8058569481;198704816 159893732\n42")
    assert ids == {1107365044, 8058569481, 198704816, 159893732, 42} and rejected == []


def test_garbage_is_reported_not_silently_dropped() -> None:
    ids, rejected = config.parse_telegram_ids('1107365044,"159893732",@kirill,ID:5')
    assert ids == {1107365044, 159893732}
    assert rejected == ["@kirill", "ID:5"]


def test_source_panel_vs_file(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_ALLOWED_USERS=1,2,3\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILES_LOADED", [env])
    monkeypatch.setattr(config, "_ENV_BEFORE_FILES", frozenset())
    assert config.env_source("TELEGRAM_ALLOWED_USERS") == f"файл {env}"
    monkeypatch.setattr(config, "_ENV_BEFORE_FILES", frozenset({"TELEGRAM_ALLOWED_USERS"}))
    assert "панель Amvera" in config.env_source("TELEGRAM_ALLOWED_USERS")


def test_spaces_inside_one_id_are_joined() -> None:
    ids, rejected = config.parse_telegram_ids("1107365044, 159 893 732")
    assert ids == {1107365044, 159893732} and rejected == []


def test_trailing_garbage_rejects_the_whole_entry() -> None:
    ids, rejected = config.parse_telegram_ids("1107365044,159893732x")
    assert ids == {1107365044} and rejected == ["159893732x"]
