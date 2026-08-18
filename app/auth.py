"""Простая парольная защита интерфейса.

Зачем это нужно именно здесь: на сервере лежит OAuth-токен пользователя
Google. Открытый доступ к чату означал бы открытый доступ к его Диску и
календарю, поэтому публичный запуск без пароля запрещён (см. run.py).

Схема: пароль проверяется один раз, дальше браузер носит подписанную
HMAC куку. Сервер не хранит список сессий — подпись самодостаточна.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from hashlib import sha256
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "operon_session"
_SECRET_FILE = "session_secret.txt"

# Защита от подбора пароля: адрес -> (число попыток, время разблокировки).
_MAX_ATTEMPTS = 7
_LOCKOUT_SECONDS = 300
_attempts: dict[str, tuple[int, float]] = {}


def _secret() -> bytes:
    """Ключ подписи: из окружения либо сгенерированный и сохранённый на диске."""
    if settings.session_secret:
        return settings.session_secret.encode("utf-8")

    path: Path = settings.data_dir / _SECRET_FILE
    if path.exists():
        return path.read_bytes().strip()

    generated = secrets.token_hex(32).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generated)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover — зависит от файловой системы
        pass
    logger.info("Сгенерирован ключ подписи сессий: %s", path)
    return generated


def issue_token() -> str:
    """Токен вида «время.подпись» — проверяется без хранения состояния."""
    issued = str(int(time.time()))
    signature = hmac.new(_secret(), issued.encode("ascii"), sha256).hexdigest()
    return f"{issued}.{signature}"


def token_is_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued, _, signature = token.partition(".")
    if not issued.isdigit():
        return False

    expected = hmac.new(_secret(), issued.encode("ascii"), sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False

    age_hours = (time.time() - int(issued)) / 3600
    return 0 <= age_hours <= settings.auth_ttl_hours


def password_is_valid(candidate: str) -> bool:
    # Сравниваем байты, а не строки: compare_digest не принимает символы вне
    # ASCII, и пароль с кириллицей иначе приводил бы к ошибке сервера.
    # compare_digest — чтобы время ответа не зависело от длины совпадения.
    return hmac.compare_digest(
        (candidate or "").encode("utf-8"), settings.access_password.encode("utf-8")
    )


def throttle_state(client: str) -> int:
    """Сколько секунд осталось до разблокировки; 0 — попытки разрешены."""
    attempts, until = _attempts.get(client, (0, 0.0))
    if attempts >= _MAX_ATTEMPTS and time.time() < until:
        return int(until - time.time())
    return 0


def register_failure(client: str) -> None:
    attempts, _ = _attempts.get(client, (0, 0.0))
    attempts += 1
    _attempts[client] = (attempts, time.time() + _LOCKOUT_SECONDS)
    if attempts >= _MAX_ATTEMPTS:
        logger.warning("Адрес %s заблокирован после %s неудачных попыток входа", client, attempts)


def register_success(client: str) -> None:
    _attempts.pop(client, None)
