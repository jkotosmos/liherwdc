"""Распознавание голосовых: какой способ поддерживает шлюз, заранее не знаем.

Проверяется переход: Whisper-эндпоинт есть → берём его; нет → аудио в
чат-модель (OGG перекодирован в mp3); сработавший способ запоминается.
"""

from __future__ import annotations

import base64
from dataclasses import replace

import httpx
import pytest

from app import stt
from app.config import settings


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    monkeypatch.setattr(stt, "settings", replace(settings, provider="routerai", api_key="k",
                                                 base_url="https://routerai.ru/api/v1"))
    monkeypatch.delenv("OPERON_STT_MODEL", raising=False)
    monkeypatch.delenv("OPERON_STT_CHAT_MODEL", raising=False)
    stt.reset()
    yield
    stt.reset()


class Gateway:
    def __init__(self, transcriptions="ok", chat_models=("google/gemini-2.5-flash",)):
        self.transcriptions = transcriptions
        self.chat_models = set(chat_models)
        self.calls = []

    def post(self, url, headers=None, files=None, data=None, json=None, **kwargs):
        if url.endswith("/audio/transcriptions"):
            self.calls.append(("transcriptions", data["model"]))
            if self.transcriptions == "absent":
                return httpx.Response(404, text="not found")
            if data["model"] != "openai/whisper-1":
                return httpx.Response(400, text="unknown model")
            return httpx.Response(200, json={"text": "Привет, бот"})
        self.calls.append(("chat", json["model"]))
        audio = json["messages"][0]["content"][1]["input_audio"]
        assert audio["format"] in {"mp3", "wav"} and base64.b64decode(audio["data"])
        if json["model"] not in self.chat_models:
            return httpx.Response(400, text="model does not support audio")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Привет из чата"}}]})


def test_whisper_endpoint_is_used_when_available(monkeypatch) -> None:
    gw = Gateway()
    monkeypatch.setattr(stt.httpx, "post", gw.post)
    assert stt.transcribe(b"OggS", "voice.ogg", "audio/ogg") == ("Привет, бот", "openai/whisper-1")


def test_falls_back_to_audio_chat_model_and_remembers(monkeypatch) -> None:
    gw = Gateway(transcriptions="absent")
    monkeypatch.setattr(stt.httpx, "post", gw.post)
    monkeypatch.setattr(stt, "to_mp3", lambda data: b"ID3-mp3")
    text, model = stt.transcribe(b"OggS", "voice.ogg", "audio/ogg")
    assert (text, model) == ("Привет из чата", "google/gemini-2.5-flash")
    # Эндпоинт отсутствует — второй раз его не пробуем, сразу сработавший способ.
    gw.calls.clear()
    stt.transcribe(b"OggS", "voice.ogg", "audio/ogg")
    assert gw.calls == [("chat", "google/gemini-2.5-flash")]


def test_wav_goes_to_chat_without_conversion(monkeypatch) -> None:
    gw = Gateway(transcriptions="absent")
    monkeypatch.setattr(stt.httpx, "post", gw.post)
    monkeypatch.setattr(stt, "to_mp3", lambda data: pytest.fail("wav перекодировать не нужно"))
    assert stt.transcribe(stt.silent_wav(), "check.wav", "audio/wav")[0] == "Привет из чата"


def test_nothing_works_is_explained(monkeypatch) -> None:
    gw = Gateway(transcriptions="absent", chat_models=())
    monkeypatch.setattr(stt.httpx, "post", gw.post)
    monkeypatch.setattr(stt, "to_mp3", lambda data: b"ID3")
    with pytest.raises(stt.STTError, match="OPERON_STT_MODEL"):
        stt.transcribe(b"OggS", "voice.ogg", "audio/ogg")


def test_models_are_configurable(monkeypatch) -> None:
    gw = Gateway()
    monkeypatch.setattr(stt.httpx, "post", gw.post)
    monkeypatch.setenv("OPERON_STT_MODEL", "my/whisper,openai/whisper-1")
    stt.transcribe(b"OggS", "voice.ogg", "audio/ogg")
    assert gw.calls[:2] == [("transcriptions", "my/whisper"), ("transcriptions", "openai/whisper-1")]


def test_silent_wav_is_valid() -> None:
    import io
    import wave

    with wave.open(io.BytesIO(stt.silent_wav(0.5))) as wav:
        assert wav.getframerate() == 16000 and wav.getnframes() == 8000
