"""Показатели и протоколы — два пункта ТЗ, которые раньше держались на промпте.

Смысл обоих модулей в том, что вывод делает код, а не формулировка в промпте:
статус отклонения считается по порогам, а поручения из протокола реально
попадают в реестр, а не остаются текстом в чате.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.config import settings
from app.tools import kpi as kpi_module
from app.tools import protocol as protocol_module
from app.tools.base import ToolError, registry


@pytest.fixture
def store(tmp_path, monkeypatch):
    conf = replace(settings, data_dir=tmp_path)
    monkeypatch.setattr(kpi_module, "settings", conf)
    monkeypatch.setattr(protocol_module, "settings", conf)
    monkeypatch.setattr("app.tools.tasks.settings", conf)
    return conf


class TestDeviationIsComputed:
    """Порог задаёт человек, вывод делает арифметика — не модель."""

    def test_shortfall_below_warning_is_ok(self) -> None:
        result = kpi_module.evaluate(
            {"plan": 100, "fact": 95, "direction": "higher_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert result["status"] == "ok"
        assert result["deviation_pct"] == -5.0

    def test_warning_threshold(self) -> None:
        result = kpi_module.evaluate(
            {"plan": 100, "fact": 88, "direction": "higher_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert result["status"] == "warning"
        assert result["shortfall_pct"] == 12.0

    def test_critical_threshold(self) -> None:
        result = kpi_module.evaluate(
            {"plan": 100, "fact": 70, "direction": "higher_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert result["status"] == "critical"

    def test_overachievement_is_never_a_deviation(self) -> None:
        result = kpi_module.evaluate(
            {"plan": 100, "fact": 150, "direction": "higher_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert result["status"] == "ok"

    def test_direction_flips_the_meaning(self) -> None:
        """Для стоимости привлечения плохо превышение плана, а не отставание."""
        cheaper = kpi_module.evaluate(
            {"plan": 100, "fact": 70, "direction": "lower_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert cheaper["status"] == "ok", "потратили меньше плана — это хорошо"

        pricier = kpi_module.evaluate(
            {"plan": 100, "fact": 130, "direction": "lower_is_better",
             "warning_pct": 10, "critical_pct": 20}
        )
        assert pricier["status"] == "critical"

    def test_missing_fact_is_not_a_deviation(self) -> None:
        """Отсутствие данных нельзя выдавать за выполнение или провал."""
        result = kpi_module.evaluate({"plan": 100, "fact": None})
        assert result["status"] == "no_data"
        assert result["deviation"] is None


class TestKpiRegistry:
    def test_empty_registry_forbids_invention(self, store) -> None:
        content, is_error = registry.execute("kpi_list", {})
        assert not is_error
        payload = json.loads(content)
        assert payload["status"] == "empty"
        assert "не придумывай" in payload["hint"].lower()

    def test_upsert_then_list(self, store) -> None:
        registry.execute(
            "kpi_upsert",
            {"name": "Выручка", "period": "2026-09", "plan": 1000, "fact": 700,
             "unit": "тыс. ₽", "warning_pct": 10, "critical_pct": 20},
        )
        content, _ = registry.execute("kpi_list", {})
        payload = json.loads(content)
        assert payload["count"] == 1
        assert payload["critical_count"] == 1
        assert payload["kpi"][0]["status"] == "critical"

    def test_second_upsert_updates_the_same_period(self, store) -> None:
        registry.execute("kpi_upsert", {"name": "Выручка", "period": "2026-09", "plan": 1000})
        registry.execute("kpi_upsert", {"name": "Выручка", "period": "2026-09", "fact": 990})
        content, _ = registry.execute("kpi_list", {})
        payload = json.loads(content)
        assert payload["count"] == 1, "тот же показатель за тот же период — одна запись"
        assert payload["kpi"][0]["fact"] == 990

    def test_deviations_come_first(self, store) -> None:
        registry.execute("kpi_upsert", {"name": "В норме", "period": "2026-09", "plan": 100, "fact": 100})
        registry.execute("kpi_upsert", {"name": "Провал", "period": "2026-09", "plan": 100, "fact": 50})
        content, _ = registry.execute("kpi_list", {})
        assert json.loads(content)["kpi"][0]["name"] == "Провал"

    def test_inverted_thresholds_are_refused(self, store) -> None:
        with pytest.raises(ToolError, match="мягче"):
            kpi_module._kpi_upsert(
                {"name": "х", "plan": 1, "warning_pct": 30, "critical_pct": 10}
            )

    def test_upsert_needs_confirmation(self) -> None:
        assert registry.get("kpi_upsert").requires_confirmation is True
        assert registry.get("kpi_list").requires_confirmation is False


class TestProtocolBecomesTasks:
    """Список действий, оставшийся текстом в чате, назавтра исчезает."""

    PROTOCOL = {
        "title": "Планёрка по продажам",
        "held_on": "2026-09-01",
        "participants": ["Иванов", "Петров"],
        "agreements": ["Скидку сверх 15% согласовывает директор"],
        "decisions": ["Выходим на рынок Казахстана в IV квартале"],
        "actions": [
            {"title": "Подготовить КП для «Ромашки»", "assignee": "Иванов", "due_date": "2026-09-10"},
            {"title": "Собрать статистику по возвратам", "assignee": "Петров", "due_date": "2026-09-05"},
        ],
        "open_questions": ["Кто отвечает за логистику в новом регионе"],
    }

    def test_actions_land_in_the_task_registry(self, store) -> None:
        content, is_error = registry.execute("protocol_save", self.PROTOCOL)
        assert not is_error
        payload = json.loads(content)
        assert payload["counts"]["actions"] == 2
        assert len(payload["tasks_created"]) == 2

        listed, _ = registry.execute("tasks_list", {})
        tasks = json.loads(listed)
        assert tasks["count"] == 2
        assert {t["assignee"] for t in tasks["tasks"]} == {"Иванов", "Петров"}

    def test_task_remembers_its_source(self, store) -> None:
        """Через месяц должно быть видно, откуда взялось поручение."""
        registry.execute("protocol_save", self.PROTOCOL)
        listed, _ = registry.execute("tasks_list", {})
        source = json.loads(listed)["tasks"][0]["source"]
        assert "Планёрка по продажам" in source
        assert "P-0001" in source

    def test_missing_assignee_is_flagged_not_invented(self, store) -> None:
        payload = json.loads(
            registry.execute(
                "protocol_save",
                {**self.PROTOCOL, "actions": [{"title": "Что-то сделать"}]},
            )[0]
        )
        assert payload["warnings"]["without_assignee"] == ["Что-то сделать"]
        assert payload["warnings"]["without_due_date"] == ["Что-то сделать"]
        assert "проконтролировать такое нельзя" in payload["hint"]

    def test_empty_protocol_is_refused(self, store) -> None:
        content, is_error = registry.execute("protocol_save", {"title": "Ни о чём"})
        assert is_error
        assert "пуст" in content

    def test_bad_date_is_reported(self, store) -> None:
        content, is_error = registry.execute(
            "protocol_save",
            {**self.PROTOCOL, "actions": [{"title": "х", "due_date": "первого сентября"}]},
        )
        assert is_error
        assert "YYYY-MM-DD" in content

    def test_one_confirmation_for_the_whole_protocol(self) -> None:
        """Пять одинаковых карточек подряд перестают читать."""
        spec = registry.get("protocol_save")
        assert spec.requires_confirmation is True
        preview = spec.build_preview(self.PROTOCOL)
        assert "2 поручений" in preview.summary
        assert "Иванов" in preview.details["Поручения в реестр"]

    def test_preview_shouts_about_missing_fields(self) -> None:
        preview = registry.get("protocol_save").build_preview(
            {**self.PROTOCOL, "actions": [{"title": "Без всего"}]}
        )
        assert "БЕЗ ОТВЕТСТВЕННОГО" in preview.details["Поручения в реестр"]
        assert "БЕЗ СРОКА" in preview.details["Поручения в реестр"]

    def test_protocols_are_searchable(self, store) -> None:
        registry.execute("protocol_save", self.PROTOCOL)
        content, _ = registry.execute("protocol_list", {"query": "казахстан"})
        payload = json.loads(content)
        assert payload["count"] == 1
        assert payload["protocols"][0]["id"] == "P-0001"

    def test_empty_protocol_list_forbids_invention(self, store) -> None:
        payload = json.loads(registry.execute("protocol_list", {})[0])
        assert payload["status"] == "empty"
        assert "не придумывай" in payload["hint"].lower()
