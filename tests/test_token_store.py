"""Шифрование OAuth-токена Google на диске."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

TOKEN = '{"refresh_token": "очень-секретное-значение", "client_id": "x"}'


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """Свежий модуль хранилища с временным каталогом учётных данных."""

    def build(passphrase: str | None):
        monkeypatch.setenv("OPERON_CREDENTIALS_DIR", str(tmp_path))
        if passphrase is None:
            monkeypatch.delenv("OPERON_TOKEN_KEY", raising=False)
        else:
            monkeypatch.setenv("OPERON_TOKEN_KEY", passphrase)
        import app.config

        importlib.reload(app.config)
        import app.integrations.token_store as ts

        return importlib.reload(ts)

    return build


class TestEncrypted:
    def test_secret_is_not_readable_on_disk(self, store, tmp_path: Path) -> None:
        """Главное свойство: доступ к диску сервера не даёт доступа к Google."""
        ts = store("парольная фраза")
        ts.save_token(TOKEN)

        files = list(tmp_path.glob("*"))
        blob = b"".join(f.read_bytes() for f in files if f.is_file())
        assert b"refresh_token" not in blob
        assert "очень-секретное-значение".encode() not in blob

    def test_roundtrip(self, store) -> None:
        ts = store("парольная фраза")
        ts.save_token(TOKEN)
        assert ts.load_token() == TOKEN

    def test_wrong_passphrase_is_rejected(self, store) -> None:
        ts = store("правильная")
        ts.save_token(TOKEN)

        ts = store("неправильная")
        with pytest.raises(ts.TokenDecryptionError, match="OPERON_TOKEN_KEY"):
            ts.load_token()

    def test_missing_passphrase_is_explained(self, store) -> None:
        ts = store("фраза")
        ts.save_token(TOKEN)

        ts = store(None)
        with pytest.raises(ts.TokenDecryptionError, match="OPERON_TOKEN_KEY не задан"):
            ts.load_token()

    def test_plain_copy_is_removed(self, store, tmp_path: Path) -> None:
        ts = store(None)
        ts.save_token(TOKEN)
        assert (tmp_path / "google_token.json").exists()

        ts = store("фраза")
        ts.load_token()  # перешифровывает найденный открытый токен
        assert not (tmp_path / "google_token.json").exists()
        assert (tmp_path / "google_token.json.enc").exists()

    def test_describe_reports_state(self, store) -> None:
        ts = store("фраза")
        ts.save_token(TOKEN)
        assert ts.describe() == {
            "exists": True,
            "encrypted": True,
            "encryption_configured": True,
        }


class TestPlain:
    def test_works_without_passphrase(self, store) -> None:
        """Локальная разработка не должна требовать настройки шифрования."""
        ts = store(None)
        ts.save_token(TOKEN)
        assert ts.load_token() == TOKEN
        assert ts.describe()["encrypted"] is False

    def test_absent_token_returns_none(self, store) -> None:
        ts = store(None)
        assert ts.load_token() is None
        assert ts.token_exists() is False
