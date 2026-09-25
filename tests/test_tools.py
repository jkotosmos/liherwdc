"""Поведение отдельных инструментов, включая деградацию без интеграций."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.config import settings
from app.errors import IntegrationUnavailable, ToolError
from app.tools import registry
from app.tools.calendar import _to_rfc3339


@pytest.fixture(autouse=True)
def clean_tasks():
    settings.tasks_path.unlink(missing_ok=True)
    yield
    settings.tasks_path.unlink(missing_ok=True)


def run(name: str, payload: dict) -> dict:
    content, is_error = registry.execute(name, payload)
    assert not is_error, content
    return json.loads(content)


class TestRegistry:
    def test_every_write_tool_is_gated(self) -> None:
        """Проверка на будущее: новый изменяющий инструмент не должен пройти мимо шлюза."""
        gated = {spec.name for spec in registry.all() if spec.requires_confirmation}
        assert gated == {
            "calendar_create_event",
            "calendar_update_event",
            "calendar_delete_event",
            "drive_create_file",
            "task_create",
            "task_update",
            "kpi_upsert",
            "protocol_save",
        }

    def test_read_tools_are_not_gated(self) -> None:
        for name in ("kb_search", "kb_get_document", "tasks_list", "drive_search", "calendar_list_events"):
            assert registry.get(name).requires_confirmation is False

    def test_tool_error_becomes_error_result_not_exception(self) -> None:
        content, is_error = registry.execute("kb_search", {})
        assert is_error
        assert "query" in content

    def test_unknown_tool_is_reported(self) -> None:
        content, is_error = registry.execute("несуществующий", {})
        assert is_error and "Неизвестный инструмент" in content

    def test_every_tool_has_a_schema_and_description(self) -> None:
        for spec in registry.all():
            assert spec.description.strip()
            assert spec.input_schema.get("type") == "object"


class TestTasks:
    def test_empty_registry_reports_emptiness(self) -> None:
        assert run("tasks_list", {})["status"] == "empty"

    def test_create_then_list(self) -> None:
        created = run("task_create", {"title": "Согласовать бюджет", "assignee": "Петров"})
        assert created["task"]["id"] == "T-0001"

        listed = run("tasks_list", {})
        assert listed["count"] == 1
        assert listed["tasks"][0]["title"] == "Согласовать бюджет"

    def test_overdue_is_computed(self) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        run("task_create", {"title": "Просроченная", "due_date": yesterday})
        listed = run("tasks_list", {"overdue_only": True})
        assert listed["count"] == 1
        assert listed["tasks"][0]["overdue"] is True

    def test_done_task_is_not_overdue(self) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        run("task_create", {"title": "Сделана вовремя", "due_date": yesterday})
        run("task_update", {"task_id": "T-0001", "status": "done"})
        assert run("tasks_list", {"overdue_only": True})["status"] == "not_found"

    def test_update_appends_dated_note(self) -> None:
        run("task_create", {"title": "С комментарием"})
        updated = run("task_update", {"task_id": "T-0001", "notes": "Ждём ответ партнёра"})
        assert "Ждём ответ партнёра" in updated["after"]["notes"]

    def test_invalid_status_is_rejected(self) -> None:
        run("task_create", {"title": "Тест"})
        content, is_error = registry.execute("task_update", {"task_id": "T-0001", "status": "готово"})
        assert is_error and "Неизвестный статус" in content

    def test_invalid_date_is_rejected(self) -> None:
        content, is_error = registry.execute("task_create", {"title": "Тест", "due_date": "01.09.2026"})
        assert is_error and "YYYY-MM-DD" in content

    def test_unknown_task_id_is_rejected(self) -> None:
        content, is_error = registry.execute("task_update", {"task_id": "T-9999", "status": "done"})
        assert is_error and "не найдено" in content

    def test_preview_is_human_readable(self) -> None:
        preview = registry.get("task_create").build_preview(
            {"title": "Подготовить отчёт", "assignee": "Сидоров", "due_date": "2026-09-30"}
        )
        assert preview.title == "Зафиксировать поручение"
        assert "Подготовить отчёт" in preview.summary
        assert preview.details["Срок"] == "2026-09-30"


class TestKnowledgeBaseTools:
    def test_empty_kb_returns_explicit_hint_not_silence(self) -> None:
        """Агент должен получить прямое указание не выдумывать содержание."""
        result = run("kb_search", {"query": "тарифы"})
        assert result["status"] == "empty_knowledge_base"
        assert "не придумывай" in result["hint"].lower()

    def test_missing_document_is_an_error_with_alternatives(self) -> None:
        content, is_error = registry.execute("kb_get_document", {"doc_id": "нет-такого.md"})
        assert is_error and "не найден" in content


class TestGoogleDegradation:
    """Без OAuth-токена инструменты обязаны объяснять причину, а не падать."""

    @pytest.mark.parametrize(
        ("tool", "payload"),
        [
            ("drive_search", {"query": "договор"}),
            ("drive_read", {"file_id": "abc"}),
            ("calendar_list_events", {}),
        ],
    )
    def test_tools_report_setup_instructions(self, tool: str, payload: dict) -> None:
        content, is_error = registry.execute(tool, payload)
        assert is_error
        assert "/auth" in content or "не установлены" in content

    def test_status_helper_does_not_raise(self) -> None:
        from app.integrations import google_client

        assert google_client.status()["connected"] is False


class TestCalendarParsing:
    def test_date_only_becomes_start_of_day(self) -> None:
        assert _to_rfc3339("2026-08-20").startswith("2026-08-20T00:00")

    def test_date_only_end_of_day(self) -> None:
        assert _to_rfc3339("2026-08-20", end_of_day=True).startswith("2026-08-20T23:59")

    def test_naive_datetime_gets_configured_timezone(self) -> None:
        assert _to_rfc3339("2026-08-20T15:30").endswith(("+03:00", "+02:00", "+00:00"))

    def test_garbage_date_is_rejected(self) -> None:
        with pytest.raises(ToolError):
            _to_rfc3339("20 августа")


class TestErrors:
    def test_integration_error_is_a_tool_error(self) -> None:
        assert issubclass(IntegrationUnavailable, ToolError)
