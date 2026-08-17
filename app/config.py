"""Конфигурация агента OPERON. Все значения читаются из окружения/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- Claude ---
    model: str = os.getenv("OPERON_MODEL", "claude-opus-5")
    max_tokens: int = _int("OPERON_MAX_TOKENS", 64000)
    effort: str = os.getenv("OPERON_EFFORT", "high")  # low|medium|high|xhigh|max
    max_tool_iterations: int = _int("OPERON_MAX_TOOL_ITERATIONS", 24)

    # --- Организация ---
    org_name: str = os.getenv("OPERON_ORG_NAME", "OPERON")
    timezone_name: str = os.getenv("OPERON_TIMEZONE", "Europe/Moscow")

    # --- Данные ---
    kb_dir: Path = Path(os.getenv("OPERON_KB_DIR", str(BASE_DIR / "knowledge_base")))
    data_dir: Path = Path(os.getenv("OPERON_DATA_DIR", str(BASE_DIR / "data")))
    credentials_dir: Path = Path(
        os.getenv("OPERON_CREDENTIALS_DIR", str(BASE_DIR / "credentials"))
    )

    # --- Интернет ---
    web_search_enabled: bool = _bool("OPERON_WEB_SEARCH", True)
    web_search_max_uses: int = _int("OPERON_WEB_SEARCH_MAX_USES", 8)

    # --- Сервер ---
    host: str = os.getenv("OPERON_HOST", "127.0.0.1")
    port: int = _int("OPERON_PORT", 8000)

    # --- Ограничения ---
    max_history_messages: int = _int("OPERON_MAX_HISTORY", 200)
    session_ttl_minutes: int = _int("OPERON_SESSION_TTL_MINUTES", 720)

    google_scopes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s
            for s in os.getenv(
                "OPERON_GOOGLE_SCOPES",
                # drive.readonly — чтение любых доступных пользователю файлов;
                # drive.file — запись только в файлы, созданные этим приложением;
                # calendar   — чтение и запись событий (запись только с подтверждением).
                "https://www.googleapis.com/auth/drive.readonly "
                "https://www.googleapis.com/auth/drive.file "
                "https://www.googleapis.com/auth/calendar",
            ).split()
            if s
        )
    )

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def google_token_path(self) -> Path:
        return self.credentials_dir / "google_token.json"

    @property
    def google_client_secret_path(self) -> Path:
        env_path = os.getenv("OPERON_GOOGLE_CLIENT_SECRET")
        if env_path:
            return Path(env_path)
        return self.credentials_dir / "client_secret.json"

    @property
    def tasks_path(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"


settings = Settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.credentials_dir.mkdir(parents=True, exist_ok=True)
