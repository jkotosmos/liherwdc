"""Вход в Mini App: проверка подписи Telegram вместо пароля.

Telegram передаёт Mini App строку initData, подписанную ключом, выведенным
из токена бота. Подделать её без токена нельзя, поэтому достаточно проверить
подпись, свежесть и что пользователь есть в белом списке — тот же список,
что у бота. Пароль в Mini App не нужен.

Алгоритм — https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

from ..config import settings

# initData живёт, пока открыт Mini App; сутки — с большим запасом.
MAX_AGE_SECONDS = 24 * 3600


class InitDataError(Exception):
    """Подпись не сошлась, данные устарели или пользователь не допущен."""


def _expected_hash(fields: dict[str, str], token: str) -> str:
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def validate(init_data: str, token: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Возвращает пользователя Telegram или бросает InitDataError."""
    token = token if token is not None else settings.telegram_token
    if not token:
        raise InitDataError("Бот не настроен: нет TELEGRAM_BOT_TOKEN.")

    fields = dict(parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", "")
    if not received:
        raise InitDataError("Нет подписи Telegram. Откройте приложение из бота.")

    # Поле signature (подпись для сторонних сервисов) по разным редакциям
    # документации то входит в проверяемую строку, то нет. Обе проверки
    # опираются на токен бота, так что принимать любую из них безопасно.
    candidates = [_expected_hash(fields, token)]
    if "signature" in fields:
        without = {k: v for k, v in fields.items() if k != "signature"}
        candidates.append(_expected_hash(without, token))
    if not any(hmac.compare_digest(c, received) for c in candidates):
        raise InitDataError("Подпись Telegram не сошлась.")

    auth_date = int(fields.get("auth_date") or 0)
    current = now if now is not None else time.time()
    if not auth_date or current - auth_date > MAX_AGE_SECONDS:
        raise InitDataError("Данные входа устарели. Закройте и откройте приложение заново.")

    try:
        user = json.loads(fields.get("user") or "{}")
    except ValueError as exc:
        raise InitDataError("Telegram не передал пользователя.") from exc
    user_id = user.get("id")
    if not isinstance(user_id, int) or user_id not in settings.telegram_allowed_users:
        raise InitDataError("Доступ закрыт: вас нет в списке TELEGRAM_ALLOWED_USERS.")
    return user


def miniapp_url() -> str:
    """Адрес Mini App. Telegram открывает только https."""
    base = settings.public_url
    if not base.startswith("https://"):
        return ""
    return base.rstrip("/") + "/miniapp"
