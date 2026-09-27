"""Исходящие соединения: сертификаты системы и управление прокси."""

from __future__ import annotations

import http.server
import threading

import httpx
import pytest

from app import net


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for name in ("OPERON_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                 "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


class TestOptions:
    def test_default_follows_system(self, clean_proxy_env) -> None:
        options = net.http_options()
        assert options["verify"] is net.ssl_context()
        assert "trust_env" not in options and "proxy" not in options

    def test_direct(self, clean_proxy_env, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_PROXY", "none")
        assert net.http_options()["trust_env"] is False
        assert net.describe_proxy() == ""

    def test_explicit_proxy(self, clean_proxy_env, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_PROXY", "http://user:secret@10.0.0.5:3128")
        options = net.http_options()
        assert options["proxy"] == "http://user:secret@10.0.0.5:3128"
        described = net.describe_proxy()
        assert "secret" not in described and "10.0.0.5:3128" in described

    def test_system_proxy_described(self, clean_proxy_env, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10809")
        assert "127.0.0.1:10809" in net.describe_proxy()

    def test_handshake_timeout_explained_with_proxy(self, clean_proxy_env, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10809")
        text = net.explain(httpx.ConnectTimeout("_ssl.c:1064: The handshake operation timed out"))
        assert "OPERON_PROXY=none" in text


def test_direct_mode_bypasses_dead_proxy(clean_proxy_env, monkeypatch) -> None:
    """Живая проверка: через «мёртвый» прокси запрос падает, напрямую — проходит."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # никто не слушает
    try:
        with pytest.raises(httpx.HTTPError):
            httpx.get(url, timeout=3, **net.http_options())
        monkeypatch.setenv("OPERON_PROXY", "none")
        assert httpx.get(url, timeout=3, **net.http_options()).status_code == 200
    finally:
        server.shutdown()


class TestGoogleTransport:
    """Библиотеки Google: те же сертификаты и прокси, что у всего бота."""

    def test_bundle_contains_certifi(self) -> None:
        import certifi

        bundle = open(net.ca_bundle_path(), encoding="utf-8").read()
        first_cert = open(certifi.where(), encoding="utf-8").read().split("-----END CERTIFICATE-----")[0]
        assert first_cert.strip() in bundle

    def test_requests_session_direct(self, clean_proxy_env, monkeypatch) -> None:
        import requests

        monkeypatch.setenv("OPERON_PROXY", "none")
        session = net.configure_requests_session(requests.Session())
        assert session.trust_env is False and session.proxies == {}
        assert session.verify == net.ca_bundle_path()

    def test_requests_session_explicit_proxy(self, clean_proxy_env, monkeypatch) -> None:
        import requests

        monkeypatch.setenv("OPERON_PROXY", "http://10.0.0.5:3128")
        session = net.configure_requests_session(requests.Session())
        assert session.proxies == {"http": "http://10.0.0.5:3128", "https": "http://10.0.0.5:3128"}

    def test_httplib2_direct_has_no_proxy(self, clean_proxy_env, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_PROXY", "none")
        http = net.httplib2_http()
        assert http.proxy_info is None
        assert http.ca_certs == net.ca_bundle_path()

    def test_oauth_exchange_uses_configured_session(self, clean_proxy_env, monkeypatch) -> None:
        """Именно здесь /auth ходил мимо OPERON_PROXY=none."""
        from app.integrations import google_oauth

        seen = {}

        class FakeFlow:
            def __init__(self):
                import requests

                self.oauth2session = requests.Session()

            def fetch_token(self, code):
                seen["trust_env"] = self.oauth2session.trust_env
                raise RuntimeError("стоп")

        monkeypatch.setenv("OPERON_PROXY", "none")
        monkeypatch.setattr(google_oauth, "_flow", FakeFlow)
        with pytest.raises(google_oauth.OAuthError):
            google_oauth.exchange_code("4/0AfakeCode1234567890")
        assert seen["trust_env"] is False
