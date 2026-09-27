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
import ssl
from functools import lru_cache

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
    return f"{exc.__class__.__name__}: {exc}"
