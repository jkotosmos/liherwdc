"""Хранение OAuth-токена Google в зашифрованном виде.

Токен даёт доступ к Диску и календарю владельца, а лежит он на постоянном
диске сервера. Поэтому при заданном OPERON_TOKEN_KEY файл шифруется:
доступ к диску Amvera сам по себе не даёт доступа к Google-аккаунту.

Ключ шифрования выводится из парольной фразы функцией scrypt со случайной
солью, которая хранится рядом. Без парольной фразы токен сохраняется как
обычный JSON — так работает локальная разработка, но на сервере это
сопровождается предупреждением в логе.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

logger = logging.getLogger(__name__)

SALT_FILE = "token_salt.bin"
ENCRYPTED_SUFFIX = ".enc"


class TokenDecryptionError(Exception):
    """Файл есть, но расшифровать его текущим ключом нельзя."""


def _passphrase() -> str:
    return os.getenv("OPERON_TOKEN_KEY", "").strip()


def encryption_enabled() -> bool:
    return bool(_passphrase())


def _salt() -> bytes:
    path: Path = settings.credentials_dir / SALT_FILE
    if path.exists():
        return path.read_bytes()
    salt = os.urandom(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(salt)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover — зависит от файловой системы
        pass
    return salt


def _cipher() -> Fernet:
    # scrypt с рекомендованными параметрами: подбор парольной фразы дорог.
    key = base64.urlsafe_b64encode(
        __import__("hashlib").scrypt(
            _passphrase().encode("utf-8"), salt=_salt(), n=2**14, r=8, p=1, dklen=32
        )
    )
    return Fernet(key)


def _encrypted_path() -> Path:
    return settings.google_token_path.with_suffix(
        settings.google_token_path.suffix + ENCRYPTED_SUFFIX
    )


def token_exists() -> bool:
    return _encrypted_path().exists() or settings.google_token_path.exists()


def save_token(payload: str) -> Path:
    """Сохраняет токен, шифруя его при наличии парольной фразы."""
    settings.credentials_dir.mkdir(parents=True, exist_ok=True)

    if encryption_enabled():
        path = _encrypted_path()
        path.write_bytes(_cipher().encrypt(payload.encode("utf-8")))
        # Открытую копию не оставляем.
        settings.google_token_path.unlink(missing_ok=True)
    else:
        path = settings.google_token_path
        path.write_text(payload, encoding="utf-8")
        logger.warning(
            "Токен Google сохранён без шифрования. На сервере задайте "
            "OPERON_TOKEN_KEY — иначе доступ к диску даёт доступ к вашему Google."
        )

    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    return path


def load_token() -> str | None:
    """Читает токен независимо от того, зашифрован он или нет."""
    encrypted = _encrypted_path()
    if encrypted.exists():
        if not encryption_enabled():
            raise TokenDecryptionError(
                "Токен зашифрован, но OPERON_TOKEN_KEY не задан. Укажите ту же "
                "парольную фразу, что использовалась при сохранении."
            )
        try:
            return _cipher().decrypt(encrypted.read_bytes()).decode("utf-8")
        except InvalidToken as exc:
            raise TokenDecryptionError(
                "Не удалось расшифровать токен Google: парольная фраза "
                "OPERON_TOKEN_KEY не подходит либо потеряна соль "
                f"({settings.credentials_dir / SALT_FILE}). Пройдите авторизацию заново."
            ) from exc

    if settings.google_token_path.exists():
        payload = settings.google_token_path.read_text(encoding="utf-8")
        # Ключ появился позже — переносим открытый токен под шифрование.
        if encryption_enabled():
            logger.info("Найден незашифрованный токен — перешифровываю")
            save_token(payload)
        return payload

    return None


def describe() -> dict[str, object]:
    return {
        "exists": token_exists(),
        "encrypted": _encrypted_path().exists(),
        "encryption_configured": encryption_enabled(),
    }
