"""База знаний OPERON: загрузка документов, нарезка на фрагменты и BM25-поиск.

Индекс строится в памяти при старте и пересобирается, когда меняются файлы
в каталоге базы знаний. Каждый фрагмент хранит ссылку на документ и раздел,
чтобы агент мог указать источник ответа.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .text import tokenize

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml"}

# Каталоги, которые не индексируются: шаблоны — это пустые формы,
# и агент не должен принимать их за фактические данные.
EXCLUDED_DIRS = {"_templates", "_archive", ".git", "__pycache__", ".obsidian"}

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$", re.MULTILINE)

MAX_CHUNK_CHARS = 1800
CHUNK_OVERLAP_CHARS = 200
MAX_DOC_CHARS = 60_000


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    section: str
    text: str
    _tokens: list[str] | None = field(default=None, repr=False, compare=False)

    @property
    def tokens(self) -> list[str]:
        if self._tokens is None:
            self._tokens = tokenize(f"{self.section}\n{self.text}")
        return self._tokens


@dataclass
class Document:
    doc_id: str
    path: Path
    title: str
    category: str
    updated: str
    owner: str
    source_url: str
    text: str
    chunks: list[Chunk]

    def citation(self, section: str = "") -> str:
        parts = [f"База знаний OPERON → {self.doc_id}"]
        if section:
            parts.append(f"раздел «{section}»")
        if self.updated:
            parts.append(f"обновлён {self.updated}")
        return ", ".join(parts)

    def meta(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "category": self.category,
            "updated": self.updated,
            "owner": self.owner,
            "source_url": self.source_url,
            "chars": len(self.text),
        }


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, raw[match.end():]


def _csv_to_text(raw: str, delimiter: str) -> str:
    try:
        rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    except csv.Error:
        return raw
    if not rows:
        return ""
    header = rows[0]
    lines = [" | ".join(header), "-" * 40]
    for row in rows[1:]:
        pairs = [
            f"{header[i] if i < len(header) else f'col{i}'}: {value}"
            for i, value in enumerate(row)
            if value.strip()
        ]
        if pairs:
            lines.append("; ".join(pairs))
    return "\n".join(lines)


def _load_raw(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _csv_to_text(raw, ",")
    if suffix == ".tsv":
        return _csv_to_text(raw, "\t")
    if suffix == ".json":
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return raw
    return raw


def _split_long(text: str) -> list[str]:
    """Режет длинный раздел по абзацам с небольшим перекрытием."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts: list[str] = []
    buffer = ""
    for paragraph in re.split(r"\n\s*\n", text):
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) > MAX_CHUNK_CHARS and buffer:
            parts.append(buffer.strip())
            tail = buffer[-CHUNK_OVERLAP_CHARS:]
            buffer = f"{tail}\n\n{paragraph}"
        else:
            buffer = candidate
    if buffer.strip():
        parts.append(buffer.strip())
    return parts or [text[:MAX_CHUNK_CHARS]]


def _chunk_markdown(doc_id: str, text: str) -> list[Chunk]:
    headings = list(HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []

    if not headings:
        sections.append(("", text))
    else:
        preamble = text[: headings[0].start()].strip()
        if preamble:
            sections.append(("", preamble))
        stack: list[str] = []
        for i, match in enumerate(headings):
            level = len(match.group(1))
            title = match.group(2).strip()
            stack = stack[: level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            body = text[match.end():end].strip()
            path = " / ".join(p for p in stack if p)
            if body:
                sections.append((path, body))

    chunks: list[Chunk] = []
    for section, body in sections:
        for part in _split_long(body):
            if not part.strip():
                continue
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    chunk_id=f"{doc_id}#{len(chunks)}",
                    section=section,
                    text=part.strip(),
                )
            )
    return chunks


class KnowledgeBase:
    """BM25-индекс по локальным документам с ленивой пересборкой."""

    K1 = 1.5
    B = 0.75

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._documents: dict[str, Document] = {}
        self._chunks: list[Chunk] = []
        self._doc_freq: Counter[str] = Counter()
        self._chunk_tokens: list[Counter[str]] = []
        self._chunk_len: list[int] = []
        self._avg_len: float = 1.0
        self._fingerprint: tuple | None = None
        self._built_at: datetime | None = None

    # --- построение индекса -------------------------------------------------

    def _scan_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        files: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            rel_parts = path.relative_to(self.root).parts
            if any(part in EXCLUDED_DIRS or part.startswith(".") for part in rel_parts):
                continue
            if path.name.upper() == "README.MD" and len(rel_parts) == 1:
                continue
            files.append(path)
        return files

    def _fingerprint_of(self, files: list[Path]) -> tuple:
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)

    def ensure_fresh(self) -> None:
        files = self._scan_files()
        fingerprint = self._fingerprint_of(files)
        if fingerprint == self._fingerprint:
            return
        with self._lock:
            if fingerprint == self._fingerprint:
                return
            self._build(files)
            self._fingerprint = fingerprint

    def _build(self, files: list[Path]) -> None:
        documents: dict[str, Document] = {}
        chunks: list[Chunk] = []

        for path in files:
            rel = path.relative_to(self.root)
            doc_id = rel.as_posix()
            raw = _load_raw(path)
            meta, body = _parse_front_matter(raw)
            body = body.strip()
            if not body:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            title = meta.get("title") or _first_heading(body) or rel.stem.replace("_", " ")
            category = meta.get("category") or (rel.parts[0] if len(rel.parts) > 1 else "общее")
            document = Document(
                doc_id=doc_id,
                path=path,
                title=title,
                category=category,
                updated=meta.get("updated") or mtime.date().isoformat(),
                owner=meta.get("owner", ""),
                source_url=meta.get("source") or meta.get("source_url", ""),
                text=body,
                chunks=[],
            )
            document.chunks = _chunk_markdown(doc_id, body)
            documents[doc_id] = document
            chunks.extend(document.chunks)

        doc_freq: Counter[str] = Counter()
        chunk_tokens: list[Counter[str]] = []
        chunk_len: list[int] = []
        for chunk in chunks:
            counts = Counter(chunk.tokens)
            chunk_tokens.append(counts)
            chunk_len.append(sum(counts.values()) or 1)
            doc_freq.update(counts.keys())

        self._documents = documents
        self._chunks = chunks
        self._doc_freq = doc_freq
        self._chunk_tokens = chunk_tokens
        self._chunk_len = chunk_len
        self._avg_len = (sum(chunk_len) / len(chunk_len)) if chunk_len else 1.0
        self._built_at = datetime.now(timezone.utc)

    # --- публичный API ------------------------------------------------------

    @property
    def stats(self) -> dict:
        self.ensure_fresh()
        categories = sorted({d.category for d in self._documents.values()})
        return {
            "root": str(self.root),
            "exists": self.root.exists(),
            "documents": len(self._documents),
            "chunks": len(self._chunks),
            "categories": categories,
            "built_at": self._built_at.isoformat() if self._built_at else None,
        }

    def list_documents(self, category: str | None = None) -> list[dict]:
        self.ensure_fresh()
        docs = self._documents.values()
        if category:
            needle = category.strip().lower()
            docs = [d for d in docs if needle in d.category.lower() or needle in d.doc_id.lower()]
        return sorted((d.meta() for d in docs), key=lambda m: m["doc_id"])

    def get_document(self, doc_id: str) -> Document | None:
        self.ensure_fresh()
        if doc_id in self._documents:
            return self._documents[doc_id]
        # Мягкий поиск: пользователь мог указать имя без расширения или каталога.
        needle = doc_id.strip().lower().lstrip("/")
        for key, doc in self._documents.items():
            key_l = key.lower()
            if key_l == needle or key_l.rsplit(".", 1)[0] == needle or Path(key_l).stem == needle:
                return doc
        return None

    def search(self, query: str, top_k: int = 6, category: str | None = None) -> list[dict]:
        self.ensure_fresh()
        if not self._chunks:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        total = len(self._chunks)
        idf: dict[str, float] = {}
        for token in set(query_tokens):
            df = self._doc_freq.get(token, 0)
            idf[token] = math.log(1 + (total - df + 0.5) / (df + 0.5))

        needle = category.strip().lower() if category else None
        scored: list[tuple[float, int]] = []
        for i, chunk in enumerate(self._chunks):
            document = self._documents.get(chunk.doc_id)
            if document is None:
                continue
            if needle and needle not in document.category.lower() and needle not in document.doc_id.lower():
                continue
            counts = self._chunk_tokens[i]
            length = self._chunk_len[i]
            score = 0.0
            for token in query_tokens:
                tf = counts.get(token, 0)
                if not tf:
                    continue
                denom = tf + self.K1 * (1 - self.B + self.B * length / self._avg_len)
                score += idf[token] * (tf * (self.K1 + 1)) / denom
            if score <= 0:
                continue
            # Небольшой бонус за совпадение в заголовке документа или раздела.
            title_tokens = set(tokenize(f"{document.title} {chunk.section}"))
            overlap = len(title_tokens & set(query_tokens))
            score *= 1.0 + 0.12 * overlap
            scored.append((score, i))

        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[dict] = []
        for score, i in scored[: max(1, top_k)]:
            chunk = self._chunks[i]
            document = self._documents[chunk.doc_id]
            results.append(
                {
                    "doc_id": document.doc_id,
                    "title": document.title,
                    "category": document.category,
                    "section": chunk.section,
                    "updated": document.updated,
                    "owner": document.owner,
                    "source_url": document.source_url,
                    "citation": document.citation(chunk.section),
                    "score": round(score, 3),
                    "text": chunk.text[:MAX_CHUNK_CHARS],
                }
            )
        return results


def _first_heading(text: str) -> str:
    match = HEADING_RE.search(text)
    return match.group(2).strip() if match else ""
