"""Распознавание голосовых сообщений — через тот же шлюз (RouterAI), тем же ключом.

Шлюзы в духе OpenAI умеют это двумя способами, и какой из них включён у
конкретного шлюза, заранее не известно:

1. **/audio/transcriptions** — классический Whisper: отправляем файл, получаем
   текст. Голосовое Telegram (OGG/Opus) принимается как есть.
2. **Аудио в чат-модель** (`input_audio`) — модель, которая слышит, пишет
   расшифровку. Такие модели принимают mp3/wav, поэтому OGG перекодируется
   через ffmpeg (он есть в Docker-образе).

Бот пробует по порядку и запоминает, что сработало, — со второго голосового
лишних запросов нет. Модели можно задать: OPERON_STT_MODEL (для способа 1) и
OPERON_STT_CHAT_MODEL (для способа 2), через запятую.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import threading
import wave
from typing import Any

import httpx

from .config import settings
from .net import explain, http_options

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
TRANSCRIBE_MODELS = ("openai/whisper-1", "openai/gpt-4o-mini-transcribe", "whisper-1")
CHAT_AUDIO_MODELS = ("openai/gpt-4o-audio-preview", "google/gemini-2.5-flash")
PROMPT = (
    "Расшифруй это голосовое сообщение дословно, на языке оригинала. "
    "Верни только текст расшифровки, без пояснений. Если речи нет — верни пустую строку."
)

_lock = threading.Lock()
_working: tuple[str, str] | None = None  # (способ, модель), что сработало последним


class STTError(Exception):
    """Текст пригоден для показа пользователю."""


def _models(env: str, defaults: tuple[str, ...]) -> list[str]:
    raw = (os.getenv(env) or "").strip()
    return [m.strip() for m in raw.split(",") if m.strip()] or list(defaults)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_key}", **settings.extra_headers}


def available() -> bool:
    return settings.provider in {"routerai", "openrouter", "custom"} and bool(settings.api_key and settings.base_url)


# --- способ 1: /audio/transcriptions ----------------------------------------


class _NoEndpoint(Exception):
    pass


def _via_transcriptions(data: bytes, filename: str, mime: str, model: str) -> str:
    try:
        response = httpx.post(
            settings.base_url.rstrip("/") + "/audio/transcriptions",
            headers=_headers(),
            files={"file": (filename, data, mime)},
            data={"model": model, "language": "ru", "response_format": "json"},
            timeout=TIMEOUT,
            **http_options(),
        )
    except httpx.HTTPError as exc:
        raise STTError(f"Шлюз не ответил на распознавание: {explain(exc)}") from exc
    if response.status_code in (404, 405):
        raise _NoEndpoint(response.text[:200])
    if response.status_code >= 400:
        raise ValueError(f"{model}: {response.status_code} {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    return str(payload.get("text", "") if isinstance(payload, dict) else payload).strip()


# --- способ 2: аудио в чат-модель -------------------------------------------


def to_mp3(data: bytes) -> bytes:
    """OGG/Opus → mp3 через ffmpeg. Без ffmpeg — STTError с объяснением."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise STTError("Для этого способа распознавания нужен ffmpeg, а его нет на сервере.")
    try:
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
             "-f", "mp3", "pipe:1"],
            input=data, capture_output=True, timeout=120, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise STTError(f"Не удалось перекодировать голосовое: {exc}") from exc
    return done.stdout


def _via_chat(data: bytes, audio_format: str, model: str) -> str:
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": audio_format}},
            ],
        }],
        "temperature": 0,
    }
    try:
        response = httpx.post(
            settings.base_url.rstrip("/") + "/chat/completions",
            headers=_headers(), json=body, timeout=TIMEOUT, **http_options(),
        )
    except httpx.HTTPError as exc:
        raise STTError(f"Шлюз не ответил на распознавание: {explain(exc)}") from exc
    if response.status_code >= 400:
        raise ValueError(f"{model}: {response.status_code} {response.text[:200]}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"{model}: неожиданный ответ") from exc
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "").strip()


# --- общий вход ---------------------------------------------------------------


def transcribe(data: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> tuple[str, str]:
    """Возвращает (текст, чем распознано). Бросает STTError, если не вышло никак."""
    global _working
    if not available():
        raise STTError("Распознавание голоса работает через RouterAI — не задан ключ шлюза.")
    if not data:
        raise STTError("Пустой аудиофайл.")

    attempts: list[tuple[str, str]] = []
    with _lock:
        if _working:
            attempts.append(_working)
    attempts += [("transcriptions", m) for m in _models("OPERON_STT_MODEL", TRANSCRIBE_MODELS)]
    attempts += [("chat", m) for m in _models("OPERON_STT_CHAT_MODEL", CHAT_AUDIO_MODELS)]

    problems: list[str] = []
    endpoint_missing = False
    converted: bytes | None = None
    tried: set[tuple[str, str]] = set()
    for way, model in attempts:
        if (way, model) in tried or (way == "transcriptions" and endpoint_missing):
            continue
        tried.add((way, model))
        try:
            if way == "transcriptions":
                text = _via_transcriptions(data, filename, mime, model)
            else:
                if mime in ("audio/wav", "audio/x-wav"):
                    payload, fmt = data, "wav"
                elif mime == "audio/mpeg":
                    payload, fmt = data, "mp3"
                else:
                    if converted is None:
                        converted = to_mp3(data)
                    payload, fmt = converted, "mp3"
                text = _via_chat(payload, fmt, model)
        except _NoEndpoint:
            endpoint_missing = True
            problems.append("шлюз не поддерживает /audio/transcriptions")
            continue
        except ValueError as exc:
            problems.append(str(exc))
            continue
        with _lock:
            _working = (way, model)
        return text, model

    logger.warning("Распознавание не удалось: %s", "; ".join(problems))
    raise STTError(
        "Не удалось распознать голосовое: шлюз не принял ни один способ. "
        + "; ".join(problems[:4])
        + ". Задайте подходящую модель из каталога RouterAI в OPERON_STT_MODEL или OPERON_STT_CHAT_MODEL."
    )


def silent_wav(seconds: float = 1.0) -> bytes:
    """Тишина в WAV — для самопроверки, без ffmpeg и без записи голоса."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buffer.getvalue()


def describe() -> dict[str, Any]:
    return {"working": _working}


def reset() -> None:
    global _working
    with _lock:
        _working = None
