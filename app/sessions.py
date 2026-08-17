"""Хранилище диалогов.

Сессии живут в памяти процесса: история содержит блоки ответов Claude вместе
с подписями блоков рассуждений, которые нужно возвращать в API без изменений.
Сериализовать их вручную рискованно, поэтому диалог не переживает перезапуск
сервера — реестр поручений и база знаний, разумеется, сохраняются на диске.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from .agent import Session
from .config import settings


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._touched: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> Session:
        with self._lock:
            self._evict_expired()
            if session_id and session_id in self._sessions:
                self._touched[session_id] = datetime.now(timezone.utc)
                return self._sessions[session_id]
            new_id = session_id or uuid.uuid4().hex
            session = Session(session_id=new_id)
            self._sessions[new_id] = session
            self._touched[new_id] = datetime.now(timezone.utc)
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def reset(self, session_id: str) -> Session:
        with self._lock:
            session = Session(session_id=session_id or uuid.uuid4().hex)
            self._sessions[session.session_id] = session
            self._touched[session.session_id] = datetime.now(timezone.utc)
            return session

    def _evict_expired(self) -> None:
        ttl = timedelta(minutes=settings.session_ttl_minutes)
        now = datetime.now(timezone.utc)
        stale = [sid for sid, seen in self._touched.items() if now - seen > ttl]
        for sid in stale:
            self._sessions.pop(sid, None)
            self._touched.pop(sid, None)


store = SessionStore()
