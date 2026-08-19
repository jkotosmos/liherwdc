"""Авторизация Google без доступа к серверу: ссылка → код → токен.

Обычный сценарий google-auth поднимает локальный веб-сервер и открывает
браузер. На Amvera так нельзя: браузера в контейнере нет, а токен, полученный
на рабочем ноутбуке, пришлось бы переносить файлами при каждой переавторизации.

Здесь используется тот же OAuth-клиент типа Desktop, но redirect_uri остаётся
непрослушанным: после подтверждения браузер пытается открыть localhost, видит
ошибку соединения — и показывает код в адресной строке. Пользователь копирует
адрес целиком и отдаёт боту, обмен на токен происходит на сервере.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..config import settings
from . import google_client, token_store

logger = logging.getLogger(__name__)

# Коды Google начинаются с «4/» и состоят из URL-safe символов. Длину берём
# с запасом вниз: настоящий код длиннее сотни знаков, но занижать порог
# безопаснее, чем отвергнуть годный код из-за смены формата.
CODE_RE = re.compile(r"^[0-9A-Za-z._~/\-]{12,}$")


class OAuthError(Exception):
    """Ошибка, текст которой можно показать пользователю целиком."""


def _client_config() -> dict[str, Any]:
    path = settings.google_client_secret_path
    if not path.exists():
        raise OAuthError(
            f"Не найден файл OAuth-клиента: {path}\n\n"
            "Как получить: console.cloud.google.com → APIs & Services → Credentials → "
            "Create credentials → OAuth client ID → тип «Desktop app». Скачанный JSON "
            "положите по этому пути (на сервере — на постоянный диск)."
        )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OAuthError(f"Файл OAuth-клиента нечитаем ({exc}). Скачайте его заново.") from exc

    if "installed" not in config and "web" not in config:
        raise OAuthError(
            "В файле нет ни секции «installed», ни «web» — это не файл OAuth-клиента. "
            "Скачайте JSON именно из раздела Credentials → OAuth 2.0 Client IDs."
        )
    if "web" in config and "installed" not in config:
        # Для клиента типа Web адрес возврата должен быть заранее прописан в консоли.
        registered = config["web"].get("redirect_uris") or []
        if settings.oauth_redirect_uri not in registered:
            raise OAuthError(
                "OAuth-клиент имеет тип «Web application», а его список Authorized "
                f"redirect URIs не содержит {settings.oauth_redirect_uri}. Либо добавьте "
                "этот адрес в Google Cloud Console, либо задайте OPERON_OAUTH_REDIRECT_URI "
                "равным одному из уже разрешённых, либо создайте клиент типа «Desktop app» "
                "(для него подходит любой localhost)."
            )
    return config


def _flow() -> Any:
    """Собирает поток заново на каждом шаге: состояние между шагами не нужно.

    ``fetch_token(code=...)`` проверяет только сам код, поэтому хранить объект
    потока между сообщениями (и переживать с ним перезапуск) не требуется.
    """
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise OAuthError(
            "Не установлены библиотеки Google. Выполните: pip install -r requirements.txt"
        ) from exc

    try:
        return Flow.from_client_config(
            _client_config(),
            scopes=list(settings.google_scopes),
            redirect_uri=settings.oauth_redirect_uri,
        )
    except ValueError as exc:
        raise OAuthError(f"Файл OAuth-клиента не подходит для авторизации: {exc}") from exc


def authorization_url() -> str:
    """Ссылка, по которой пользователь выдаёт доступ."""
    flow = _flow()
    url, _state = flow.authorization_url(
        # Без access_type=offline Google не выдаст refresh_token, и через час
        # агент потеряет доступ до следующей ручной авторизации.
        access_type="offline",
        # Повторная авторизация без prompt=consent возвращается без refresh_token.
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def looks_like_code(text: str) -> bool:
    """Отличает вставленный код (или адрес возврата) от обычного вопроса."""
    raw = (text or "").strip()
    if not raw or len(raw.split()) > 1:
        return False
    if "code=" in raw:
        return True
    return bool(CODE_RE.match(raw))


def extract_code(text: str) -> str:
    """Достаёт код из адреса возврата или принимает его в чистом виде."""
    raw = (text or "").strip().strip("<>\"'")
    if not raw:
        raise OAuthError("Пустое сообщение — пришлите код или адрес из строки браузера.")

    if "code=" in raw:
        query = urlparse(raw).query or raw.split("?", 1)[-1]
        values = parse_qs(query).get("code") or []
        if not values or not values[0]:
            raise OAuthError(
                "В присланном адресе нет параметра code. Скопируйте адрес целиком — "
                "он выглядит так: http://localhost:8765/?code=4/0A…&scope=…"
            )
        return values[0]

    if "error=" in raw:
        raise OAuthError("Google вернул отказ в доступе. Повторите /auth и нажмите «Разрешить».")

    # Скопированный из адресной строки код может остаться percent-encoded.
    code = unquote(raw)
    if not CODE_RE.match(code):
        raise OAuthError(
            "Это не похоже на код авторизации. Нужен либо код целиком (начинается с «4/»), "
            "либо весь адрес из строки браузера после подтверждения доступа."
        )
    return code


def exchange_code(text: str) -> dict[str, Any]:
    """Обменивает код на токен и сохраняет его. Возвращает описание результата."""
    code = extract_code(text)
    flow = _flow()

    # Google возвращает scope'ы в своём порядке и добавляет openid — oauthlib
    # считает это подменой прав и падает. Расхождение здесь безопасно:
    # фактически выданные права мы проверяем ниже сами.
    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 — библиотека бросает разные типы
        raise OAuthError(_describe_exchange_error(exc)) from exc
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous

    creds = flow.credentials
    if not getattr(creds, "refresh_token", None):
        raise OAuthError(
            "Google выдал токен без refresh_token — через час доступ пропадёт. "
            "Отзовите доступ приложению на myaccount.google.com/permissions "
            "и пройдите /auth заново."
        )

    granted = set(getattr(creds, "scopes", None) or [])
    missing = [scope for scope in settings.google_scopes if scope not in granted]

    path = token_store.save_token(creds.to_json())
    google_client.reset_cache()
    logger.info("Токен Google сохранён (%s), выдано %s разрешений", path, len(granted))

    return {
        "path": str(path),
        "encrypted": token_store.encryption_enabled(),
        "scopes": sorted(granted),
        "missing_scopes": missing,
        "account": _account_email(),
    }


def _account_email() -> str:
    """Под какой учётной записью выдан доступ — важно перепроверить глазами."""
    try:
        return google_client.status().get("account_hint", "") or ""
    except Exception:  # noqa: BLE001 — необязательная информация
        return ""


def _describe_exchange_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "invalid_grant" in lowered:
        return (
            "Google отклонил код (invalid_grant). Обычно это значит, что код уже "
            "использован или истёк — он живёт несколько минут. Начните заново: /auth"
        )
    if "redirect_uri_mismatch" in lowered:
        return (
            "Google отклонил адрес возврата (redirect_uri_mismatch). Ожидался "
            f"{settings.oauth_redirect_uri}. Убедитесь, что OAuth-клиент имеет тип "
            "«Desktop app», либо задайте OPERON_OAUTH_REDIRECT_URI под свой клиент."
        )
    if "invalid_client" in lowered:
        return (
            "Google не признал OAuth-клиент (invalid_client). Проверьте, что на сервере "
            "лежит актуальный client_secret.json от того же проекта."
        )
    if "access_denied" in lowered:
        return "Доступ не выдан: на экране согласия нажата «Отмена». Повторите /auth."
    return f"Не удалось обменять код на токен: {message}"


def instructions() -> str:
    """Текст, который бот показывает вместе со ссылкой (без разметки)."""
    return (
        "1. Откройте ссылку и разрешите доступ — под той учётной записью, "
        "с документами которой должен работать ассистент.\n"
        "2. Браузер попытается открыть "
        f"{settings.oauth_redirect_uri} и покажет ошибку соединения. Это нормально: "
        "слушать этот адрес некому.\n"
        "3. Скопируйте адрес из строки браузера целиком и пришлите его сюда "
        "следующим сообщением.\n\n"
        f"Ссылка действует ограниченное время, жду код {settings.oauth_wait_minutes} мин."
    )
