"""«Квант» (Qdrant): подключение, определение устройства коллекции, поиск.

Настоящий сервер в тестах не нужен: подменяем HTTP и отвечаем так, как отвечает
Qdrant REST API (обёртка {"result": ...}).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import quant
from app.tools.base import registry

URL = "https://qdrant.example"

POINTS = [
    {"id": 1, "payload": {
        "page_content": "Тариф Бизнес — 1200 ₽ в месяц, партнёрская скидка 15%.",
        "metadata": {"title": "Тарифы 2026", "modifiedTime": "2026-03-01", "file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"},
    }},
    {"id": 2, "payload": {"content": "Договор с Ромашкой продлён до 2027 года.", "source": "dogovory.docx"}},
]


class FakeQdrant:
    def __init__(self, vectors=None, embed_size=4):
        self.vectors = vectors if vectors is not None else {"size": 4, "distance": "Cosine"}
        self.embed_size = embed_size
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, json=None, headers=None, **kwargs):
        path = url[len(URL):]
        self.calls.append((method, path, json))
        assert headers == {"api-key": "secret"}
        if path == "/collections":
            return self._ok({"collections": [{"name": "operon"}]})
        if path == "/collections/operon":
            return self._ok({"points_count": 2, "config": {"params": {"vectors": self.vectors}}})
        if path.endswith("/points/search"):
            return self._ok([{**POINTS[0], "score": 0.91}])
        if path.endswith("/points/scroll"):
            return self._ok({"points": POINTS, "next_page_offset": None})
        return httpx.Response(404, text="not found")

    def post(self, url, json=None, headers=None, **kwargs):
        self.calls.append(("POST", url, json))
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * self.embed_size}]})

    @staticmethod
    def _ok(result):
        return httpx.Response(200, content=json.dumps({"result": result, "status": "ok"}).encode())


@pytest.fixture
def qdrant(monkeypatch):
    for name in ("QDRANT_COLLECTION", "QDRANT_VECTOR_NAME", "QDRANT_EMBEDDING_MODEL", "QDRANT_TEXT_FIELD",
                 "QDRANT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QDRANT_URL", URL + "/")
    monkeypatch.setenv("QDRANTSERVICEAPI_KEY", "secret")  # имя ключа из панели Amvera
    fake = FakeQdrant()
    monkeypatch.setattr(quant.httpx, "request", fake.request)
    monkeypatch.setattr(quant.httpx, "post", fake.post)
    quant.reset_cache()
    yield fake
    quant.reset_cache()


def test_not_configured_without_url(monkeypatch) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    assert quant.search("тарифы")["status"] == "not_configured"
    content, is_error = registry.execute("quant_search", {"query": "тарифы"})
    assert is_error and "QDRANT_URL" in content


def test_semantic_search_with_model(qdrant, monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    result = quant.search("какие тарифы для партнёров")
    assert result["status"] == "ok"
    assert "по смыслу (some/embedder)" in result["search_mode"]
    top = result["results"][0]
    assert top["title"] == "Тарифы 2026" and top["date"] == "2026-03-01"
    assert top["link"] == "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz"
    assert "1200" in top["text"] and top["source"] == "Квант"
    search_body = next(body for _, path, body in qdrant.calls if path.endswith("/points/search"))
    assert search_body["vector"] == [0.1] * 4


def test_named_vector_is_used(qdrant, monkeypatch) -> None:
    qdrant.vectors = {"dense": {"size": 4, "distance": "Cosine"}}
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    quant.search("тарифы")
    body = next(body for _, path, body in qdrant.calls if path.endswith("/points/search"))
    assert body["vector"] == {"name": "dense", "vector": [0.1] * 4}


def test_wrong_model_size_falls_back_to_words_and_says_why(qdrant, monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    qdrant.embed_size = 3
    result = quant.search("договор Ромашка")
    assert "по словам" in result["search_mode"]
    assert any("даёт вектор 3" in w for w in result["warnings"])
    assert result["results"][0]["text"].startswith("Договор с Ромашкой")


def test_word_search_without_model(qdrant) -> None:
    result = quant.search("договор с Ромашкой")
    assert result["status"] == "ok" and "по словам" in result["search_mode"]
    assert not any(path.endswith("/embeddings") for _, path, _ in qdrant.calls)
    scroll = next(body for _, path, body in qdrant.calls if path.endswith("/points/scroll"))
    words = {c["match"]["text"] for c in scroll["filter"]["should"]}
    assert {"догово", "ромашк"} <= words  # основы слов, а не окончания


def test_guessed_model_by_vector_size(qdrant) -> None:
    qdrant.vectors = {"size": 1536}
    qdrant.embed_size = 1536
    result = quant.search("тарифы")
    assert "openai/text-embedding-3-small" in result["search_mode"]


def test_bad_key_is_explained(qdrant, monkeypatch) -> None:
    monkeypatch.setattr(quant.httpx, "request", lambda *a, **k: httpx.Response(401, text="unauthorized"))
    content, is_error = registry.execute("quant_search", {"query": "тарифы"})
    assert is_error and "QDRANT_API_KEY" in content


def test_llamaindex_payload_is_understood() -> None:
    conf = quant.config()
    node = {"text": "Регламент закупок", "metadata": {"file_name": "zakupki.pdf", "last_modified": "2026-05-05"}}
    view = quant.describe_point({"id": 7, "payload": {"_node_content": json.dumps(node)}}, "c", conf)
    assert view["text"] == "Регламент закупок"
    assert view["title"] == "zakupki.pdf" and view["date"] == "2026-05-05"


def test_unknown_payload_is_passed_through() -> None:
    view = quant.describe_point({"id": 1, "payload": {"txt": "что-то"}}, "c", quant.config())
    assert view["text"] == "" and view["payload"] == {"txt": "что-то"}


def test_probe_reports_structure(qdrant) -> None:
    lines = "\n".join(quant.probe("тарифы"))
    assert "Коллекции: operon" in lines
    assert "записей: 2" in lines and "размер 4" in lines
    assert "поля metadata: file_id, modifiedTime, title" in lines


def test_selfcheck_group(qdrant, monkeypatch) -> None:
    from app import selfcheck

    checks = selfcheck.check_quant()
    assert checks[0].status == selfcheck.OK
    assert checks[1].status == selfcheck.WARN  # модели нет — только по словам, и это сказано
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    assert selfcheck.check_quant()[1].status == selfcheck.OK


def test_env_file_is_loaded_before_reading_settings(tmp_path) -> None:
    """`python -m app.quant` без бота: QDRANT_URL из .env должен быть виден."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDRANT")}
    code = "import os, app.quant as q; print('app.config' in __import__('sys').modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "True", out.stderr


def test_slow_collection_info_falls_back_to_a_sample(qdrant, monkeypatch) -> None:
    """Настройки коллекции не ответили (индексация) — размер вектора берём по записи."""
    original = qdrant.request

    def slow(method, url, json=None, headers=None, **kwargs):
        if url.endswith("/collections/operon"):
            raise httpx.ReadTimeout("timed out")
        if url.endswith("/points/scroll") and json and json.get("with_vector"):
            qdrant.calls.append((method, url[len(URL):], json))
            return FakeQdrant._ok({"points": [{"id": 1, "vector": {"dense": [0.0] * 4}}]})
        return original(method, url, json=json, headers=headers, **kwargs)

    monkeypatch.setattr(quant.httpx, "request", slow)
    monkeypatch.setenv("QDRANT_EMBEDDING_MODEL", "some/embedder")
    info = quant.collection_info("operon", quant.config())
    assert (info.size, info.vector_name, info.points) == (4, "dense", -1)
    lines = "\n".join(quant.probe("тарифы"))
    assert "настройки не ответили" in lines and "по смыслу" in lines


def test_cyrillic_collection_name_is_encoded(qdrant, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        quant.httpx, "request",
        lambda method, url, **kw: seen.append(url) or FakeQdrant._ok({"points_count": 0, "config": {}}),
    )
    quant.collection_info("квантум", quant.config())
    assert seen[0] == URL + "/collections/%D0%BA%D0%B2%D0%B0%D0%BD%D1%82%D1%83%D0%BC"


def test_probe_says_when_collection_is_empty(qdrant, monkeypatch) -> None:
    def empty(method, url, json=None, headers=None, **kwargs):
        path = url[len(URL):]
        if path == "/collections":
            return FakeQdrant._ok({"collections": [{"name": "knowledge_base"}]})
        if path.endswith("/points/count"):
            return FakeQdrant._ok({"count": 0})
        if path.endswith("/points/scroll"):
            return FakeQdrant._ok({"points": [], "next_page_offset": None})
        return FakeQdrant._ok({"points_count": 0, "config": {"params": {"vectors": {"size": 4}}}})

    monkeypatch.setattr(quant.httpx, "request", empty)
    lines = "\n".join(quant.probe("тарифы"))
    assert "точный подсчёт записей: 0" in lines and "коллекция ПУСТА" in lines
