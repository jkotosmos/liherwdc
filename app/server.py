"""HTTP-слой: чат по SSE, подтверждение действий, статус интеграций."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import agent
from .config import BASE_DIR, settings
from .integrations import google_client
from .kb import knowledge_base
from .sessions import store
from .tools import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("operon")

STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _startup_report()
    yield


app = FastAPI(title=f"Ассистент {settings.org_name}", version="1.0.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    session_id: str | None = None


class Decision(BaseModel):
    tool_use_id: str
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class ConfirmRequest(BaseModel):
    session_id: str
    decisions: list[Decision]


class ResetRequest(BaseModel):
    session_id: str | None = None


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    """Оборачивает поток событий агента в Server-Sent Events."""
    try:
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:  # noqa: BLE001 — иначе клиент зависнет на открытом стриме
        logger.exception("Сбой в потоке агента")
        payload = {
            "type": "error",
            "message": f"Внутренняя ошибка агента: {exc.__class__.__name__}: {exc}",
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'stop': 'error'}, ensure_ascii=False)}\n\n"


def _stream_response(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    session = store.get_or_create(request.session_id)

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": session.session_id}
        yield from agent.send_user_message(session, request.message)

    return _stream_response(events())


@app.post("/api/confirm")
def confirm(request: ConfirmRequest) -> StreamingResponse:
    session = store.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена — начните новый диалог.")
    if not session.awaiting_confirmation:
        raise HTTPException(status_code=409, detail="Нет действий, ожидающих подтверждения.")

    decisions = {d.tool_use_id: d.decision for d in request.decisions}
    comments = {d.tool_use_id: d.comment for d in request.decisions if d.comment}

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": session.session_id}
        yield from agent.resume_with_decisions(session, decisions, comments)

    return _stream_response(events())


@app.post("/api/session/reset")
def reset(request: ResetRequest) -> dict[str, str]:
    session = store.reset(request.session_id or "")
    return {"session_id": session.session_id}


@app.get("/api/status")
def status() -> dict[str, Any]:
    google = google_client.status()
    kb = knowledge_base.stats
    return {
        "org": settings.org_name,
        "model": settings.model,
        "effort": settings.effort,
        "timezone": settings.timezone_name,
        "knowledge_base": kb,
        "google": google,
        "web_search": settings.web_search_enabled,
        "tools": [
            {
                "name": spec.name,
                "requires_confirmation": spec.requires_confirmation,
                "activity": spec.activity,
            }
            for spec in registry.all()
        ],
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _startup_report() -> None:
    kb = knowledge_base.stats
    logger.info("Модель: %s (effort=%s)", settings.model, settings.effort)
    logger.info(
        "База знаний: %s документов в %s", kb["documents"], kb["root"]
    )
    if not kb["documents"]:
        logger.warning(
            "База знаний пуста — агент будет честно отвечать «данных нет». "
            "Положите документы в %s (структура описана в knowledge_base/README.md).",
            kb["root"],
        )
    google = google_client.status()
    if google.get("connected"):
        logger.info("Google подключён: %s", google.get("account_hint") or "учётная запись определена")
    else:
        logger.warning("Google не подключён: %s", google.get("reason"))
    logger.info("Инструментов зарегистрировано: %s", len(registry.names()))
