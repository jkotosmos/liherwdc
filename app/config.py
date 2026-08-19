"""Конфигурация агента OPERON. Все значения читаются из окружения/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# Провайдеры доступа к модели. Anthropic-совместимый протокол поддерживают оба,
# поэтому код агента одинаков — различаются адрес, ключ и набор возможностей.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# RouterAI — российский шлюз, совместим с API OpenAI.
ROUTERAI_BASE_URL = "https://routerai.ru/api/v1"


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


def _detect_provider() -> str:
    explicit = (os.getenv("OPERON_LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("ROUTERAI_API_KEY"):
        return "routerai"
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    # Свой адрес шлюза + свой ключ = сторонний провайдер («AI-роутер»).
    if os.getenv("OPERON_LLM_BASE_URL") and os.getenv("OPERON_LLM_API_KEY"):
        return "custom"
    return "anthropic"


def _default_model(provider: str) -> str:
    if provider == "openrouter":
        # В OpenRouter идентификаторы моделей включают вендора.
        return "anthropic/claude-opus-4.1"
    if provider in {"custom", "routerai"}:
        # У шлюза свой каталог — имя модели обязан задать пользователь.
        # Посмотреть доступные: python -m app.probe
        return ""
    return "claude-opus-5"


@dataclass(frozen=True)
class Settings:
    # --- Провайдер модели ---
    provider: str = _detect_provider()
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = _int("OPERON_MAX_TOKENS", 32000)
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
    web_search_max_uses: int = _int("OPERON_WEB_SEARCH_MAX_USES", 8)

    # --- Сервер ---
    host: str = os.getenv("OPERON_HOST", "127.0.0.1")
    port: int = _int("PORT", _int("OPERON_PORT", 8000))
    public_url: str = os.getenv("OPERON_PUBLIC_URL", "").rstrip("/")

    # --- Доступ ---
    access_password: str = os.getenv("OPERON_ACCESS_PASSWORD", "")
    session_secret: str = os.getenv("OPERON_SESSION_SECRET", "")
    auth_ttl_hours: int = _int("OPERON_AUTH_TTL_HOURS", 168)

    # --- Telegram ---
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_poll_timeout: int = _int("TELEGRAM_POLL_TIMEOUT", 30)

    # --- Ограничения ---
    max_history_messages: int = _int("OPERON_MAX_HISTORY", 200)
    session_ttl_minutes: int = _int("OPERON_SESSION_TTL_MINUTES", 720)

    google_scopes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s
            for s in os.getenv(
                "OPERON_GOOGLE_SCOPES",
                # Минимальный набор: каждый лишний scope — лишний риск.
                # drive.readonly   — чтение доступных пользователю файлов;
                # calendar.events  — чтение и запись событий (запись — только
                #                    после подтверждения пользователя).
                # Для создания файлов на Диске (протоколы, КП) добавьте
                # https://www.googleapis.com/auth/drive.file
                "https://www.googleapis.com/auth/drive.readonly "
                "https://www.googleapis.com/auth/calendar.events",
            ).split()
            if s
        )
    )

    def __post_init__(self) -> None:
        # dataclass заморожен, поэтому вычисляемые поля выставляем через object.
        # Значение, переданное явно, имеет приоритет над окружением — иначе
        # Settings(base_url=...) молча игнорировался бы (важно для тестов
        # и для программной сборки конфигурации).
        provider = self.provider or "anthropic"
        object.__setattr__(self, "provider", provider)

        if not self.model:
            object.__setattr__(
                self, "model", os.getenv("OPERON_MODEL") or _default_model(provider)
            )
        if not self.api_key:
            object.__setattr__(
                self,
                "api_key",
                os.getenv("OPERON_LLM_API_KEY")
                or os.getenv("ROUTERAI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or "",
            )
        if not self.base_url:
            default_base = {
                "openrouter": OPENROUTER_BASE_URL,
                "routerai": ROUTERAI_BASE_URL,
            }.get(provider, "")
            # ANTHROPIC_BASE_URL учитываем только для самой Anthropic: иначе
            # оставшаяся в окружении переменная увела бы запросы стороннего
            # шлюза на чужой адрес.
            anthropic_base = os.getenv("ANTHROPIC_BASE_URL", "") if provider == "anthropic" else ""
            object.__setattr__(
                self,
                "base_url",
                (
                    os.getenv("OPERON_LLM_BASE_URL") or default_base or anthropic_base
                ).rstrip("/"),
            )
        else:
            object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    # --- возможности провайдера -------------------------------------------
    # Серверные инструменты поиска, параметр effort и кэширование промпта —
    # расширения Anthropic. Через сторонний шлюз они могут быть не приняты,
    # поэтому по умолчанию включаются только на прямом доступе к Anthropic.

    @property
    def is_anthropic_direct(self) -> bool:
        return self.provider == "anthropic"

    @property
    def llm_protocol(self) -> str:
        """Формат запросов: «anthropic» (/v1/messages) или «openai» (/chat/completions).

        Явно задаётся через OPERON_LLM_PROTOCOL. По умолчанию: Anthropic — для
        прямого доступа и OpenRouter (у него есть Anthropic-совместимый режим),
        OpenAI-совместимый — для прочих шлюзов, где это де-факто стандарт.
        """
        explicit = (os.getenv("OPERON_LLM_PROTOCOL") or "").strip().lower()
        if explicit in {"anthropic", "openai"}:
            return explicit
        return "anthropic" if self.provider in {"anthropic", "openrouter"} else "openai"

    @property
    def models_url(self) -> str:
        """Адрес каталога моделей провайдера (для диагностики)."""
        if not self.base_url:
            return ""
        return self.base_url.rstrip("/") + "/models"

    @property
    def web_search_enabled(self) -> bool:
        return _bool("OPERON_WEB_SEARCH", self.is_anthropic_direct)

    @property
    def effort_enabled(self) -> bool:
        return _bool("OPERON_USE_EFFORT", self.is_anthropic_direct)

    @property
    def prompt_cache_enabled(self) -> bool:
        return _bool("OPERON_USE_PROMPT_CACHE", self.is_anthropic_direct)

    @property
    def extra_headers(self) -> dict[str, str]:
        """OpenRouter просит указывать источник трафика — влияет на лимиты."""
        if self.provider != "openrouter":
            return {}  # у остальных шлюзов обязательных заголовков нет
        return {
            "HTTP-Referer": self.public_url or "https://amvera.ru",
            "X-Title": f"Assistant {self.org_name}",
        }

    @property
    def auth_required(self) -> bool:
        return bool(self.access_password)

    @property
    def telegram_allowed_users(self) -> frozenset[int]:
        """Белый список Telegram ID. Единственная граница доступа к боту.

        Бот работает с Google-аккаунтом владельца, поэтому любое сообщение
        не из этого списка игнорируется молча — без списка бот не стартует.
        """
        raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        ids = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return frozenset(ids)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_allowed_users)

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
