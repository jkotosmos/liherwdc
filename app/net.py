"""Проверка сертификатов для исходящих HTTPS-запросов.

По умолчанию httpx доверяет только своему набору корневых сертификатов
(certifi) и не смотрит в хранилище операционной системы. Браузер же доверяет
хранилищу Windows. Разница всплывает, когда HTTPS-соединение перехватывает
антивирус («проверка защищённых соединений» у Kaspersky, ESET, Dr.Web, Avast)
или корпоративный прокси: они подставляют свой сертификат и кладут свой
корневой в хранилище Windows. Браузер работает, а бот падает с
CERTIFICATE_VERIFY_FAILED — причём не на каждом запросе, если антивирус
перехватывает соединения выборочно.

Поэтому для всех своих запросов собираем контекст, который доверяет обоим:
хранилищу системы (как браузер) и certifi (как раньше).
"""

from __future__ import annotations

import logging
import os
import ssl
from functools import lru_cache
from urllib.parse import urlsplit
from urllib.request import getproxies

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Системное хранилище + certifi. Создаётся один раз на процесс."""
    try:
        # Windows: хранилища ROOT и CA; Linux: системный набор (/etc/ssl).
        # Переменные SSL_CERT_FILE и SSL_CERT_DIR тоже учитываются.
        context = ssl.create_default_context()
    except (ssl.SSLError, OSError, ValueError) as exc:  # pragma: no cover — битый сертификат в хранилище
        logger.warning("Хранилище сертификатов системы не прочиталось (%s) — беру только certifi", exc)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, ssl.SSLError, OSError) as exc:  # pragma: no cover — certifi приходит с httpx
        logger.warning("Набор certifi не загрузился: %s", exc)
    return context


def is_certificate_error(exc: BaseException) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(exc, ssl.SSLCertVerificationError)


CERTIFICATE_HINT = (
    "сертификат сайта не прошёл проверку. Обычно так бывает, когда HTTPS-соединение "
    "перехватывает антивирус («проверка защищённых соединений») или прокси. "
    "Исключите python.exe из проверки HTTPS в антивирусе или выключите эту проверку"
)


def explain(exc: BaseException) -> str:
    """Короткое объяснение сетевой ошибки для человека."""
    if is_certificate_error(exc):
        return CERTIFICATE_HINT
    text = f"{exc.__class__.__name__}: {exc}"
    if "handshake" in str(exc).lower() or exc.__class__.__name__ in {"ConnectTimeout", "ProxyError"}:
        via = describe_proxy()
        if via:
            return (f"{text} — соединение идёт через прокси {via}, и он не ответил вовремя. "
                    "Проверьте VPN или попробуйте напрямую: OPERON_PROXY=none в .env")
    return text


# --- прокси ------------------------------------------------------------------
# httpx по умолчанию идёт через системный прокси: переменные HTTPS_PROXY/
# HTTP_PROXY, а на Windows — ещё и настройку «Прокси» из параметров системы,
# которую включают VPN-программы. Если такой прокси нестабилен, запросы падают
# с «handshake operation timed out», хотя сайт доступен напрямую.
# OPERON_PROXY: пусто — как в системе; none — напрямую; адрес — свой прокси.

DIRECT = {"none", "off", "direct", "нет"}


def proxy_setting() -> str:
    return (os.getenv("OPERON_PROXY") or "").strip().strip("\"'")


def http_options() -> dict:
    """Параметры для всех исходящих запросов httpx: сертификаты и прокси."""
    options: dict = {"verify": ssl_context()}
    setting = proxy_setting()
    if setting.lower() in DIRECT:
        options["trust_env"] = False
    elif setting:
        options["proxy"] = setting
        options["trust_env"] = False
    return options


def _mask(url: str) -> str:
    """Адрес прокси без логина и пароля."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***@{host}{port}"
    return url


def describe_proxy() -> str:
    """Через какой прокси идут запросы; пустая строка — напрямую."""
    setting = proxy_setting()
    if setting.lower() in DIRECT:
        return ""
    if setting:
        return f"{_mask(setting)} (OPERON_PROXY)"
    proxies = getproxies()
    url = proxies.get("https") or proxies.get("all") or proxies.get("http") or ""
    return f"{_mask(url)} (системный прокси или переменная окружения)" if url else ""
