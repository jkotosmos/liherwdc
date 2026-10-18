"""Права к «Кванту» по роли с сайта базы знаний и даты документов.

Сайт хранит в MongoDB: пользователя агента (chatIdTG, role, status), роли,
файлы и темы. В Qdrant у фрагмента — metadata.accessibleByRoles (ID ролей
строками), fileId или topicId. Настоящая база в тестах не нужна: подставляем
коллекции с теми же полями, Qdrant — заглушка из test_quant.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app import quant, selfcheck, site_db
from app.integrations import accounts
from app.tools.base import registry
from test_quant import URL, FakeQdrant

MANAGER, DIRECTOR = ObjectId(), ObjectId()
FILE_DRIVE, FILE_UPLOADED, TOPIC = ObjectId(), ObjectId(), ObjectId()

SITE_POINTS = [
    {"id": 1, "payload": {"text": "Тарифы для партнёров: базовый 10 000 руб.", "metadata": {
        "source": "google_drive", "fileId": str(FILE_DRIVE), "name": "Тарифы 2026",
        "link": "https://docs.google.com/document/d/abc/edit", "accessibleByRoles": [str(MANAGER)]}}},
    {"id": 2, "payload": {"text": "Тарифы директора: скидки до 30%.", "metadata": {
        "source": "file", "fileId": str(FILE_UPLOADED), "name": "Тарифы для директора",
        "accessibleByRoles": [str(DIRECTOR)]}}},
    {"id": 3, "payload": {"text": "# Тарифы: регламент\nСогласует директор.", "metadata": {
        "source": "topic", "topicId": str(TOPIC), "name": "Тарифы: регламент",
        "accessibleByRoles": [str(MANAGER), str(DIRECTOR)]}}},
]


class FakeCollection:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def find_one(self, flt, projection=None):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in flt.items())), None)

    def find(self, flt, projection=None):
        wanted = flt["_id"]["$in"]
        return [d for d in self.docs if d["_id"] in wanted]

    def count_documents(self, flt):
        return len(self.docs)


class FakeDB(dict):
    def __getattr__(self, name):
        return self[name]


@pytest.fixture
def site(monkeypatch, tmp_path):
    db = FakeDB(
        agentroles=FakeCollection([{"_id": MANAGER, "name": "Менеджер"}, {"_id": DIRECTOR, "name": "Директор"}]),
        agentusers=FakeCollection([
            {"chatIdTG": "111", "role": MANAGER, "status": "active"},
            {"chatIdTG": "222", "role": None, "status": "pending"},
            {"chatIdTG": "333", "role": DIRECTOR, "status": "blocked"},
        ]),
        knowledgefiles=FakeCollection([
            # 22:30 UTC 1 сентября — уже 2 сентября по Москве.
            {"_id": FILE_DRIVE, "google": {"modifiedTime": datetime(2026, 9, 1, 22, 30)},
             "createdAt": datetime(2026, 1, 5, tzinfo=timezone.utc)},
            {"_id": FILE_UPLOADED, "createdAt": datetime(2026, 8, 15, 9, 0)},
        ]),
        topics=FakeCollection([{"_id": TOPIC, "status": "review", "updatedAt": datetime(2026, 10, 10, 12, 0)}]),
    )
    calls = {"db": 0}

    def fake_db():
        calls["db"] += 1
        return db

    monkeypatch.setenv("OPERON_MONGODB_URI", "mongodb://site/operon")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111,222,333,444")
    monkeypatch.setattr(site_db, "_db", fake_db)
    site_db.reset()
    yield db, calls
    site_db.reset()


@pytest.fixture
def qdrant(monkeypatch, tmp_path):
    for name in ("QDRANT_COLLECTION", "QDRANT_VECTOR_NAME", "QDRANT_EMBEDDING_MODEL", "QDRANT_TEXT_FIELD",
                 "QDRANT_API_KEY", "QDRANT_ROLES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QDRANT_URL", URL)
    monkeypatch.setenv("QDRANT_API_KEY", "secret")
    from dataclasses import replace

    monkeypatch.setattr(quant, "settings", replace(quant.settings, data_dir=tmp_path))
    monkeypatch.setattr(sys.modules["test_quant"], "POINTS", SITE_POINTS)
    fake = FakeQdrant()
    monkeypatch.setattr(quant.httpx, "request", fake.request)
    monkeypatch.setattr(quant.httpx, "post", fake.post)
    quant.reset_cache()
    yield fake
    quant.reset_cache()


def titles(result):
    return sorted(r["title"] for r in result.get("results", []))


def test_role_from_site_limits_documents(site, qdrant) -> None:
    with accounts.use("111"):
        result = quant.search("тарифы")
    assert result["access_role"] == "Менеджер"
    assert titles(result) == ["Тарифы 2026", "Тарифы: регламент"]


def test_vector_search_is_filtered_by_role_id(site, qdrant, monkeypatch) -> None:
    """Как у бота сайта: фильтр metadata.accessibleByRoles по ID роли."""
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    with accounts.use("111"):
        quant.search("тарифы")
    body = next(body for _, path, body in qdrant.calls if path.endswith("/points/search"))
    assert body["filter"] == {"must": [{"key": "metadata.accessibleByRoles", "match": {"any": [str(MANAGER)]}}]}


def test_document_dates_come_from_site(site, qdrant) -> None:
    with accounts.use("111"):
        result = quant.search("тарифы")
    by_title = {r["title"]: r for r in result["results"]}
    assert by_title["Тарифы 2026"]["date"] == "02.09.2026"
    assert by_title["Тарифы 2026"]["date_meaning"] == "изменён в Google Drive"
    assert by_title["Тарифы 2026"]["link"] == "https://docs.google.com/document/d/abc/edit"
    assert by_title["Тарифы: регламент"]["date"] == "10.10.2026"
    assert by_title["Тарифы: регламент"]["topic_status"] == "на проверке"


def test_uploaded_file_gets_upload_date_and_no_fake_drive_link(site, qdrant, monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_ROLES", str(DIRECTOR))  # роль по умолчанию для тех, кого нет на сайте
    with accounts.use("444"):
        result = quant.search("тарифы")
    assert result["access_role"] == "по умолчанию (QDRANT_ROLES)"
    uploaded = next(r for r in result["results"] if r["title"] == "Тарифы для директора")
    assert uploaded["date"] == "15.08.2026" and uploaded["date_meaning"] == "загружен на сайт"
    # fileId здесь — номер документа на сайте, а не файл Google Drive: ссылку из него не выдумываем.
    assert uploaded["link"] is None


@pytest.mark.parametrize("user, words", [
    ("222", "не назначена роль"),
    ("333", "заблокирован"),
    ("444", "нет среди пользователей агента"),
])
def test_without_role_documents_are_not_shown(site, qdrant, user, words) -> None:
    with accounts.use(user):
        result = quant.search("тарифы")
        content, is_error = registry.execute("quant_search", {"query": "тарифы"})
    assert result["status"] == "no_access" and "results" not in result
    assert words in result["hint"]
    assert not is_error and words in content and "в обход прав" in content
    assert not any(path.endswith("/points/scroll") for _, path, _ in qdrant.calls), "в Квант даже не ходим"


def test_blocked_user_gets_no_default_roles(site, qdrant, monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_ROLES", str(MANAGER))
    with accounts.use("333"):
        assert quant.search("тарифы")["status"] == "no_access"


def test_web_chat_is_the_owner(site, qdrant) -> None:
    """Веб-чат по паролю — владелец, первый в TELEGRAM_ALLOWED_USERS."""
    with accounts.use(""):
        assert quant.search("тарифы")["access_role"] == "Менеджер"


def test_role_is_cached_for_a_minute_and_change_is_picked_up(site, qdrant) -> None:
    db, calls = site
    with accounts.use("111"):
        quant.search("тарифы")
        quant.search("тарифы")
        # Роль и даты (файлы, темы) — по запросу в базу; второй поиск целиком из кэша.
        assert calls["db"] == 3
        db["agentusers"].docs[0]["role"] = DIRECTOR
        site_db._roles.clear()  # прошла минута
        assert quant.search("тарифы")["access_role"] == "Директор"


def test_site_down_uses_last_known_role_then_refuses(site, qdrant, monkeypatch) -> None:
    with accounts.use("111"):
        assert quant.search("тарифы")["access_role"] == "Менеджер"

        def down():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(site_db, "_db", down)
        monkeypatch.setattr(site_db, "ROLE_TTL", 0)
        result = quant.search("тарифы")
        assert result["access_role"] == "Менеджер"  # последняя известная роль
        assert "Даты документов с сайта сейчас недоступны." not in result.get("warnings", [])  # даты из кэша
        site_db._roles.clear()
        result = quant.search("тарифы")
    assert result["status"] == "no_access" and "сайт базы знаний сейчас не отвечает" in result["hint"]
    assert "connection refused" not in result["hint"], "технические подробности — в журнал, не пользователю"


def test_without_site_db_nothing_changes(qdrant, monkeypatch) -> None:
    monkeypatch.delenv("OPERON_MONGODB_URI", raising=False)
    result = quant.search("тарифы")
    assert "access_role" not in result and len(result["results"]) == 3


def test_selfcheck_shows_role(site, qdrant) -> None:
    with accounts.use("111"):
        checks = {c.name: c for c in selfcheck.check_quant_rights()}
    assert checks["База сайта (MongoDB)"].status == selfcheck.OK
    assert checks["Права в Кванте"].status == selfcheck.OK and "Менеджер" in checks["Права в Кванте"].detail
    with accounts.use("222"):
        rights = {c.name: c for c in selfcheck.check_quant_rights()}["Права в Кванте"]
    assert rights.status == selfcheck.WARN and "222" in rights.detail


def test_selfcheck_without_site_db_warns_only_for_several_users(qdrant, monkeypatch) -> None:
    from app.telegram import access

    monkeypatch.delenv("OPERON_MONGODB_URI", raising=False)
    monkeypatch.setattr(access, "allowed", lambda: frozenset({1, 2}))
    [check] = selfcheck.check_quant_rights()
    assert check.status == selfcheck.WARN and "OPERON_MONGODB_URI" in check.hints[0]
    monkeypatch.setattr(access, "allowed", lambda: frozenset({1}))
    assert selfcheck.check_quant_rights()[0].status == selfcheck.OK
