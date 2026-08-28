"""Реестр показателей: план, факт и пороги реагирования.

ТЗ требует «анализировать показатели, выявлять отклонения, риски и узкие
места». Модель умеет считать проценты, но не умеет знать, какое отклонение
считается тревожным именно в этой компании: 5% по марже — катастрофа, 5% по
трафику — шум. Поэтому порог задаёт человек, а агент лишь применяет его.

Отсюда главное свойство реестра: **вывод «отклонение» здесь считается кодом,
а не моделью**. Модель получает готовый статус (ok / warning / critical) и
объясняет его — но не решает, наступил ли он. Так «выявление отклонений»
перестаёт зависеть от формулировок промпта.

Направление показателя важно не меньше порога: для выручки плохо «меньше
плана», для стоимости привлечения — «больше плана».
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..config import settings
from ..storage import read_json, update_json
from .base import Preview, ToolError, ToolSpec, registry

# Куда смотрит показатель: чем больше — тем лучше, или наоборот.
DIRECTIONS = ("higher_is_better", "lower_is_better")

STATUS_RU = {
    "ok": "в норме",
    "warning": "отклонение",
    "critical": "критично",
    "no_data": "нет факта",
}


def _now() -> str:
    return datetime.now(settings.tz).isoformat(timespec="seconds")


def _load() -> list[dict[str, Any]]:
    data = read_json(settings.kpi_path, {"kpi": []})
    return data.get("kpi", []) if isinstance(data, dict) else []


def _number(value: Any, field: str) -> float:
    if value is None or value == "":
        raise ToolError(f"Поле {field}: нужно число.")
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Поле {field}: «{value}» не число.") from exc


def _period(value: str) -> str:
    """Период показателя: 2026-09 (месяц) или 2026-09-30 (дата среза)."""
    raw = (value or "").strip()
    if not raw:
        return datetime.now(settings.tz).strftime("%Y-%m")
    try:
        if len(raw) == 7:
            date.fromisoformat(raw + "-01")
            return raw
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ToolError(f"Период «{value}»: ожидается YYYY-MM или YYYY-MM-DD.") from exc


def evaluate(kpi: dict[str, Any]) -> dict[str, Any]:
    """Считает отклонение и статус. Ядро всего модуля — здесь и только здесь.

    Никакой интерпретации: есть план, факт, порог и направление — статус
    выводится арифметикой. Модель этот вывод не переопределяет.
    """
    view = dict(kpi)
    plan = kpi.get("plan")
    fact = kpi.get("fact")

    if fact is None or plan is None:
        view.update(
            {"deviation": None, "deviation_pct": None, "status": "no_data",
             "status_ru": STATUS_RU["no_data"]}
        )
        return view

    deviation = fact - plan
    view["deviation"] = round(deviation, 4)
    view["deviation_pct"] = round(deviation / plan * 100, 2) if plan else None

    lower_is_better = kpi.get("direction") == "lower_is_better"
    # «Насколько плохо» — со знаком: положительное значение всегда означает
    # отставание от плана, в какую бы сторону показатель ни смотрел.
    shortfall_pct = -(view["deviation_pct"] or 0) if not lower_is_better else (view["deviation_pct"] or 0)
    view["shortfall_pct"] = round(shortfall_pct, 2)

    warning = kpi.get("warning_pct")
    critical = kpi.get("critical_pct")
    if critical is not None and shortfall_pct >= critical:
        status = "critical"
    elif warning is not None and shortfall_pct >= warning:
        status = "warning"
    else:
        status = "ok"
    view["status"] = status
    view["status_ru"] = STATUS_RU[status]
    return view


def load_deviations() -> list[dict[str, Any]]:
    """Показатели со статусом хуже нормы — для сводки и анализа."""
    return [v for v in (evaluate(k) for k in _load()) if v["status"] in {"warning", "critical"}]


def _kpi_list(tool_input: dict[str, Any]) -> Any:
    items = _load()
    if not items:
        return {
            "status": "empty",
            "hint": (
                "Реестр показателей пуст. Не придумывай KPI и их значения. "
                "Предложи завести показатель через kpi_upsert: нужны название, "
                "план, единица измерения, направление и пороги реагирования."
            ),
        }

    result = [evaluate(k) for k in items]

    period = (tool_input.get("period") or "").strip()
    if period:
        result = [k for k in result if k.get("period") == period]
    if tool_input.get("deviations_only"):
        result = [k for k in result if k["status"] in {"warning", "critical"}]
    query = (tool_input.get("query") or "").strip().lower()
    if query:
        result = [k for k in result if query in (k.get("name") or "").lower()]

    if not result:
        return {"status": "not_found", "total_in_registry": len(items),
                "hint": "Под фильтр ничего не подошло. Не подставляй другие показатели."}

    order = {"critical": 0, "warning": 1, "no_data": 2, "ok": 3}
    result.sort(key=lambda k: (order.get(k["status"], 9), k.get("name", "")))
    return {
        "status": "ok",
        "count": len(result),
        "critical_count": sum(1 for k in result if k["status"] == "critical"),
        "warning_count": sum(1 for k in result if k["status"] == "warning"),
        "hint": (
            "Статус посчитан по заданным порогам, а не оценкой на глаз. "
            "Объясняй причину отклонения и предлагай действие, но не меняй сам вывод."
        ),
        "kpi": result,
    }


def _kpi_upsert(tool_input: dict[str, Any]) -> Any:
    name = (tool_input.get("name") or "").strip()
    if not name:
        raise ToolError("Не указано название показателя (name).")

    period = _period(tool_input.get("period", ""))
    direction = (tool_input.get("direction") or "higher_is_better").strip()
    if direction not in DIRECTIONS:
        raise ToolError(f"Направление «{direction}» неизвестно. Допустимо: {list(DIRECTIONS)}")

    plan = _number(tool_input["plan"], "plan") if tool_input.get("plan") is not None else None
    fact = _number(tool_input["fact"], "fact") if tool_input.get("fact") is not None else None
    warning = (
        _number(tool_input["warning_pct"], "warning_pct")
        if tool_input.get("warning_pct") is not None
        else 10.0
    )
    critical = (
        _number(tool_input["critical_pct"], "critical_pct")
        if tool_input.get("critical_pct") is not None
        else 20.0
    )
    if critical < warning:
        raise ToolError(
            f"Критический порог ({critical}%) не может быть мягче порога внимания ({warning}%)."
        )

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        items = data.setdefault("kpi", [])
        for item in items:
            if item.get("name") == name and item.get("period") == period:
                before = dict(item)
                if plan is not None:
                    item["plan"] = plan
                if fact is not None:
                    item["fact"] = fact
                item["direction"] = direction
                item["warning_pct"] = warning
                item["critical_pct"] = critical
                if tool_input.get("unit"):
                    item["unit"] = tool_input["unit"].strip()
                if tool_input.get("source"):
                    item["source"] = tool_input["source"].strip()
                item["updated_at"] = _now()
                return {"created": False, "before": before, "after": dict(item)}

        item = {
            "name": name,
            "period": period,
            "plan": plan,
            "fact": fact,
            "unit": (tool_input.get("unit") or "").strip(),
            "direction": direction,
            "warning_pct": warning,
            "critical_pct": critical,
            "source": (tool_input.get("source") or "").strip(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        items.append(item)
        return {"created": True, "before": None, "after": dict(item)}

    changed = update_json(settings.kpi_path, {"kpi": []}, mutate)
    return {
        "status": "created" if changed["created"] else "updated",
        "kpi": evaluate(changed["after"]),
        "previous": evaluate(changed["before"]) if changed["before"] else None,
    }


def _preview_upsert(tool_input: dict[str, Any]) -> Preview:
    name = tool_input.get("name", "")
    period = tool_input.get("period") or "текущий месяц"
    return Preview(
        title="Записать показатель",
        summary=f"«{name}» за {period}: план {tool_input.get('plan', '—')}, "
        f"факт {tool_input.get('fact', '—')}",
        details={
            "Показатель": name,
            "Период": period,
            "План": tool_input.get("plan", "—"),
            "Факт": tool_input.get("fact", "—"),
            "Единица": tool_input.get("unit") or "—",
            "Направление": (
                "больше — лучше"
                if (tool_input.get("direction") or "higher_is_better") == "higher_is_better"
                else "меньше — лучше"
            ),
            "Порог внимания": f"{tool_input.get('warning_pct', 10)}%",
            "Критический порог": f"{tool_input.get('critical_pct', 20)}%",
            "Источник": tool_input.get("source") or "—",
        },
    )


registry.register(
    ToolSpec(
        name="kpi_list",
        description=(
            "Показатели компании с планом, фактом и статусом отклонения. Статус считается "
            "по заранее заданным порогам, а не оценкой: ok / warning / critical. Используй "
            "для вопросов о результатах, отклонениях, рисках и узких местах. Если реестр "
            "пуст — так и скажи, показатели не выдумывай."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Период: YYYY-MM или YYYY-MM-DD."},
                "deviations_only": {
                    "type": "boolean",
                    "description": "Только те, где статус хуже нормы.",
                },
                "query": {"type": "string", "description": "Фильтр по названию показателя."},
            },
        },
        handler=_kpi_list,
        activity="Смотрю показатели",
    )
)

registry.register(
    ToolSpec(
        name="kpi_upsert",
        description=(
            "Заводит показатель или записывает его факт за период. Пороги задаёт "
            "пользователь: warning_pct и critical_pct — это отставание от плана в "
            "процентах, при котором показатель считается отклонившимся. Не придумывай "
            "пороги и значения сам — спроси. Выполняется после подтверждения."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Название показателя."},
                "period": {"type": "string", "description": "YYYY-MM или YYYY-MM-DD. По умолчанию текущий месяц."},
                "plan": {"type": "number", "description": "Плановое значение."},
                "fact": {"type": "number", "description": "Фактическое значение."},
                "unit": {"type": "string", "description": "Единица измерения: ₽, шт., %, дн."},
                "direction": {
                    "type": "string",
                    "enum": list(DIRECTIONS),
                    "description": (
                        "higher_is_better — плохо, когда факт ниже плана (выручка); "
                        "lower_is_better — плохо, когда выше (стоимость привлечения)."
                    ),
                },
                "warning_pct": {"type": "number", "description": "Отставание в %, с которого это отклонение. По умолчанию 10."},
                "critical_pct": {"type": "number", "description": "Отставание в %, с которого это критично. По умолчанию 20."},
                "source": {"type": "string", "description": "Откуда взяты цифры."},
            },
            "required": ["name"],
        },
        handler=_kpi_upsert,
        requires_confirmation=True,
        preview=_preview_upsert,
        activity="Записываю показатель",
    )
)
