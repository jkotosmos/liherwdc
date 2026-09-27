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


# --- requests и httplib2 (библиотеки Google) ----------------------------------
# Библиотеки Google ходят в сеть не через httpx: обмен кода и обновление токена
# идут через requests, вызовы Drive/Calendar — через httplib2. Им нельзя
# передать SSLContext, только путь к файлу сертификатов, и прокси они берут
# из окружения сами. Поэтому собираем общий файл сертификатов (certifi +
# хранилище системы) и применяем ту же настройку OPERON_PROXY.

_bundle_lock = __import__("threading").Lock()
_bundle_path: str = ""


def ca_bundle_path() -> str:
    """Файл PEM: certifi плюс корневые сертификаты системы (на Windows — хранилище)."""
    global _bundle_path
    with _bundle_lock:
        if _bundle_path and os.path.exists(_bundle_path):
            return _bundle_path
        import tempfile

        import certifi

        parts = [open(certifi.where(), encoding="utf-8").read()]
        enum = getattr(ssl, "enum_certificates", None)  # есть только на Windows
        if enum is not None:
            for store in ("ROOT", "CA"):
                try:
                    for cert, encoding, trust in enum(store):
                        if encoding == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust):
                            parts.append(ssl.DER_cert_to_PEM_cert(cert))
                except (OSError, PermissionError) as exc:  # pragma: no cover — Windows
                    logger.warning("Хранилище %s не прочиталось: %s", store, exc)
        else:
            system = ssl.get_default_verify_paths().cafile
            if system and os.path.exists(system):
                parts.append(open(system, encoding="utf-8", errors="ignore").read())
        extra = os.getenv("SSL_CERT_FILE")
        if extra and os.path.exists(extra):
            parts.append(open(extra, encoding="utf-8", errors="ignore").read())

        fd, path = tempfile.mkstemp(prefix="operon-ca-", suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(parts))
        _bundle_path = path
        return path


def configure_requests_session(session):
    """Сертификаты и прокси для requests.Session (обмен кода Google, токены)."""
    session.verify = ca_bundle_path()
    setting = proxy_setting()
    if setting.lower() in DIRECT:
        session.trust_env = False
        session.proxies = {}
    elif setting:
        session.trust_env = False
        session.proxies = {"http": setting, "https": setting}
    return session


def httplib2_http():
    """httplib2.Http с теми же сертификатами и прокси — для API Google."""
    import httplib2

    setting = proxy_setting()
    if setting.lower() in DIRECT:
        proxy_info = None
    elif setting:
        proxy_info = httplib2.proxy_info_from_url(setting)
    else:
        proxy_info = httplib2.proxy_info_from_environment
    return httplib2.Http(ca_certs=ca_bundle_path(), proxy_info=proxy_info, timeout=60)
