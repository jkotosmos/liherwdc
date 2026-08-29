"""Извлечение текста из офисных форматов: PDF, Word, Excel, PowerPoint.

Реальные договоры, регламенты и тарифы лежат не в markdown, а в .docx и .pdf.
Без этого модуля база знаний их просто не видела, а `drive_read` на PDF
отвечал отказом — то есть пункт ТЗ «знать проект» упирался в формат файла.

Три решения:

* **Разбор в процессе, без внешних сервисов.** Никаких конвертеров и очередей:
  на Amvera это лишний сервис, который надо поднимать и оплачивать.
* **Таблицы разворачиваются в строки.** Ячейка на строку бесполезна для
  поиска: смысл в строке целиком, поэтому Excel и таблицы Word собираются
  в « | »-разделённые строки, как это делает наш индекс для CSV.
* **Ошибка разбора — не молчание.** Битый или зашифрованный файл возвращает
  понятную причину, а не пустой текст: пустой текст выглядит как «в документе
  ничего нет» и приводит к уверенному неверному ответу.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Форматы, из которых умеем доставать текст своими силами.
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}

# Старые бинарные форматы Office библиотеками не читаются: у них другой
# контейнер. Сообщаем об этом прямо, а не «файл пуст».
LEGACY_HINT = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

MAX_CHARS = 400_000


class DocumentError(Exception):
    """Файл не удалось прочитать; текст сообщения показывается пользователю."""


def is_supported(name: str) -> bool:
    return Path(name).suffix.lower() in DOCUMENT_SUFFIXES


def legacy_alternative(name: str) -> str | None:
    """Для .doc/.xls/.ppt подсказывает, во что пересохранить."""
    return LEGACY_HINT.get(Path(name).suffix.lower())


# --- отдельные форматы ------------------------------------------------------


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover — зависит от установки
        raise DocumentError("Не установлена библиотека pypdf.") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — pypdf бросает разные типы
        raise DocumentError(f"PDF не читается: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise DocumentError(
                "PDF защищён паролем. Пришлите незащищённую копию."
            ) from None

    pages: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — одна битая страница не отменяет документ
            logger.warning("Не удалось разобрать страницу %s PDF", number)
            continue
        if text.strip():
            pages.append(f"### Страница {number}\n{text.strip()}")

    if not pages:
        raise DocumentError(
            "В PDF нет текстового слоя — вероятно, это скан. Распознавание "
            "изображений не поддерживается; пришлите текстовую версию."
        )
    return "\n\n".join(pages)


def _from_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("Не установлена библиотека python-docx.") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Файл .docx не читается: {exc}") from exc

    blocks = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    # Таблицы — часто самое ценное в договоре: сроки, суммы, условия.
    for index, table in enumerate(document.tables, start=1):
        rows = []
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            blocks.append(f"### Таблица {index}\n" + "\n".join(rows))

    if not blocks:
        raise DocumentError("Документ .docx пуст — текста в нём нет.")
    return "\n\n".join(blocks)


def _from_xlsx(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("Не установлена библиотека openpyxl.") from exc

    try:
        # data_only=True даёт посчитанные значения формул, а не сами формулы:
        # искать по «=B2*1.2» бессмысленно.
        book = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Файл .xlsx не читается: {exc}") from exc

    sheets: list[str] = []
    for sheet in book.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell).strip() for cell in row]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            sheets.append(f"### Лист: {sheet.title}\n" + "\n".join(rows))
    book.close()

    if not sheets:
        raise DocumentError("В книге .xlsx нет заполненных ячеек.")
    return "\n\n".join(sheets)


def _from_pptx(data: bytes) -> str:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("Не установлена библиотека python-pptx.") from exc

    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Файл .pptx не читается: {exc}") from exc

    slides: list[str] = []
    for number, slide in enumerate(deck.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
        if parts:
            slides.append(f"### Слайд {number}\n" + "\n".join(parts))

    if not slides:
        raise DocumentError("В презентации нет текста.")
    return "\n\n".join(slides)


_EXTRACTORS: dict[str, Any] = {
    ".pdf": _from_pdf,
    ".docx": _from_docx,
    ".xlsx": _from_xlsx,
    ".xlsm": _from_xlsx,
    ".pptx": _from_pptx,
}


# --- публичный интерфейс ----------------------------------------------------


def extract_text(data: bytes, name: str) -> str:
    """Достаёт текст из байтов файла. Формат определяется по имени."""
    suffix = Path(name).suffix.lower()

    alternative = LEGACY_HINT.get(suffix)
    if alternative:
        raise DocumentError(
            f"Формат {suffix} — устаревший бинарный Office, прочитать его нельзя. "
            f"Пересохраните файл как {alternative}."
        )

    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise DocumentError(f"Формат {suffix or '(без расширения)'} не поддерживается.")

    text = extractor(data)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n[...документ обрезан на {MAX_CHARS} символах]"
    return text


def extract_from_path(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DocumentError(f"Файл не читается: {exc}") from exc
    return extract_text(data, path.name)
