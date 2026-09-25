"""Значения из .env приходят с мусором по краям, и это ломает не сразу.

Файл настроек правят руками. Пробел в конце строки не виден глазом, но
уходит прямо в адрес запроса: токен Telegram с хвостовым пробелом даёт
«404 Not Found», и человек начинает искать причину в токене, а не в пробеле.
Кавычки ставят по привычке из shell-скриптов — с тем же результатом.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from app.config import Settings, _str

ROOT = Path(__file__).resolve().parent.parent


class TestStringCleaning:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("значение ", "значение"),
            ("  значение", "значение"),
            ("\tзначение\n", "значение"),
            ('"значение"', "значение"),
            ("'значение'", "значение"),
            (' "значение" ', "значение"),
            ("", ""),
        ],
    )
    def test_edges_are_trimmed(self, monkeypatch, raw: str, expected: str) -> None:
        monkeypatch.setenv("OPERON_TEST_VALUE", raw)
        assert _str("OPERON_TEST_VALUE") == expected

    def test_inner_spaces_survive(self, monkeypatch) -> None:
        """Обрезаем только края: «ООО Ромашка» — законное название организации."""
        monkeypatch.setenv("OPERON_TEST_VALUE", "  ООО Ромашка  ")
        assert _str("OPERON_TEST_VALUE") == "ООО Ромашка"

    def test_absent_variable_gives_default(self, monkeypatch) -> None:
        monkeypatch.delenv("OPERON_TEST_VALUE", raising=False)
        assert _str("OPERON_TEST_VALUE", "по умолчанию") == "по умолчанию"


class TestTokensAreUsable:
    """Именно этот случай приехал из настоящего файла заказчика."""

    @staticmethod
    def _in_fresh_process(env: dict[str, str], expression: str) -> str:
        """Запускает Python заново с заданным окружением.

        Часть настроек — поля со значением по умолчанию: они вычисляются один
        раз при импорте модуля, как и происходит при настоящем запуске, когда
        .env уже прочитан. Подменить переменную после импорта нельзя, поэтому
        проверяем так, как оно работает в жизни.
        """
        import os
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", f"from app.config import settings; print({expression})"],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_telegram_token_with_trailing_space(self) -> None:
        token = self._in_fresh_process(
            {"TELEGRAM_BOT_TOKEN": "1234567890:AAFakeTokenForTests "},
            "repr(settings.telegram_token)",
        )
        assert token == "'1234567890:AAFakeTokenForTests'"
        assert " " not in token.strip("'"), "пробел ушёл бы в адрес запроса и дал 404"

    def test_api_key_in_quotes(self, monkeypatch) -> None:
        monkeypatch.setenv("ROUTERAI_API_KEY", '"sk-abc123"')
        monkeypatch.delenv("OPERON_LLM_API_KEY", raising=False)
        assert Settings(provider="routerai").api_key == "sk-abc123"

    def test_model_name_with_tab(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_MODEL", "\topenai/gpt-4.1-mini\n")
        assert Settings().model == "openai/gpt-4.1-mini"

    def test_google_client_id_with_space(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com ")
        assert Settings().google_client_id.endswith(".com")

    def test_public_url_trailing_slash_and_space(self) -> None:
        url = self._in_fresh_process(
            {"OPERON_PUBLIC_URL": " https://operon.amvera.io/ "}, "settings.public_url"
        )
        assert url == "https://operon.amvera.io"


class TestTimezoneNeverThrows:
    """Запасной вариант не должен зависеть от того, из-за чего отказал основной.

    На Windows нет системной базы IANA. Прежний запасной вариант возвращал
    ZoneInfo("UTC") — и падал сам, потому что «UTC» ищется в той же базе.
    Исключение вылетало из свойства settings.tz и роняло любой вызов
    datetime.now(...): создание поручения, сохранение протокола, сводку.
    """

    @staticmethod
    def _without_tz_database(monkeypatch):
        import sys
        import zoneinfo

        monkeypatch.setattr(zoneinfo, "TZPATH", ())
        zoneinfo.reset_tzpath([])
        monkeypatch.setitem(sys.modules, "tzdata", None)
        monkeypatch.setattr(
            zoneinfo, "ZoneInfo", _always_missing, raising=False
        )

    def test_unknown_timezone_falls_back_without_raising(self) -> None:
        from datetime import datetime

        conf = Settings(timezone_name="Такого/Пояса/Нет")
        tz = conf.tz  # не должно бросить

        assert datetime.now(tz).utcoffset() is not None

    def test_datetime_works_with_the_fallback(self) -> None:
        """Именно здесь падало: datetime.now(settings.tz) в task_create."""
        from datetime import datetime

        conf = Settings(timezone_name="Ерунда")
        stamp = datetime.now(conf.tz).isoformat(timespec="seconds")
        assert stamp.endswith("+00:00"), "запасной вариант — UTC"

    def test_valid_timezone_is_honoured(self) -> None:
        conf = Settings(timezone_name="Europe/Moscow")
        assert "Moscow" in str(conf.tz)

    def test_tzdata_is_declared(self) -> None:
        """На Windows базы часовых поясов нет в системе, её ставят пакетом."""
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "tzdata" in text


def _always_missing(key):
    from zoneinfo import ZoneInfoNotFoundError

    raise ZoneInfoNotFoundError(f"No time zone found with key {key}")
