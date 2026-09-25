"""Приём документов: имя файла приходит снаружи, и это главное здесь.

«../../etc/passwd» и «договор.docx» приезжают из одного и того же поля
Telegram. Всё остальное в этом модуле — удобство, а вот проверка имени
и разбор до сохранения — то, ради чего он отдельный.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import settings
from app.kb import intake
from app.kb.intake import IntakeError

from test_documents import make_docx, make_xlsx


@pytest.fixture
def kb_root(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "settings", replace(settings, kb_dir=tmp_path))
    from app.kb.store import KnowledgeBase

    kb = KnowledgeBase(root=tmp_path)
    monkeypatch.setattr("app.kb.knowledge_base", kb, raising=False)
    return tmp_path


class TestFilenameSafety:
    """Имя файла — это ввод извне, а не подпись под документом."""

    @pytest.mark.parametrize(
        ("sent", "expected"),
        [
            ("договор.docx", "договор.docx"),
            ("../../etc/passwd.md", "passwd.md"),
            ("..\\..\\windows\\system32\\cfg.json", "cfg.json"),
            ("/absolute/path/тариф.xlsx", "тариф.xlsx"),
            ("файл с пробелами.pdf", "файл с пробелами.pdf"),
            # Идущие подряд недопустимые знаки схлопываются в одно подчёркивание.
            ("плохой:*имя?.md", "плохой_имя_.md"),
            ("ДОГОВОР.DOCX", "ДОГОВОР.docx"),
        ],
    )
    def test_dangerous_names_are_defused(self, sent: str, expected: str) -> None:
        assert intake.safe_filename(sent) == expected

    def test_traversal_cannot_escape_the_base(self, kb_root) -> None:
        prepared = intake.prepare("# note\n\ntext".encode(), "../../../evil.md")
        path = intake.save(prepared)
        assert kb_root.resolve() in path.resolve().parents

    def test_reserved_windows_names_are_renamed(self) -> None:
        assert intake.safe_filename("CON.txt") == "CON_.txt"
        assert intake.safe_filename("com1.md") == "com1_.md"

    def test_name_without_stem_is_refused(self) -> None:
        with pytest.raises(IntakeError):
            intake.safe_filename(".docx")

    def test_category_is_a_single_safe_segment(self) -> None:
        assert intake.safe_category("договоры") == "договоры"
        assert intake.safe_category("../../etc") == "etc"
        assert intake.safe_category("") == ""
        assert intake.safe_category("a/b/c") == "c"


class TestAcceptance:
    def test_docx_is_parsed_before_saving(self, kb_root) -> None:
        """Файл, из которого не достать текст, в базе бесполезен — он не найдётся."""
        prepared = intake.prepare(make_docx(), "договор.docx", category="договоры")
        assert prepared.characters > 0
        assert "Договор поставки" in prepared.preview
        assert prepared.relative_path == "договоры/договор.docx"

    def test_xlsx_is_accepted(self, kb_root) -> None:
        prepared = intake.prepare(make_xlsx(), "тарифы.xlsx")
        assert "Тарифы" in prepared.preview

    def test_markdown_is_accepted(self, kb_root) -> None:
        prepared = intake.prepare("# Регламент\n\nСкидки до 15%".encode(), "регламент.md")
        assert "Регламент" in prepared.preview

    def test_unreadable_pdf_is_refused_with_reason(self, kb_root) -> None:
        from test_documents import make_pdf

        with pytest.raises(IntakeError, match="скан"):
            intake.prepare(make_pdf(), "скан.pdf")

    def test_unsupported_format_names_what_is_accepted(self, kb_root) -> None:
        with pytest.raises(IntakeError, match=r"\.docx"):
            intake.prepare(b"PK\x03\x04", "архив.zip")

    def test_legacy_office_suggests_the_new_format(self, kb_root) -> None:
        with pytest.raises(IntakeError, match="Пересохраните"):
            intake.prepare(b"data", "старый.doc")

    def test_empty_file_is_refused(self, kb_root) -> None:
        with pytest.raises(IntakeError, match="пустой"):
            intake.prepare(b"", "пусто.md")

    def test_oversized_file_is_refused(self, kb_root) -> None:
        with pytest.raises(IntakeError, match="МБ"):
            intake.prepare(b"x" * (intake.MAX_FILE_BYTES + 1), "большой.md")


class TestSaving:
    def test_saved_document_becomes_searchable(self, kb_root, monkeypatch) -> None:
        from app.kb.store import KnowledgeBase

        kb = KnowledgeBase(root=kb_root)
        monkeypatch.setattr("app.kb.knowledge_base", kb, raising=False)

        prepared = intake.prepare(make_docx(), "поставка.docx", category="договоры")
        intake.save(prepared)

        kb.ensure_fresh()
        results = kb.search("срок оплаты")
        assert results, "принятый документ обязан находиться поиском"

    def test_same_name_does_not_overwrite(self, kb_root) -> None:
        """Прежняя версия договора может быть нужна — молча затирать нельзя."""
        first = intake.save(intake.prepare("# a\n\nпервый".encode(), "версия.md"))
        second = intake.save(intake.prepare("# b\n\nвторой".encode(), "версия.md"))

        assert first != second
        assert first.exists() and second.exists()
        assert first.read_bytes() != second.read_bytes()

    def test_category_creates_a_subdirectory(self, kb_root) -> None:
        path = intake.save(intake.prepare("# x\n\nтекст".encode(), "файл.md", category="тарифы"))
        assert path.parent.name == "тарифы"
