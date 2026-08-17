"""Локальная база знаний проекта OPERON."""

from ..config import settings
from .store import Chunk, Document, KnowledgeBase

knowledge_base = KnowledgeBase(settings.kb_dir)

__all__ = ["Chunk", "Document", "KnowledgeBase", "knowledge_base"]
