"""Пульт «Сегодня», список документов и выбор модели только из трёх семейств."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, dashboard, model_families, quant, routerai, server, site_db
from app.integrations import google_client
from app.tools import calendar as calendar_tool
from app.tools import kpi as kpi_tool
from app.tools import remind
from app.tools import tasks as tasks_tool

OWNER = "111"
MANAGER_ROLE = "6aa000000000000000000001"
DIRECTOR_ROLE = "6aa000000000000000000002"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER},222")
    monkeypatch.delenv("OPERON_ACCESS_PASSWORD", raising=False)
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.example")
    monkeypatch.setenv("QDRANT_COLLECTION", "knowledge_base")
    monkeypatch.delenv("QDRANT_ROLES", raising=False)
    monkeypatch.setenv("OPERON_MONGODB_URI", "mongodb://site/operon")
    monkeypatch.setattr(routerai, "available", lambda: False)

    monkeypatch.setattr(google_client, "status", lambda: {
        "connected": True, "mode": "delegated", "account_hint": "kirill@gmail.com"})
    monkeypatch.setattr(calendar_tool, "list_events_between", lambda day_from, day_to, calendar_id="": [
        {"event_id": "e2", "title": "Планёрка", "start": "2026-10-26T15:00:00+03:00",
         "end": "2026-10-26T15:30:00+03:00", "all_day": False, "location": "", "attendees": [{"email": "a"}],
         "organizer": "kirill@gmail.com"},
        {"event_id": "e1", "title": "Созвон с Ивановым", "start": "2026-10-26T10:00:00+03:00",
         "end": "2026-10-26T11:00:00+03:00", "all_day": False, "location": "Zoom", "attendees": [],
         "organizer": ""},
    ])
    monkeypatch.setattr(tasks_tool, "load_active", lambda: [
        {"id": "T-0002", "title": "КП для Петрова", "assignee": "Иван", "due_date": "2026-10-20",
         "days_left": -6, "overdue": True, "priority": "high", "status": "open", "status_ru": "открыто"},
        {"id": "T-0001", "title": "Отчёт за неделю", "assignee": "", "due_date": "2026-10-26",
         "days_left": 0, "overdue": False, "priority": "normal", "status": "in_progress", "status_ru": "в работе"},
        {"id": "T-0003", "title": "Без срока", "assignee": "", "due_date": None,
         "days_left": None, "overdue": False, "priority": "low", "status": "open", "status_ru": "открыто"},
    ])
    monkeypatch.setattr(kpi_tool, "_load", lambda: [{}, {}, {}])
    monkeypatch.setattr(kpi_tool, "load_deviations", lambda: [
        {"name": "Выручка", "period": "2026-10", "status": "warning", "status_ru": "внимание",
         "plan": 1000, "fact": 900, "deviation_pct": -10.0, "unit": "₽"},
        {"name": "Лидов", "period": "2026-10", "status": "critical", "status_ru": "критично",
         "plan": 50, "fact": 20, "deviation_pct": -60.0, "unit": "шт."},
    ])
    monkeypatch.setattr(remind, "_load", lambda: [
        {"id": "R-1", "account": OWNER, "text": "Позвонить Петрову", "at": "2026-10-26T18:00:00+03:00",
         "repeat": "", "status": "active"},
        {"id": "R-2", "account": "222", "text": "Чужое", "at": "2026-10-26T19:00:00+03:00",
         "repeat": "", "status": "active"},
    ])

    # Квант: три фрагмента одного файла, фрагмент темы и фрагмент без роли.
    points = [
        {"id": 1, "payload": {"text": "Тарифы партнёров", "metadata": {
            "source": "google_drive", "fileId": "f1", "name": "Тарифы 2026",
            "link": "https://docs.google.com/document/d/f1/edit", "accessibleByRoles": [MANAGER_ROLE],
            "modifiedTime": "2026-09-01"}}},
        {"id": 2, "payload": {"text": "Скидки партнёрам", "metadata": {
            "source": "google_drive", "fileId": "f1", "name": "Тарифы 2026",
            "accessibleByRoles": [MANAGER_ROLE]}}},
        {"id": 3, "payload": {"text": "# Регламент\nСогласует директор.", "metadata": {
            "source": "topic", "topicId": "t1", "name": "Регламент продаж",
            "accessibleByRoles": [MANAGER_ROLE, DIRECTOR_ROLE]}}},
        {"id": 4, "payload": {"text": "Только для директора", "metadata": {
            "source": "file", "fileId": "f9", "name": "Прогноз 2027",
            "accessibleByRoles": [DIRECTOR_ROLE]}}},
    ]
    monkeypatch.setattr(quant, "_scroll", lambda name, conf, **kw: points)
    monkeypatch.setattr(quant, "collection_names", lambda conf: ("knowledge_base",))
    monkeypatch.setattr(quant, "collection_info", lambda name, conf: SimpleNamespace(points=4, size=3072, name=name))
    monkeypatch.setattr(site_db, "enabled", lambda: True)
    roles = {OWNER: ("Директор", DIRECTOR_ROLE), "222": ("Менеджер", MANAGER_ROLE)}

    def access(account=None):
        from app.integrations import accounts as acc
        key = str(account if account is not None else acc.current()) or OWNER
        if key in roles:
            name, role_id = roles[key]
            return site_db.Access("role", role_id=role_id, role_name=name)
        return site_db.Access("no_user")

    monkeypatch.setattr(site_db, "access", access)
    monkeypatch.setattr(site_db, "explain", lambda found, user: "нет роли на сайте")
    monkeypatch.setattr(site_db, "add_dates", lambda items: "")
    dashboard._library_cache.clear()
    yield
    dashboard._library_cache.clear()


@pytest.fixture
def client(world) -> TestClient:
    with TestClient(server.app) as test_client:
        yield test_client


# --- модели -----------------------------------------------------------------


@pytest.mark.parametrize("model_id, family", [
    ("deepseek/deepseek-chat", "deepseek"),
    ("openai/gpt-4.1-mini", "gpt"),
    ("anthropic/claude-sonnet-4.5", "claude"),
    ("google/gemini-2.5-flash", None),
    ("openai/o3", None),
    ("meta/llama-3", None),
])
def test_only_three_families_are_offered(model_id, family) -> None:
    assert model_families.family(model_id) == family
    assert model_families.is_allowed(model_id) is (family is not None)


def test_models_endpoint_lists_only_three_families(client, monkeypatch) -> None:
    monkeypatch.setattr(routerai, "available", lambda: True)
    monkeypatch.setattr(routerai, "currency", lambda: "₽")
    catalog = [
        {"id": "deepseek/deepseek-chat", "name": "DeepSeek V3", "tools": True, "price_in": 1, "price_out": 2},
        {"id": "openai/gpt-4.1-mini", "name": "GPT-4.1 mini", "tools": True, "price_in": 1, "price_out": 2},
        {"id": "anthropic/claude-haiku", "name": "Claude Haiku", "tools": None, "price_in": 1, "price_out": 2},
        {"id": "google/gemini-2.5-flash", "name": "Gemini", "tools": True, "price_in": 1, "price_out": 2},
        {"id": "openai/gpt-4o-audio", "name": "No tools", "tools": False, "price_in": 1, "price_out": 2},
    ]
    monkeypatch.setattr(routerai, "list_models", lambda force=False, client=None: catalog)
    data = client.get("/api/models").json()
    assert sorted(m["id"] for m in data["models"]) == [
        "anthropic/claude-haiku", "deepseek/deepseek-chat", "openai/gpt-4.1-mini"]
    assert [(f["key"], f["count"]) for f in data["families"]] == [("deepseek", 1), ("gpt", 1), ("claude", 1)]
    assert data["hidden_without_tools"] == 1
    assert data["hidden_by_family"] == 1


def test_choosing_other_family_is_refused(client, monkeypatch) -> None:
    saved = []
    monkeypatch.setattr(server.model_choice, "set_model", lambda model_id, changed_by="": saved.append(model_id))
    response = client.post("/api/model", json={"model": "google/gemini-2.5-flash"})
    assert response.status_code == 422
    assert "DeepSeek, GPT и Claude" in response.json()["detail"]
    assert saved == []


# --- пульт ------------------------------------------------------------------


def test_dashboard_sections_are_filled(client) -> None:
    data = client.get("/api/dashboard").json()
    assert data["errors"] == {}
    meetings = data["meetings"]["items"]
    assert [m["title"] for m in meetings] == ["Созвон с Ивановым", "Планёрка"]  # по времени
    assert meetings[0]["time"] == "10:00" and meetings[0]["end"] == "11:00"
    tasks = data["tasks"]
    assert (tasks["open"], tasks["overdue"], tasks["today"]) == (3, 1, 1)
    assert tasks["items"][0]["id"] == "T-0002"  # просроченное — первым
    assert data["kpi"]["critical"] == 1 and data["kpi"]["warning"] == 1
    assert data["kpi"]["items"][0]["name"] == "Лидов"  # критичное — первым
    assert data["reminders"]["active"] == 1  # чужое напоминание не видно
    assert data["google"]["connected"] is True
    assert data["today"]["iso"] == date.today().isoformat() or data["today"]["iso"]


def test_one_failed_section_does_not_break_the_rest(client, monkeypatch) -> None:
    def broken(day_from, day_to, calendar_id=""):
        raise RuntimeError("Google ответил 503")

    monkeypatch.setattr(calendar_tool, "list_events_between", broken)
    data = client.get("/api/dashboard").json()
    assert "Календарь" in data["errors"] and "503" in data["errors"]["Календарь"]
    assert data["tasks"]["open"] == 3
    assert data["knowledge"]["documents"] == 2  # директор: регламент и прогноз


def test_meetings_without_google_say_why(client, monkeypatch) -> None:
    monkeypatch.setattr(google_client, "status", lambda: {"connected": False, "reason": "Нет OAuth-токена"})
    data = client.get("/api/dashboard").json()
    assert data["meetings"]["available"] is False
    assert data["meetings"]["reason"] == "Нет OAuth-токена"


# --- документы ----------------------------------------------------------------


def test_documents_grouped_by_file_and_limited_to_role(client) -> None:
    director = client.get("/api/documents").json()  # владелец — директор
    assert sorted(d["title"] for d in director["items"]) == ["Прогноз 2027", "Регламент продаж"]
    assert director["by_source"] == {"Тема": 1, "Файл": 1}
    assert director["role"] == "Директор"


def test_manager_sees_grouped_fragments(client, monkeypatch) -> None:
    dashboard._library_cache.clear()
    lib = dashboard._library("222")
    tariffs = next(d for d in lib["items"] if d["title"] == "Тарифы 2026")
    assert tariffs["fragments"] == 2  # два фрагмента одного файла — один документ
    assert tariffs["source"] == "Google Drive"
    assert tariffs["date"] == "01.09.2026"
    assert tariffs["link"].startswith("https://docs.google.com/")


def test_manager_sees_only_manager_documents(client) -> None:
    dashboard._library_cache.clear()
    with_manager = dashboard._library("222")
    titles = sorted(d["title"] for d in with_manager["items"])
    assert titles == ["Регламент продаж", "Тарифы 2026"]  # «Прогноз» — только для директора
    assert with_manager["role"] == "Менеджер"


def test_user_without_role_gets_no_documents(client) -> None:
    lib = dashboard._library("555")
    assert lib["access"] is False
    assert lib["documents"] == 0
    assert "нет роли" in lib["reason"]


def test_dashboard_requires_login_when_password_is_set(client, monkeypatch) -> None:
    monkeypatch.setattr(server, "settings", replace(server.settings, access_password="секрет"))
    assert client.get("/api/dashboard").status_code == 401


def test_telegram_token_still_opens_dashboard(client, monkeypatch) -> None:
    monkeypatch.setattr(server, "settings", replace(server.settings, access_password="секрет"))
    token = auth.issue_token(OWNER)
    response = client.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["meetings"]["items"][0]["title"] == "Созвон с Ивановым"


def test_miniapp_page_and_brand_font_are_served(client) -> None:
    """Пульт открывается по /miniapp, шрифт из админки отдаётся статикой."""
    page = client.get("/miniapp")
    assert page.status_code == 200
    assert "Пульт управления" in page.text and "telegram-web-app.js" in page.text
    font = client.get("/static/fonts/Gilroy-Regular.ttf")
    assert font.status_code == 200 and font.headers["content-type"].startswith(("font/", "application/"))


def test_dashboard_is_only_in_miniapp(client) -> None:
    """Пульт включается флагом только в мини-приложении; веб-чат на / остаётся чатом."""
    web = client.get("/")
    mini = client.get("/miniapp")
    assert "window.OPERON_MINIAPP = true" not in web.text
    assert "telegram-web-app.js" not in web.text
    assert "window.OPERON_MINIAPP = true" in mini.text
