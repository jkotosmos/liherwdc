"""Чей Google: у каждого пользователя свой доступ.

ТЗ требует работать с документами и календарём «только в рамках прав
пользователя». Пока пользователь один, токен тоже один. Со вторым человеком в
белом списке общий токен означал бы, что он видит чужой Диск и календарь.

Поэтому токен выбирается по тому, кто спрашивает:

* владелец — первый ID в TELEGRAM_ALLOWED_USERS — и веб-чат по паролю
  пользуются основным токеном (google_token.json), как и раньше;
* любой другой пользователь — своим (google_token_<id>.json) после своего /auth.
  Пока он его не прошёл, Google для него не подключён: чужой токен не
  подставляется никогда.

Текущего пользователя несёт contextvar: бот выставляет его на время обработки
сообщения, агент — на время вызова инструмента.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[str] = ContextVar("operon_google_account", default="")


def current() -> str:
    return _current.get()


@contextmanager
def use(account: object) -> Iterator[None]:
    token = _current.set(str(account or "").strip())
    try:
        yield
    finally:
        _current.reset(token)


def owner_id() -> str:
    """Первый ID из TELEGRAM_ALLOWED_USERS в том порядке, как он записан."""
    raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            return part
    return ""


def resolve(account: str | None = None) -> str:
    """Ключ токена: пусто — основной (владелец и веб-чат), иначе ID пользователя."""
    key = current() if account is None else str(account or "").strip()
    if not key or key == owner_id():
        return ""
    return "".join(ch for ch in key if ch.isalnum() or ch == "-")
