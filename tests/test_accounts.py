"""У каждого пользователя свой Google: чужой токен не подставляется никогда."""

from __future__ import annotations

import pytest

from fakes import FakeClient, text_block, tool_use_block, turn

from app import auth
from app.integrations import accounts, google_client, token_store

OWNER, OTHER = "8058569481", "198704816"


@pytest.fixture(autouse=True)
def two_users(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},{OTHER}")
    monkeypatch.delenv("OPERON_TOKEN_KEY", raising=False)
    google_client.reset_cache()
    yield
    for account in ("", OTHER):
        with accounts.use(account):
            token_store._plain_path().unlink(missing_ok=True)
            token_store._encrypted_path().unlink(missing_ok=True)
    google_client.reset_cache()


class TestResolve:
    def test_owner_and_web_share_main_token(self) -> None:
        assert accounts.resolve(OWNER) == "" and accounts.resolve("") == ""

    def test_other_user_has_own_key(self) -> None:
        assert accounts.resolve(OTHER) == OTHER

    def test_owner_is_first_listed_not_smallest(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "900, 100")
        assert accounts.owner_id() == "900"


class TestTokens:
    def test_owner_token_is_not_visible_to_other_user(self) -> None:
        """Ровно то, что было бы со вторым ID в белом списке до исправления."""
        with accounts.use(OWNER):
            token_store.save_token('{"token": "owner"}')
            assert token_store.token_exists()
        with accounts.use(OTHER):
            assert not token_store.token_exists()
            assert token_store.load_token() is None

    def test_other_user_token_is_separate(self) -> None:
        with accounts.use(OTHER):
            path = token_store.save_token('{"token": "other"}')
        assert OTHER in path.name
        with accounts.use(OWNER):
            assert not token_store.token_exists()

    def test_status_says_not_connected_for_other(self) -> None:
        with accounts.use(OWNER):
            token_store.save_token('{"token": "owner"}')
        with accounts.use(OTHER):
            status = google_client.status()
        assert status["connected"] is False
        assert "свой доступ" in status["hint"] or "/auth" in status["hint"]


def test_agent_runs_tools_as_session_owner(monkeypatch) -> None:
    from app.agent import OperonAgent, Session
    from app.tools import registry

    seen = []
    real = registry.execute

    def spy(name, tool_input):
        seen.append(accounts.current())
        return real(name, tool_input)

    monkeypatch.setattr(registry, "execute", spy)
    agent = OperonAgent()
    agent._client = FakeClient([
        turn(tool_use_block("t1", "kb_list_documents", {}), stop_reason="tool_use"),
        turn(text_block("Готово.")),
    ])
    session = Session(session_id="acc")
    with accounts.use(OTHER):
        generator = agent.send_user_message(session, "что в базе?")
        next(generator)  # первое событие — сессия запоминает пользователя
    list(generator)  # дальше — вне контекста, как в пуле потоков веб-сервера
    assert session.account == OTHER
    assert seen == [OTHER]


class TestMiniappToken:
    def test_subject_is_signed(self) -> None:
        token = auth.issue_token(OTHER)
        assert auth.token_is_valid(token)
        assert auth.token_subject(token) == OTHER

    def test_forged_subject_rejected(self) -> None:
        issued, _, signature = auth.issue_token(OTHER).split(".")
        forged = f"{issued}.{OWNER}.{signature}"
        assert not auth.token_is_valid(forged)
        assert auth.token_subject(forged) == ""

    def test_password_login_token_is_owner(self) -> None:
        assert auth.token_subject(auth.issue_token()) == ""
