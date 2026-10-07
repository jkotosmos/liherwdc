"""Google через сервисный аккаунт: вход без OAuth-экранов и адресов возврата.

Проверяется: ключ читается из переменной/файла, «/auth почта» подключает в
нужном режиме (делегирование организации или общий доступ), у каждого
пользователя своя почта, календарь и Диск ведут себя по режиму.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from app.config import settings
from app.integrations import accounts, google_client, google_sa

OWNER, KIRILL = "8058569481", "1107365044"


def _key() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        "type": "service_account", "project_id": "operon", "private_key_id": "k1", "private_key": pem,
        "client_email": "operon-bot@operon.iam.gserviceaccount.com", "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


KEY = _key()


@pytest.fixture
def sa(monkeypatch, tmp_path):
    conf = replace(settings, data_dir=tmp_path / "state", credentials_dir=tmp_path / "cred")
    monkeypatch.setattr(google_sa, "settings", conf)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(KEY))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},{KIRILL}")
    monkeypatch.delenv("GOOGLE_ACCOUNTS", raising=False)
    google_client.reset_cache()
    yield conf
    google_client.reset_cache()


def delegation(monkeypatch, works: bool, access: str = "writer"):
    monkeypatch.setattr(google_sa, "_try_delegated", lambda mail: works)
    monkeypatch.setattr(google_sa, "_shared_access", lambda mail: access)


class TestKey:
    def test_json_base64_and_file(self, sa, monkeypatch, tmp_path) -> None:
        assert google_sa.email() == "operon-bot@operon.iam.gserviceaccount.com"
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", base64.b64encode(json.dumps(KEY).encode()).decode())
        assert google_sa.enabled()
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        assert not google_sa.enabled()
        (tmp_path / "cred").mkdir()
        (tmp_path / "cred" / "service_account.json").write_text(json.dumps(KEY))
        assert google_sa.enabled()

    def test_garbage_is_reported(self, sa, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{\"type\": \"authorized_user\"}")
        assert not google_sa.enabled() and "не ключ сервисного аккаунта" in google_sa.problem()

    def test_delegated_credentials_act_as_the_person(self, sa) -> None:
        creds = google_sa.credentials({"mode": "delegated", "email": "kirill@firma.ru"})
        assert creds._subject == "kirill@firma.ru"
        assert google_sa.credentials({"mode": "shared"})._subject is None


class TestConnect:
    def test_organization_account_needs_nothing(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=True)
        with accounts.use(KIRILL):
            link = google_sa.connect("Kirill@Firma.ru")
            assert link == {"email": "kirill@firma.ru", "mode": "delegated", "access": "owner"}
            assert google_client.mode() == "delegated"
            assert google_client.default_calendar() == "primary"
            assert google_client.can_invite() is True
            assert google_client.status()["account_hint"] == "kirill@firma.ru"

    def test_personal_gmail_with_shared_calendar(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=False, access="writer")
        with accounts.use(KIRILL):
            google_sa.connect("kirill@gmail.com")
            assert google_client.mode() == "shared"
            assert google_client.default_calendar() == "kirill@gmail.com"
            assert google_client.can_invite() is False

    def test_not_shared_explains_what_to_do(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=False, access="")
        with accounts.use(KIRILL), pytest.raises(google_sa.LinkError) as exc:
            google_sa.connect("kirill@gmail.com")
        text = str(exc.value)
        assert "operon-bot@operon.iam.gserviceaccount.com" in text and "Вносить изменения" in text

    def test_free_busy_only_is_not_enough(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=False, access="freeBusyReader")
        with accounts.use(KIRILL), pytest.raises(google_sa.LinkError, match="свободен/занят"):
            google_sa.connect("kirill@gmail.com")

    def test_each_person_has_own_mail(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=True)
        with accounts.use(KIRILL):
            google_sa.connect("kirill@firma.ru")
        with accounts.use(OWNER):
            assert google_sa.current_link() is None
            assert google_client.status()["connected"] is False
        with accounts.use(""):  # веб-чат = владелец, у него своя почта, не Кирилла
            assert google_sa.current_link() is None

    def test_preset_connects_without_commands(self, sa, monkeypatch) -> None:
        delegation(monkeypatch, works=True)
        monkeypatch.setenv("GOOGLE_ACCOUNTS", f"{KIRILL}=kirill@firma.ru")
        with accounts.use(KIRILL):
            assert google_sa.current_link()["email"] == "kirill@firma.ru"

    def test_bad_address(self, sa) -> None:
        with pytest.raises(google_sa.LinkError, match="/auth kirill@gmail.com"):
            google_sa.connect("кирилл")

    def test_access_role_comes_from_events_list(self, sa, monkeypatch) -> None:
        class Service:
            def events(self):
                return self

            def list(self, calendarId, maxResults):
                assert calendarId == "kirill@gmail.com"
                return self

            def execute(self):
                return {"accessRole": "writer", "items": []}

        monkeypatch.setattr(google_sa, "_calendar", lambda creds: Service())
        assert google_sa._shared_access("kirill@gmail.com") == "writer"


class _Insert:
    def __init__(self):
        self.kwargs = None

    def events(self):
        return self

    def insert(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        return {"id": "e1", "summary": "x", "start": {"dateTime": "2026-10-08T10:00:00+03:00"},
                "end": {"dateTime": "2026-10-08T11:00:00+03:00"}}


class TestToolsByMode:
    def test_shared_mode_keeps_attendees_in_description(self, sa, monkeypatch) -> None:
        from app.tools import calendar as cal
        from app.tools.base import registry

        delegation(monkeypatch, works=False)
        fake = _Insert()
        monkeypatch.setattr(cal, "_calendar", lambda: fake)
        with accounts.use(KIRILL):
            google_sa.connect("kirill@gmail.com")
            content, is_error = registry.execute("calendar_create_event", {
                "title": "Встреча", "start": "2026-10-08T10:00", "attendees": ["ivanov@mail.ru"],
            })
        assert not is_error, content
        body = fake.kwargs["body"]
        assert "attendees" not in body and "ivanov@mail.ru" in body["description"]
        assert fake.kwargs["calendarId"] == "kirill@gmail.com" and fake.kwargs["sendUpdates"] == "none"

    def test_delegated_mode_sends_invites(self, sa, monkeypatch) -> None:
        from app.tools import calendar as cal
        from app.tools.base import registry

        delegation(monkeypatch, works=True)
        fake = _Insert()
        monkeypatch.setattr(cal, "_calendar", lambda: fake)
        with accounts.use(KIRILL):
            google_sa.connect("kirill@firma.ru")
            registry.execute("calendar_create_event", {
                "title": "Встреча", "start": "2026-10-08T10:00", "attendees": ["ivanov@mail.ru"],
            })
        assert fake.kwargs["body"]["attendees"] == [{"email": "ivanov@mail.ru"}]
        assert fake.kwargs["calendarId"] == "primary" and fake.kwargs["sendUpdates"] == "all"

    def test_shared_drive_search_shows_only_own_files(self, sa, monkeypatch) -> None:
        from app.tools import drive

        delegation(monkeypatch, works=False)
        with accounts.use(KIRILL):
            google_sa.connect("kirill@gmail.com")
            assert drive._owner_filter() == (
                "('kirill@gmail.com' in owners or 'kirill@gmail.com' in writers or "
                "'kirill@gmail.com' in readers)"
            )
            content, is_error = __import__("app.tools.base", fromlist=["registry"]).registry.execute(
                "drive_create_file", {"name": "КП", "content": "текст"})
        assert is_error and "document_prepare" in content


class TestBot:
    def test_auth_in_service_account_mode(self, sa, monkeypatch) -> None:
        from tests.test_telegram_bot import FakeAgent, make_bot, message

        delegation(monkeypatch, works=True)
        bot, api = make_bot(FakeAgent([]), monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},{KIRILL}")
        bot._allowed = {int(OWNER), int(KIRILL)}

        bot._handle_update(message("/auth", user_id=int(KIRILL), chat_id=int(KIRILL)))
        assert "/auth kirill@gmail.com" in api.texts()[-1]
        assert "operon-bot@operon.iam.gserviceaccount.com" in api.texts()[-1]

        bot._handle_update(message("/auth kirill@firma.ru", user_id=int(KIRILL), chat_id=int(KIRILL)))
        assert "Google подключён" in api.texts()[-1] and "kirill@firma.ru" in api.texts()[-1]


def test_failed_preset_is_not_retried_every_message(sa, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(google_sa, "_try_delegated", lambda mail: calls.append(mail) or False)
    monkeypatch.setattr(google_sa, "_shared_access", lambda mail: "")
    monkeypatch.setenv("GOOGLE_ACCOUNTS", f"{KIRILL}=kirill@gmail.com")
    google_sa._preset_failed.clear()
    with accounts.use(KIRILL):
        assert google_sa.current_link() is None
        assert google_sa.current_link() is None
    assert calls == ["kirill@gmail.com"]
    google_sa._preset_failed.clear()


def test_selfcheck_in_service_account_mode(sa, monkeypatch) -> None:
    from app import selfcheck

    with accounts.use(KIRILL):
        checks = selfcheck.check_google()
    assert checks[0].status == selfcheck.OK and "operon-bot@" in checks[0].detail
    assert checks[1].status == selfcheck.WARN and "/auth" in checks[1].hints[0]
