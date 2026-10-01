"""Поиск в «Кванте» — основной базе знаний OPERON (Qdrant)."""

from __future__ import annotations

from typing import Any

from .. import quant
from .base import ToolError, ToolSpec, registry


def _quant_search(tool_input: dict[str, Any]) -> Any:
    query = (tool_input.get("query") or "").strip()
    if not query:
        raise ToolError("Не указан поисковый запрос (query).")
    limit = min(max(int(tool_input.get("top_k") or 6), 1), 15)
    try:
        result = quant.search(query, limit=limit)
    except quant.QuantError as exc:
        raise ToolError(f"{exc} Скажи пользователю, что основная база сейчас недоступна.") from exc
    if result["status"] == "not_configured":
        raise ToolError("Квант не подключён (QDRANT_URL не задан). Ищи в kb_search и на Google Диске.")
    if result["status"] != "ok":
        result["hint"] = (
            "В Кванте по этому запросу ничего нет. Попробуй другую формулировку, затем kb_search "
            "и drive_search. Не выдумывай ответ."
        )
    else:
        result["note"] = (
            "Квант — источник правды. Ссылайся: «Квант: название, дата, ссылка». "
            "Если личный Диск говорит другое — отвечай по Кванту и назови расхождение."
        )
    return result


registry.register(
    ToolSpec(
        name="quant_search",
        description=(
            "Поиск в «Кванте» — ОСНОВНОЙ базе знаний OPERON: документы компании (бизнес-модель, "
            "продукты, партнёры, договоры, процессы, тарифы, KPI, задачи). Вызывай ПЕРВЫМ на любой "
            "вопрос о внутренних данных. Возвращает фрагменты с названием документа, датой и "
            "ссылкой — приводи их как источник. Пусто — данных нет, не домысливай."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Запрос на естественном языке."},
                "top_k": {"type": "integer", "description": "Сколько фрагментов (1–15), по умолчанию 6."},
            },
            "required": ["query"],
        },
        handler=_quant_search,
        activity="Ищу в Кванте",
    )
)
