"""Индексация и поиск по базе знаний."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import DEMO_KB

from app.kb.store import KnowledgeBase
from app.kb.text import stem_ru, tokenize


@pytest.fixture(scope="module")
def demo_kb() -> KnowledgeBase:
    kb = KnowledgeBase(DEMO_KB)
    kb.ensure_fresh()
    return kb


class TestMorphology:
    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("тарифы", "тариф"),
            ("тарифов", "тариф"),
            ("тарифами", "тариф"),
            ("партнерами", "партнер"),
            ("партнеров", "партнер"),
            ("договора", "договор"),
            ("процессов", "процесс"),
        ],
    )
    def test_word_forms_collapse_to_one_stem(self, word: str, expected: str) -> None:
        assert stem_ru(word) == expected

    def test_stopwords_and_short_words_dropped(self) -> None:
        tokens = tokenize("Какие тарифы у партнёров в 2026 году?")
        assert "тариф" in tokens
        assert "партнер" in tokens
        assert "2026" in tokens
        assert "в" not in tokens and "у" not in tokens

    def test_yo_is_normalized(self) -> None:
        assert tokenize("партнёр") == tokenize("партнер")


class TestIndexing:
    def test_demo_documents_are_indexed(self, demo_kb: KnowledgeBase) -> None:
        stats = demo_kb.stats
        assert stats["documents"] >= 3
        assert stats["chunks"] >= 3

    def test_front_matter_metadata_is_read(self, demo_kb: KnowledgeBase) -> None:
        doc = demo_kb.get_document("produkty/platforma.md")
        assert doc is not None
        assert doc.title == "Платформа Operon Core (демо-данные)"
        assert doc.category == "продукты"
        assert doc.updated == "2026-02-10"

    def test_markdown_is_split_into_sections(self, demo_kb: KnowledgeBase) -> None:
        doc = demo_kb.get_document("produkty/platforma.md")
        sections = {chunk.section for chunk in doc.chunks}
        assert any("Ограничения" in section for section in sections)

    def test_csv_becomes_searchable_text(self, demo_kb: KnowledgeBase) -> None:
        doc = demo_kb.get_document("tarify/tarify-2026.csv")
        assert doc is not None
        assert "Партнёрский" in doc.text
        assert "cena: 1800" in doc.text

    def test_templates_directory_is_excluded(self) -> None:
        """Пустые шаблоны не должны попадать в ответы как факты."""
        kb = KnowledgeBase(Path(__file__).resolve().parent.parent / "knowledge_base")
        kb.ensure_fresh()
        assert all("_templates" not in doc["doc_id"] for doc in kb.list_documents())

    def test_index_rebuilds_when_files_change(self, tmp_path: Path) -> None:
        kb = KnowledgeBase(tmp_path)
        assert kb.stats["documents"] == 0

        (tmp_path / "note.md").write_text("# Заметка\n\nСодержание про логистику.", encoding="utf-8")
        assert kb.stats["documents"] == 1

        (tmp_path / "note2.md").write_text("# Вторая\n\nЕщё текст.", encoding="utf-8")
        assert kb.stats["documents"] == 2


class TestSearch:
    def test_finds_document_by_inflected_query(self, demo_kb: KnowledgeBase) -> None:
        """Запрос в другом падеже и числе всё равно должен находить документ."""
        results = demo_kb.search("партнёрские тарифы")
        assert results
        assert any("tarify" in hit["doc_id"] for hit in results)

    def test_result_carries_citation_fields(self, demo_kb: KnowledgeBase) -> None:
        hit = demo_kb.search("MRR выручка план")[0]
        assert hit["doc_id"]
        assert hit["updated"]
        assert "База знаний OPERON" in hit["citation"]

    def test_no_match_returns_empty_not_garbage(self, demo_kb: KnowledgeBase) -> None:
        assert demo_kb.search("криптовалютный майнинг на луне") == []

    def test_category_filter_narrows_results(self, demo_kb: KnowledgeBase) -> None:
        results = demo_kb.search("Operon Core", category="kpi")
        assert all("kpi" in hit["doc_id"] or hit["category"] == "kpi" for hit in results)

    def test_lookup_by_stem_without_extension(self, demo_kb: KnowledgeBase) -> None:
        assert demo_kb.get_document("platforma") is not None
