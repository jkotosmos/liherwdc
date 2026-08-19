"""Минимальный синхронный клиент Telegram Bot API.

Зависимостей не добавляем: нужен десяток методов, а агент синхронный —
отдельная асинхронная библиотека потребовала бы моста между мирами.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, timeout: float = 60.0) -> None:
        if not token:
            raise TelegramError("Не задан TELEGRAM_BOT_TOKEN")
        self._url = f"{BASE}/bot{token}"
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.post(f"{self._url}/{method}", json=payload or {})
        except httpx.HTTPError as exc:
            raise TelegramError(f"Сеть недоступна при вызове {method}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: ответ не в формате JSON ({response.status_code})") from exc

        if not data.get("ok"):
            description = data.get("description", "")
            if response.status_code == 401:
                raise TelegramError("Telegram отклонил токен бота (401). Проверьте TELEGRAM_BOT_TOKEN.")
            if response.status_code == 409:
                raise TelegramError(
                    "Другой экземпляр бота уже читает обновления (409). "
                    "Остановите его — Telegram отдаёт обновления только одному получателю."
                )
            raise TelegramError(f"{method}: {description}")
        return data.get("result")

    # --- методы, которые реально используются ---

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            # Читаем только нужные типы: меньше трафика и меньше сюрпризов.
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return self._call("sendMessage", payload)
        except TelegramError as exc:
            # Разметка модели может оказаться невалидной — отправляем как текст,
            # чтобы пользователь получил ответ, а не сообщение об ошибке.
            if parse_mode and "parse" in str(exc).lower():
                logger.warning("Telegram отверг разметку, отправляю без неё: %s", exc)
                return self.send_message(chat_id, text, reply_markup, parse_mode=None)
            raise

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return self._call("editMessageText", payload)
        except TelegramError as exc:
            message = str(exc).lower()
            if "not modified" in message:
                return None
            if parse_mode and "parse" in message:
                return self.edit_message_text(chat_id, message_id, text, reply_markup, None)
            raise

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError:
            pass  # индикатор набора не критичен

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass
