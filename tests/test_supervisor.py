"""Надзор за ботом: упавший поток обязан подняться, а healthcheck — знать правду."""

from __future__ import annotations

import threading
import time

import pytest

from app.telegram.supervisor import TelegramSupervisor


class CrashingBot:
    """Падает заданное число раз, потом работает до остановки."""

    def __init__(self, crashes: int, exc: Exception | None = None) -> None:
        self.crashes = crashes
        self.exc = exc or RuntimeError("соединение разорвано")
        self.runs = 0
        self._stop = threading.Event()
        self.alive = threading.Event()

    def run(self) -> None:
        self.runs += 1
        if self.runs <= self.crashes:
            raise self.exc
        self.alive.set()
        self._stop.wait(10)

    def stop(self) -> None:
        self._stop.set()


class SilentlyReturningBot:
    """Не падает, а просто возвращается — самый коварный случай."""

    def __init__(self) -> None:
        self.runs = 0
        self._stop = threading.Event()
        self.restarted = threading.Event()

    def run(self) -> None:
        self.runs += 1
        if self.runs > 1:
            self.restarted.set()
            self._stop.wait(10)

    def stop(self) -> None:
        self._stop.set()


def run_supervised(bot, *, first_retry: float = 0.01) -> TelegramSupervisor:
    supervisor = TelegramSupervisor(lambda: bot, first_retry=first_retry, max_retry=0.05)
    thread = threading.Thread(target=supervisor.run_forever, daemon=True)
    thread.start()
    return supervisor


class TestRestart:
    def test_crashed_bot_is_restarted(self) -> None:
        bot = CrashingBot(crashes=2)
        supervisor = run_supervised(bot)
        try:
            assert bot.alive.wait(5), "бот так и не поднялся после падений"
            assert bot.runs == 3
            assert supervisor.status()["restarts"] == 2
            assert "соединение разорвано" in supervisor.status()["last_error"]
        finally:
            supervisor.stop()

    def test_silently_returning_bot_is_restarted_too(self) -> None:
        """Раньше поток просто возвращался — и система оставалась без бота."""
        bot = SilentlyReturningBot()
        supervisor = run_supervised(bot)
        try:
            assert bot.restarted.wait(5)
            assert "завершился сам" in supervisor.status()["last_error"]
        finally:
            supervisor.stop()

    def test_backoff_grows_between_attempts(self) -> None:
        bot = CrashingBot(crashes=3)
        started = time.monotonic()
        supervisor = TelegramSupervisor(lambda: bot, first_retry=0.05, max_retry=0.4)
        thread = threading.Thread(target=supervisor.run_forever, daemon=True)
        thread.start()
        try:
            assert bot.alive.wait(5)
            # Паузы 0.05 + 0.1 + 0.2 — не мгновенный цикл перезапусков.
            assert time.monotonic() - started >= 0.3
        finally:
            supervisor.stop()

    def test_stop_ends_the_loop(self) -> None:
        bot = CrashingBot(crashes=0)
        supervisor = run_supervised(bot)
        assert bot.alive.wait(5)
        supervisor.stop()
        time.sleep(0.1)
        assert supervisor.status()["running"] is False


class TestStatus:
    def test_running_bot_is_healthy(self) -> None:
        bot = CrashingBot(crashes=0)
        supervisor = run_supervised(bot)
        try:
            assert bot.alive.wait(5)
            state = supervisor.status()
            assert state["running"] is True
            assert state["healthy"] is True
            assert state["running_since"]
        finally:
            supervisor.stop()

    def test_disabled_bot_is_healthy_but_not_running(self, monkeypatch) -> None:
        """Не настроенный бот — осознанное решение, а не сбой."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        supervisor = TelegramSupervisor(lambda: None)
        assert supervisor.start() is False
        state = supervisor.status()
        assert state["running"] is False
        assert state["healthy"] is True
        assert state["enabled"] is False

    def test_configured_but_dead_bot_is_unhealthy(self) -> None:
        """Ровно тот случай, ради которого healthcheck и меняли."""
        supervisor = TelegramSupervisor(lambda: None)
        supervisor._state.update({"enabled": True, "running": False})
        assert supervisor.status()["healthy"] is False

    def test_start_refuses_without_whitelist(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        from dataclasses import replace

        from app.config import settings
        from app.telegram import supervisor as supervisor_module

        monkeypatch.setattr(
            supervisor_module, "_default_factory", lambda: pytest.fail("бот не должен стартовать")
        )
        # Токен читается из окружения через свежие настройки.
        monkeypatch.setattr("app.config.settings", replace(settings, telegram_token="123:abc"))
        supervisor = TelegramSupervisor()
        assert supervisor.start() is False
        assert supervisor.status()["healthy"] is True
