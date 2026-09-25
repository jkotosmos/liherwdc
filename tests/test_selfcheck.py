"""Самопроверка: она должна отличать «не запустится» от «работает хуже».

Смысл инструмента в приговоре, а не в списке галочек. Если он назовёт
отсутствие ключа поиска блокирующим, человек будет чинить не то; если
промолчит про нерабочий вызов инструментов — запустит бесполезного бота.
"""

from __future__ import annotations

from dataclasses import replace


from app import selfcheck
from app.config import settings
from app.selfcheck import FAIL, OK, SKIP, WARN, Check


class TestVerdict:
    """Код возврата: 2 — не запустится, 1 — частично, 0 — годен."""

    def _run(self, monkeypatch, checks: list[Check]) -> int:
        for name in ("check_settings", "check_model", "check_telegram",
                     "check_google", "check_search", "check_knowledge_base"):
            monkeypatch.setattr(selfcheck, name, lambda: [])
        monkeypatch.setattr(selfcheck, "check_settings", lambda: checks)
        return selfcheck.main()

    def test_blocking_failure_gives_two(self, monkeypatch, capsys) -> None:
        code = self._run(monkeypatch, [Check("Модель", FAIL, "нет ключа", blocking=True)])
        assert code == 2
        assert "НЕ ЗАПУСТИТСЯ" in capsys.readouterr().out

    def test_non_blocking_failure_gives_one(self, monkeypatch, capsys) -> None:
        """Telegram не поднялся — веб-интерфейс всё равно работает."""
        code = self._run(monkeypatch, [Check("Telegram", FAIL, "токен отклонён")])
        assert code == 1
        assert "часть функций не работает" in capsys.readouterr().out

    def test_warnings_alone_give_zero(self, monkeypatch, capsys) -> None:
        code = self._run(monkeypatch, [Check("База знаний", WARN, "пусто")])
        assert code == 0
        assert "Готов к работе" in capsys.readouterr().out

    def test_clean_run_gives_zero(self, monkeypatch, capsys) -> None:
        code = self._run(monkeypatch, [Check("Всё", OK, "хорошо")])
        assert code == 0
        assert "замечаний нет" in capsys.readouterr().out

    def test_broken_check_does_not_crash_the_run(self, monkeypatch, capsys) -> None:
        """Проверка, упавшая сама, не должна лишать отчёта об остальных."""
        def boom():
            raise RuntimeError("проверка сломалась")

        for name in ("check_model", "check_telegram", "check_google",
                     "check_search", "check_knowledge_base"):
            monkeypatch.setattr(selfcheck, name, lambda: [])
        monkeypatch.setattr(selfcheck, "check_settings", boom)

        code = selfcheck.main()
        out = capsys.readouterr().out
        assert "проверка сорвалась" in out
        assert code in (1, 2)


class TestSettingsCheck:
    @staticmethod
    def _bare(monkeypatch, **overrides):
        """Настройки без подсказок из окружения.

        Settings.__post_init__ дозаполняет пустые поля из переменных среды —
        это правильно для приложения, но здесь мешает: чтобы проверить
        реакцию на «ключа нет», окружение надо сначала вычистить.
        """
        from app.config import Settings

        for name in (
            "OPERON_LLM_API_KEY", "ROUTERAI_API_KEY", "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY", "OPERON_MODEL", "OPERON_ACCESS_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        # У стороннего шлюза нет модели по умолчанию — её обязан задать человек.
        conf = Settings(provider="routerai", **overrides)
        monkeypatch.setattr(selfcheck, "settings", conf)
        return conf

    def test_missing_model_is_blocking(self, monkeypatch) -> None:
        """Без имени модели приложение не стартует — это не «замечание»."""
        conf = self._bare(monkeypatch, api_key="k")
        assert conf.model == "", "у стороннего шлюза модели по умолчанию нет"

        # Точное имя: «Ключ доступа к модели» тоже содержит слово «модели».
        model = [c for c in selfcheck.check_settings() if c.name == "Имя модели"][0]
        assert model.status == FAIL
        assert model.blocking is True

    def test_missing_api_key_is_blocking(self, monkeypatch) -> None:
        self._bare(monkeypatch)
        key = [c for c in selfcheck.check_settings() if "Ключ" in c.name][0]
        assert key.status == FAIL
        assert key.blocking is True

    def test_missing_password_is_only_a_warning(self, monkeypatch) -> None:
        """Локально пароль не нужен; на сервере приложение откажется стартовать само."""
        self._bare(monkeypatch, api_key="k", model="m", access_password="")
        pwd = [c for c in selfcheck.check_settings() if "Пароль" in c.name][0]
        assert pwd.status == WARN
        assert pwd.blocking is False


class TestTelegramCheck:
    def test_absent_token_is_skipped_not_failed(self, monkeypatch) -> None:
        """Бот выключен намеренно — это не сбой."""
        monkeypatch.setattr(selfcheck, "settings", replace(settings, telegram_token=""))
        checks = selfcheck.check_telegram()
        assert checks[0].status == SKIP

    def test_rejected_token_is_reported_with_reason(self, monkeypatch) -> None:
        import httpx

        monkeypatch.setattr(selfcheck, "settings", replace(settings, telegram_token="1:bad"))
        monkeypatch.setattr(
            selfcheck,
            "_http",
            lambda url, **kw: httpx.Response(
                401, json={"ok": False, "description": "Unauthorized"},
                request=httpx.Request("GET", url),
            ),
        )
        checks = selfcheck.check_telegram()
        assert checks[0].status == FAIL
        assert "Unauthorized" in checks[0].detail
        assert any("BotFather" in h for h in checks[0].hints)

    def test_working_token_without_whitelist_is_a_failure(self, monkeypatch) -> None:
        """Бот с токеном, но без списка, не запустится — надо сказать прямо."""
        import httpx

        monkeypatch.setattr(selfcheck, "settings", replace(settings, telegram_token="1:good"))
        monkeypatch.setattr(
            selfcheck,
            "_http",
            lambda url, **kw: httpx.Response(
                200, json={"ok": True, "result": {"username": "operon_bot"}},
                request=httpx.Request("GET", url),
            ),
        )
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        checks = selfcheck.check_telegram()
        assert checks[0].status == OK
        assert checks[1].status == FAIL
        assert "userinfobot" in " ".join(checks[1].hints)


class TestKnowledgeBaseCheck:
    def test_empty_base_is_a_warning_not_a_failure(self) -> None:
        """Пустая база — ожидаемое состояние до наполнения, а не поломка."""
        checks = selfcheck.check_knowledge_base()
        assert checks[0].status in (OK, WARN)
        if checks[0].status == WARN:
            assert "не поломка" in " ".join(checks[0].hints)


class TestGoogleRedirectAdvice:
    """Совет чинить то, что уже верно, — худший вид отчёта о проверке."""

    @staticmethod
    def _client(monkeypatch, *, callback_mode: bool, declared: str, redirect: str):
        from dataclasses import replace as _replace

        from app.integrations import google_client, google_oauth

        monkeypatch.setattr(
            google_oauth,
            "describe_client",
            lambda: {
                "configured": True,
                "source": "переменные окружения",
                "redirect_uri": redirect,
                "callback_mode": callback_mode,
            },
        )
        monkeypatch.setattr(google_client, "status", lambda: {"connected": False, "reason": "нет токена"})
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", declared)
        monkeypatch.setattr(selfcheck, "settings", _replace(settings))

    def test_loopback_is_correct_for_a_desktop_client(self, monkeypatch) -> None:
        """Раньше здесь стояло ВНИМАНИЕ с советом перейти на публичный адрес."""
        self._client(
            monkeypatch, callback_mode=False, declared="desktop",
            redirect="http://localhost:8765/",
        )
        redirect = [c for c in selfcheck.check_google() if c.name == "Google: адрес возврата"][0]
        assert redirect.status == OK
        assert "так и нужно" in redirect.detail
        assert redirect.hints == [], "советовать здесь нечего"

    def test_loopback_without_declared_type_asks_which_it_is(self, monkeypatch) -> None:
        self._client(
            monkeypatch, callback_mode=False, declared="",
            redirect="http://localhost:8765/",
        )
        redirect = [c for c in selfcheck.check_google() if c.name == "Google: адрес возврата"][0]
        assert redirect.status == WARN
        hints = " ".join(redirect.hints)
        assert "Desktop" in hints and "Web" in hints, "оба варианта должны быть названы"

    def test_callback_mode_is_fine(self, monkeypatch) -> None:
        self._client(
            monkeypatch, callback_mode=True, declared="web",
            redirect="https://operon.amvera.io/oauth2/callback",
        )
        redirect = [c for c in selfcheck.check_google() if c.name == "Google: адрес возврата"][0]
        assert redirect.status == OK

    def test_desktop_with_public_redirect_is_a_contradiction(self, monkeypatch) -> None:
        """Заявлен Desktop, а адрес публичный — Google откажет уже после клика."""
        self._client(
            monkeypatch, callback_mode=True, declared="desktop",
            redirect="https://operon.amvera.io/oauth2/callback",
        )
        conflict = [c for c in selfcheck.check_google() if c.name == "Google: тип клиента"]
        assert conflict and conflict[0].status == FAIL


class TestDamagedKeys:
    """Ключ, испорченный при вставке, сервис называет «невалидным».

    Человек идёт перевыпускать исправный ключ и получает тот же ответ.
    Проверка обязана отличить «ключ плохой» от «ключ доехал побитым».
    """

    def test_non_ascii_in_key_is_named_as_paste_damage(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CSE_KEY", "AIzaSyXX•••••••••••")
        checks = selfcheck.check_search()

        assert checks[0].status == FAIL
        assert "испорчен при копировании" in checks[0].detail
        assert any("Перевыпускать ключ не нужно" in h for h in checks[0].hints)

    def test_dash_replacement_is_caught(self, monkeypatch) -> None:
        """Мессенджеры превращают дефис в тире — ключ ломается незаметно."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly—abc123")  # длинное тире
        assert selfcheck.check_search()[0].status == FAIL

    def test_inner_space_is_caught(self, monkeypatch) -> None:
        monkeypatch.setenv("SERPER_API_KEY", "abc 123")
        assert "пробел" in selfcheck.check_search()[0].detail

    def test_clean_key_passes_through(self, monkeypatch) -> None:
        for name in selfcheck.SEARCH_KEY_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("GOOGLE_CSE_KEY", "AIzaSyFAKEfakeFAKEfakeFAKEfakeFAKEfake0")
        monkeypatch.setenv("GOOGLE_CSE_ID", "0123456789abcdef0")

        assert selfcheck._damaged_keys() == []

    def test_absent_keys_are_not_damaged(self, monkeypatch) -> None:
        for name in selfcheck.SEARCH_KEY_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        assert selfcheck._damaged_keys() == []
