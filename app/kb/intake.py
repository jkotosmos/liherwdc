"""Приём документов в базу знаний: проверить, сохранить, переиндексировать.

Файл, присланный боту, — это изменение данных, поэтому он не попадает в базу
молча: сначала он разбирается, показывается человеку и ждёт подтверждения.
Так же, как событие календаря или поручение.

Отдельный модуль, а не пара строк в боте, по одной причине: имя файла
приходит снаружи. Его нельзя просто подставить в путь — «../../etc/passwd»
и «CON.txt» приезжают из того же поля, что и «договор.docx».
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .documents import DOCUMENT_SUFFIXES, DocumentError, extract_text, legacy_alternative
from .store import TEXT_SUFFIXES

logger = logging.getLogger(__name__)

# Ограничение Telegram на скачивание ботом — 20 МБ; берём с запасом вниз.
MAX_FILE_BYTES = 20 * 1024 * 1024

ACCEPTED = TEXT_SUFFIXES | DOCUMENT_SUFFIXES

# Всё, кроме букв, цифр, пробела, точки, дефиса и подчёркивания.
_UNSAFE = re.compile(r"[^\w .\-]", re.UNICODE)


class IntakeError(Exception):
    """Причина отказа, которую можно показать пользователю целиком."""


@dataclass
class Prepared:
    """Разобранный документ, ожидающий решения человека."""

    original_name: str
    safe_name: str
    category: str
    size: int
    characters: int
    preview: str
    data: bytes

    @property
    def relative_path(self) -> str:
        return f"{self.category}/{self.safe_name}" if self.category else self.safe_name


def safe_filename(name: str) -> str:
    """Делает из присланного имени безопасное имя файла.

    Имя приходит извне, поэтому здесь отбрасывается всё, что могло бы увести
    запись за пределы каталога базы знаний.
    """
    # Берём только последний сегмент: «../../etc/passwd» превращается в «passwd».
    base = Path(str(name or "").replace("\\", "/")).name
    base = unicodedata.normalize("NFC", base).strip().strip(".")
    base = _UNSAFE.sub("_", base)
    base = re.sub(r"_{2,}", "_", base).strip("_ ")

    if not base:
        raise IntakeError("Не удалось разобрать имя файла. Переименуйте его и пришлите заново.")

    stem, _, suffix = base.rpartition(".")
    if not stem:  # имя вида «.docx» — расширение без названия
        raise IntakeError("У файла нет имени, только расширение.")
    # Windows не позволяет такие имена; на диске Amvera это Linux, но
    # переносимость файлов базы знаний дороже экономии двух строк.
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", stem.upper()):
        stem = f"{stem}_"
    return f"{stem[:120]}.{suffix.lower()}"


def safe_category(name: str) -> str:
    """Подкаталог базы знаний. Пустая строка — корень."""
    raw = str(name or "").strip().strip("/")
    if not raw:
        return ""
    part = Path(raw.replace("\\", "/")).name
    part = _UNSAFE.sub("_", unicodedata.normalize("NFC", part)).strip("_ .")
    return part[:60]


def prepare(data: bytes, name: str, category: str = "") -> Prepared:
    """Проверяет присланный файл и достаёт из него текст. Ничего не сохраняет."""
    if not data:
        raise IntakeError("Файл пустой.")
    if len(data) > MAX_FILE_BYTES:
        raise IntakeError(
            f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ — Telegram не отдаёт "
            "такие ботам. Разделите его или загрузите через панель Amvera."
        )

    safe = safe_filename(name)
    suffix = Path(safe).suffix.lower()

    alternative = legacy_alternative(safe)
    if alternative:
        raise IntakeError(
            f"Формат {suffix} — устаревший бинарный Office, прочитать его нельзя. "
            f"Пересохраните файл как {alternative}."
        )
    if suffix not in ACCEPTED:
        raise IntakeError(
            f"Формат {suffix or '(без расширения)'} в базу знаний не принимается. "
            f"Подходят: {', '.join(sorted(ACCEPTED))}."
        )

    # Разбираем сразу: класть в базу файл, из которого не достать текст,
    # значит получить документ, который никогда не найдётся поиском.
    if suffix in DOCUMENT_SUFFIXES:
        try:
            text = extract_text(data, safe)
        except DocumentError as exc:
            raise IntakeError(str(exc)) from exc
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        if not text.strip():
            raise IntakeError("В файле нет текста.")

    preview = "\n".join(line for line in text.splitlines() if line.strip())[:400]
    return Prepared(
        original_name=str(name),
        safe_name=safe,
        category=safe_category(category),
        size=len(data),
        characters=len(text),
        preview=preview,
        data=data,
    )


def save(prepared: Prepared) -> Path:
    """Кладёт файл в базу знаний и просит индекс перечитать каталог."""
    root = settings.kb_dir
    target_dir = root / prepared.category if prepared.category else root
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / prepared.safe_name
    # Одноимённый файл не затираем молча: прежняя версия могла быть нужна.
    if path.exists():
        stem, suffix = path.stem, path.suffix
        for number in range(2, 100):
            candidate = target_dir / f"{stem}-{number}{suffix}"
            if not candidate.exists():
                path = candidate
                break

    path.write_bytes(prepared.data)

    # Проверка на всякий случай: путь обязан остаться внутри базы знаний.
    if root.resolve() not in path.resolve().parents:
        path.unlink(missing_ok=True)
        raise IntakeError("Путь файла вышел за пределы базы знаний — сохранение отменено.")

    from . import knowledge_base

    knowledge_base.ensure_fresh()
    logger.info("В базу знаний добавлен документ %s (%s символов)", path, prepared.characters)
    return path
