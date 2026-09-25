"""Какая модель отвечает сейчас: выбор из Mini App сильнее OPERON_MODEL.

Выбор лежит на постоянном диске и переживает передеплой. Сбросить — выбрать
модель заново или удалить data/model.json: тогда снова действует OPERON_MODEL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import settings
from .storage import read_json, write_json


def _path():
    return settings.data_dir / "model.json"


def current_model(conf: Any = None) -> str:
    """Выбранная модель; без выбора — OPERON_MODEL из переданных настроек."""
    conf = conf or settings
    stored = read_json(conf.data_dir / "model.json", {})
    if isinstance(stored, dict) and stored.get("model"):
        return str(stored["model"])
    return conf.model


def choice() -> dict[str, Any]:
    stored = read_json(_path(), {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "model": current_model(),
        "default": settings.model,
        "changed_by": stored.get("changed_by", ""),
        "changed_at": stored.get("changed_at", ""),
    }


def set_model(model_id: str, changed_by: str = "") -> None:
    write_json(
        _path(),
        {
            "model": model_id,
            "changed_by": changed_by,
            "changed_at": datetime.now(settings.tz).isoformat(timespec="seconds"),
        },
    )
