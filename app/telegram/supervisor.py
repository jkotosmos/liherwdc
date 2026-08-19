"""Надзор за потоком бота: падение не должно оставлять систему без бота.

Бот живёт фоновым потоком одного процесса (на Amvera процесс один). Поток,
упавший на необработанном исключении или на недоступности Telegram, умирает
молча: веб-интерфейс продолжает отвечать, healthcheck зелёный, а бот не
отвечает никому. Поэтому поток запускается не напрямую, а под надзором,
который перезапускает его с нарастающей паузой и хранит причину падения —
её видно и в логе, и в /api/health.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

FIRST_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0
# Проработал дольше — значит запуск был удачным, и следующий сбой не связан
# с предыдущим: пауза начинается заново с минимальной.
STABLE_RUN_SECONDS = 120.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_factory() -> Any:
    from .bot import TelegramBot

    return TelegramBot()


class TelegramSupervisor:
    """Держит бота запущенным и честно отвечает, запущен ли он."""

    def __init__(
        self,
        factory: Callable[[], Any] | None = None,
        *,
        first_retry: float = FIRST_RETRY_SECONDS,
        max_retry: float = MAX_RETRY_SECONDS,
    ) -> None:
        self._factory = factory or _default_factory
        self._first_retry = first_retry
        self._max_retry = max_retry
        self._thread: threading.Thread | None = None
        self._bot: Any = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "starts": 0,
            "restarts": 0,
            "last_error": "",
            "last_error_at": "",
            "running_since": "",
        }

    # --- управление ---------------------------------------------------------

    def start(self) -> bool:
        """Запускает надзор. False — бот выключен настройками, это не сбой."""
        from ..config import settings

        if not settings.telegram_token:
            self._note(enabled=False, last_error="Не задан TELEGRAM_BOT_TOKEN")
            logger.info("Telegram не настроен — работает только веб-интерфейс")
            return False
        if not settings.telegram_allowed_users:
            self._note(enabled=False, last_error="Не задан TELEGRAM_ALLOWED_USERS")
            logger.error(
                "Telegram-бот НЕ запущен: не задан TELEGRAM_ALLOWED_USERS. "
                "Без белого списка доступ к вашему Google-аккаунту получил бы любой."
            )
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._state["enabled"] = True
            self._thread = threading.Thread(target=self.run_forever, name="telegram", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            bot, thread = self._bot, self._thread
        if bot is not None:
            try:
                bot.stop()
            except Exception:  # noqa: BLE001 — остановка не должна ронять выключение
                logger.exception("Ошибка при остановке бота")
        if thread is not None:
            thread.join(timeout=timeout)

    # --- цикл надзора -------------------------------------------------------

    def run_forever(self) -> None:
        """Держит бота живым, пока не позвали stop(). Блокирует поток."""
        delay = self._first_retry

        while not self._stop.is_set():
            started = time.monotonic()
            reason = ""
            try:
                bot = self._factory()
                with self._lock:
                    self._bot = bot
                    self._state["running"] = True
                    self._state["starts"] += 1
                    self._state["running_since"] = _now()
                bot.run()
                reason = "поток бота завершился сам"
            except Exception as exc:  # noqa: BLE001 — ради этого надзор и существует
                reason = f"{exc.__class__.__name__}: {exc}"
                logger.exception("Telegram-бот упал")
            finally:
                with self._lock:
                    self._bot = None
                    self._state["running"] = False
                    self._state["running_since"] = ""

            if self._stop.is_set():
                break

            uptime = time.monotonic() - started
            if uptime > STABLE_RUN_SECONDS:
                delay = self._first_retry

            with self._lock:
                self._state["restarts"] += 1
                self._state["last_error"] = reason
                self._state["last_error_at"] = _now()

            logger.error(
                "Telegram-бот остановлен (%s), проработал %.0f с — перезапуск через %.0f с",
                reason,
                uptime,
                delay,
            )
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self._max_retry)

        logger.info("Надзор за Telegram-ботом завершён")

    # --- состояние ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        from ..config import settings

        with self._lock:
            state = dict(self._state)
        state["configured"] = bool(settings.telegram_token)
        state["allowed_users"] = len(settings.telegram_allowed_users)
        # Здоровым считается либо работающий бот, либо сознательно выключенный.
        state["healthy"] = bool(state["running"]) or not state["enabled"]
        return state

    def _note(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
            if "last_error" in fields and fields["last_error"]:
                self._state["last_error_at"] = _now()


supervisor = TelegramSupervisor()
