"""Простое надёжное хранилище состояния в JSON-файлах.

Запись атомарная (через временный файл + rename), доступ сериализован
блокировкой — этого достаточно для одного процесса uvicorn.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]


def read_json(path: Path, default: Any) -> Any:
    with _lock_for(path):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Повреждённый файл не должен ронять сервис: отводим его в сторону.
            broken = path.with_suffix(path.suffix + ".broken")
            try:
                path.replace(broken)
            except OSError:
                pass
            return default


def write_json(path: Path, payload: Any) -> None:
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def update_json(path: Path, default: Any, mutate) -> Any:
    """Читает, применяет mutate(data) и записывает результат под одной блокировкой."""
    with _lock_for(path):
        data = read_json(path, default)
        result = mutate(data)
        write_json(path, data)
        return result
