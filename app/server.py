"""HTTP-слой: чат по SSE, подтверждение действий, статус интеграций."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
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


_telegram_bot: Any = None
_telegram_thread: threading.Thread | None = None


def _start_telegram() -> None:
    """Поднимает бота фоновым потоком: на Amvera процесс один."""
    global _telegram_bot, _telegram_thread

    if not settings.telegram_token:
        logger.info("Telegram не настроен (нет TELEGRAM_BOT_TOKEN) — работает только веб-интерфейс")
        return
    if not settings.telegram_allowed_users:
        logger.error(
            "Telegram-бот НЕ запущен: не задан TELEGRAM_ALLOWED_USERS. "
            "Без белого списка доступ к вашему Google-аккаунту получил бы любой."
        )
        return

    from .telegram.api import TelegramError
    from .telegram.bot import TelegramBot

    try:
        _telegram_bot = TelegramBot()
    except TelegramError as exc:
        logger.error("Telegram-бот не запущен: %s", exc)
        return

    _telegram_thread = threading.Thread(target=_telegram_bot.run, name="telegram", daemon=True)
    _telegram_thread.start()


def _stop_telegram() -> None:
    if _telegram_bot is not None:
        _telegram_bot.stop()
    if _telegram_thread is not None:
        _telegram_thread.join(timeout=5)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _startup_report()
    _start_telegram()
    yield
    _stop_telegram()


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


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


# Пути, доступные без входа: проверка живости, сама страница входа и статика.
PUBLIC_PATHS = {"/api/health", "/api/login", "/login", "/favicon.ico"}


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


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """Пропускает запрос только с действующей кукой, если задан пароль."""
    path = request.url.path
    if not settings.auth_required or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    if auth.token_is_valid(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "Требуется вход в систему."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.post("/api/login")
def login(request: LoginRequest, http_request: Request) -> JSONResponse:
    if not settings.auth_required:
        return JSONResponse({"status": "ok", "auth_required": False})

    client = http_request.client.host if http_request.client else "unknown"
    locked_for = auth.throttle_state(client)
    if locked_for:
        return JSONResponse(
            {"detail": f"Слишком много попыток. Повторите через {locked_for} с."},
            status_code=429,
        )

    if not auth.password_is_valid(request.password):
        auth.register_failure(client)
        return JSONResponse({"detail": "Неверный пароль."}, status_code=401)

    auth.register_success(client)
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(),
        max_age=settings.auth_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        # За прокси Amvera соединение идёт по HTTPS — куку помечаем Secure.
        secure=bool(settings.public_url.startswith("https://")),
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/login")
def login_page() -> Response:
    if not settings.auth_required:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


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
        "provider": settings.provider,
        "effort": settings.effort if settings.effort_enabled else None,
        "auth_required": settings.auth_required,
        "timezone": settings.timezone_name,
        "knowledge_base": kb,
        "google": google,
        "web_search": settings.web_search_enabled,
        "telegram": {
            "configured": bool(settings.telegram_token),
            "allowed_users": len(settings.telegram_allowed_users),
            "running": _telegram_thread is not None and _telegram_thread.is_alive(),
        },
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
    logger.info(
        "Провайдер: %s | модель: %s | адрес: %s",
        settings.provider,
        settings.model,
        settings.base_url or "api.anthropic.com",
    )
    if not settings.auth_required:
        logger.warning("Пароль не задан (OPERON_ACCESS_PASSWORD) — вход в интерфейс свободный")
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
    if settings.telegram_enabled:
        logger.info(
            "Telegram: белый список из %s пользователей", len(settings.telegram_allowed_users)
        )
