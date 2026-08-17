"""Инструменты работы с базой знаний OPERON."""

from __future__ import annotations

from typing import Any

from ..kb import knowledge_base
from .base import ToolError, ToolSpec, registry

EMPTY_KB_HINT = (
    "База знаний OPERON пуста или не найдена: в каталоге {root} нет проиндексированных "
    "документов. Ответить по внутренним данным невозможно. Сообщи об этом пользователю "
    "и предложи положить документы в каталог базы знаний (структура описана в "
    "knowledge_base/README.md). Не придумывай содержание документов."
)


def _kb_search(tool_input: dict[str, Any]) -> Any:
    query = (tool_input.get("query") or "").strip()
    if not query:
        raise ToolError("Не указан поисковый запрос (query).")

    stats = knowledge_base.stats
    if not stats["documents"]:
        return {"status": "empty_knowledge_base", "hint": EMPTY_KB_HINT.format(root=stats["root"])}

    top_k = int(tool_input.get("top_k") or 6)
    results = knowledge_base.search(query, top_k=min(max(top_k, 1), 15), category=tool_input.get("category"))
    if not results:
        return {
            "status": "not_found",
            "query": query,
            "documents_indexed": stats["documents"],
            "available_categories": stats["categories"],
            "hint": (
                "Совпадений нет. Скажи пользователю, что во внутренней базе данных по этому "
                "запросу не найдено, и предложи уточнить формулировку или проверить другие "
                "источники. Не выдумывай ответ."
            ),
        }
    return {"status": "ok", "query": query, "results_count": len(results), "results": results}


def _kb_get_document(tool_input: dict[str, Any]) -> Any:
    doc_id = (tool_input.get("doc_id") or "").strip()
    if not doc_id:
        raise ToolError("Не указан идентификатор документа (doc_id).")
    document = knowledge_base.get_document(doc_id)
    if document is None:
        available = [d["doc_id"] for d in knowledge_base.list_documents()][:40]
        raise ToolError(
            f"Документ «{doc_id}» не найден в базе знаний. Доступные документы: {available}"
        )

    section = (tool_input.get("section") or "").strip()
    if section:
        needle = section.lower()
        parts = [c.text for c in document.chunks if needle in c.section.lower()]
        if not parts:
            sections = sorted({c.section for c in document.chunks if c.section})
            raise ToolError(
                f"В документе «{document.doc_id}» нет раздела «{section}». Разделы: {sections}"
            )
        text = "\n\n".join(parts)
    else:
        text = document.text

    truncated = len(text) > 40_000
    return {
        "status": "ok",
        **document.meta(),
        "section": section or None,
        "citation": document.citation(section),
        "truncated": truncated,
        "text": text[:40_000],
    }


def _kb_list_documents(tool_input: dict[str, Any]) -> Any:
    stats = knowledge_base.stats
    documents = knowledge_base.list_documents(tool_input.get("category"))
    if not documents:
        return {
            "status": "empty_knowledge_base" if not stats["documents"] else "not_found",
            "knowledge_base_root": stats["root"],
            "categories": stats["categories"],
            "hint": EMPTY_KB_HINT.format(root=stats["root"]) if not stats["documents"] else None,
        }
    return {
        "status": "ok",
        "documents_count": len(documents),
        "categories": stats["categories"],
        "documents": documents,
    }


registry.register(
    ToolSpec(
        name="kb_search",
        description=(
            "Полнотекстовый поиск по внутренней базе знаний OPERON (бизнес-модель, продукты, "
            "партнёры, договоры, процессы, тарифы, KPI, текущие задачи). ВСЕГДА используй этот "
            "инструмент первым, когда вопрос касается внутренних данных компании. Возвращает "
            "фрагменты документов с указанием файла, раздела и даты обновления — эти данные "
            "нужно приводить как источник. Если результатов нет, значит данных в базе нет: "
            "сообщи об этом, не домысливай."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос на естественном языке, например «тарифы для партнёров 2026».",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Сколько фрагментов вернуть (1–15). По умолчанию 6.",
                },
                "category": {
                    "type": "string",
                    "description": "Необязательный фильтр по категории или подкаталогу базы знаний.",
                },
            },
            "required": ["query"],
        },
        handler=_kb_search,
        activity="Ищу во внутренней базе знаний OPERON",
    )
)

registry.register(
    ToolSpec(
        name="kb_get_document",
        description=(
            "Читает документ базы знаний целиком или один его раздел. Используй после kb_search, "
            "когда найденного фрагмента недостаточно и нужен полный контекст (например, весь "
            "текст договора или полная таблица тарифов)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Идентификатор документа из результатов kb_search, например «tarify/tarify_2026.md».",
                },
                "section": {
                    "type": "string",
                    "description": "Необязательное название раздела; если не указано, вернётся весь документ.",
                },
            },
            "required": ["doc_id"],
        },
        handler=_kb_get_document,
        activity="Читаю документ базы знаний",
    )
)

registry.register(
    ToolSpec(
        name="kb_list_documents",
        description=(
            "Показывает состав базы знаний: список документов с категориями, владельцами и датами "
            "обновления. Используй, когда нужно понять, какие данные вообще есть, или когда поиск "
            "ничего не нашёл и надо показать пользователю доступные материалы."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Необязательный фильтр по категории или подкаталогу.",
                }
            },
        },
        handler=_kb_list_documents,
        activity="Смотрю состав базы знаний",
    )
)
