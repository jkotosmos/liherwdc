"""«Квант» — основная база знаний OPERON в Qdrant.

Документы технического аккаунта загружаются туда отдельным конвейером; бот
только ищет. По договорённости это источник правды: если в Кванте и в личном
Google Drive одно и то же расходится, верим Кванту.

Как устроена коллекция, заранее неизвестно, поэтому почти всё определяется
само:

* коллекции — QDRANT_COLLECTION (можно несколько через запятую); не задано —
  ищем во всех;
* вектор — размер и имя читаются из настроек коллекции; QDRANT_VECTOR_NAME
  нужен, только если именованных векторов несколько;
* поля записи — текст, название, ссылка и дата ищутся по распространённым
  именам (LangChain, n8n, LlamaIndex, свои) в записи и в её ``metadata``.

Поиск двух видов:

* **по смыслу** — запрос превращается в вектор той же моделью, которой
  наполняли базу (QDRANT_EMBEDDING_MODEL, через RouterAI или свой адрес);
* **по словам** — если модель не задана или не ответила: фильтр Qdrant по
  вхождению слов в текст. Хуже по смыслу, но честно находит то, что есть.

Проверка с вашего компьютера: ``python -m app.quant`` — покажет коллекции,
размер векторов, поля записей и пробный поиск.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

# Импорт config загружает .env — до первого чтения QDRANT_* из окружения.
from .config import ROUTERAI_BASE_URL, settings
from .net import explain, http_options

TIMEOUT = httpx.Timeout(25.0, connect=15.0)
INFO_TTL_SECONDS = 600

TEXT_KEYS = ("page_content", "content", "text", "chunk", "chunk_text", "document", "body", "pageContent")
TITLE_KEYS = ("title", "document_title", "doc_title", "file_name", "filename", "name", "source_name")
LINK_KEYS = ("url", "link", "webViewLink", "web_view_link", "source_url", "file_url", "doc_url", "source")
DATE_KEYS = (
    "modified", "modifiedTime", "modified_time", "updated_at", "updated", "last_modified",
    "date", "created_at", "createdTime", "created",
)
ID_KEYS = ("file_id", "doc_id", "document_id", "fileId", "drive_id")

# Размер вектора → модель, которой его обычно строят. Только подсказка для
# автонастройки: настоящая модель задаётся QDRANT_EMBEDDING_MODEL.
KNOWN_SIZES = {
    1536: "openai/text-embedding-3-small",
    3072: "openai/text-embedding-3-large",
}


class QuantError(Exception):
    """Текст пригоден для показа пользователю."""


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str
    collections: tuple[str, ...]
    vector_name: str
    embedding_model: str
    embedding_url: str
    embedding_key: str
    text_field: str

    @property
    def enabled(self) -> bool:
        return bool(self.url)


def config() -> Config:
    def env(*names: str) -> str:
        for name in names:
            value = (os.getenv(name) or "").strip()
            if value:
                return value
        return ""

    url = env("QDRANT_URL").rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    collections = tuple(c.strip() for c in env("QDRANT_COLLECTION").split(",") if c.strip())
    # Эмбеддинги по умолчанию — через тот же шлюз и ключ, что и модель.
    gateway = settings.base_url if settings.provider in {"routerai", "openrouter"} else ROUTERAI_BASE_URL
    return Config(
        url=url,
        # QDRANTSERVICEAPI_KEY — так ключ назван в панели Amvera.
        api_key=env("QDRANT_API_KEY", "QDRANTSERVICEAPI_KEY"),
        collections=collections,
        vector_name=env("QDRANT_VECTOR_NAME"),
        embedding_model=env("QDRANT_EMBEDDING_MODEL"),
        embedding_url=(env("QDRANT_EMBEDDING_URL") or gateway).rstrip("/"),
        embedding_key=env("QDRANT_EMBEDDING_KEY") or settings.api_key,
        text_field=env("QDRANT_TEXT_FIELD"),
    )


# --- HTTP --------------------------------------------------------------------


def _request(method: str, path: str, conf: Config, body: dict | None = None) -> Any:
    headers = {"api-key": conf.api_key} if conf.api_key else {}
    try:
        response = httpx.request(
            method, conf.url + path, json=body, headers=headers, timeout=TIMEOUT, **http_options()
        )
    except httpx.HTTPError as exc:
        raise QuantError(f"Квант не отвечает ({conf.url}): {explain(exc)}") from exc
    if response.status_code in (401, 403):
        raise QuantError("Квант отклонил ключ: проверьте QDRANT_API_KEY.")
    if response.status_code == 404:
        raise QuantError(f"Квант: не найдено {path}. Проверьте имя коллекции (QDRANT_COLLECTION).")
    if response.status_code >= 400:
        raise QuantError(f"Квант ответил {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise QuantError(f"Квант вернул не JSON: {response.text[:200]}") from exc
    return data.get("result", data) if isinstance(data, dict) else data


# --- устройство коллекций ----------------------------------------------------


@dataclass(frozen=True)
class CollectionInfo:
    name: str
    points: int
    vector_name: str  # "" — безымянный вектор
    size: int


_lock = threading.Lock()
_info_cache: dict[str, tuple[float, CollectionInfo]] = {}
_names_cache: tuple[float, tuple[str, ...]] | None = None


def collection_names(conf: Config) -> tuple[str, ...]:
    global _names_cache
    if conf.collections:
        return conf.collections
    with _lock:
        if _names_cache and time.monotonic() - _names_cache[0] < INFO_TTL_SECONDS:
            return _names_cache[1]
    result = _request("GET", "/collections", conf)
    names = tuple(c.get("name", "") for c in (result or {}).get("collections", []) if c.get("name"))
    with _lock:
        _names_cache = (time.monotonic(), names)
    return names


def collection_info(name: str, conf: Config) -> CollectionInfo:
    with _lock:
        cached = _info_cache.get(name)
        if cached and time.monotonic() - cached[0] < INFO_TTL_SECONDS:
            return cached[1]
    result = _request("GET", f"/collections/{name}", conf) or {}
    vectors = ((result.get("config") or {}).get("params") or {}).get("vectors") or {}
    vector_name, size = "", 0
    if isinstance(vectors, dict) and "size" in vectors:
        size = int(vectors.get("size") or 0)
    elif isinstance(vectors, dict) and vectors:
        names = list(vectors)
        vector_name = conf.vector_name if conf.vector_name in vectors else names[0]
        size = int((vectors.get(vector_name) or {}).get("size") or 0)
    info = CollectionInfo(
        name=name,
        points=int(result.get("points_count") or result.get("vectors_count") or 0),
        vector_name=vector_name,
        size=size,
    )
    with _lock:
        _info_cache[name] = (time.monotonic(), info)
    return info


def reset_cache() -> None:
    global _names_cache
    with _lock:
        _info_cache.clear()
        _names_cache = None


# --- разбор записи -----------------------------------------------------------


def _layers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    layers = [payload]
    for key in ("metadata", "meta", "_metadata"):
        if isinstance(payload.get(key), dict):
            layers.append(payload[key])
    node = payload.get("_node_content")
    if isinstance(node, str):
        # LlamaIndex хранит узел JSON-строкой.
        try:
            parsed = json.loads(node)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            layers.append(parsed)
            if isinstance(parsed.get("metadata"), dict):
                layers.append(parsed["metadata"])
    return layers


def _first(layers: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for key in keys:
        for layer in layers:
            value = layer.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
    return ""


def describe_point(point: dict[str, Any], collection: str, conf: Config) -> dict[str, Any]:
    payload = point.get("payload") or {}
    layers = _layers(payload)
    text_keys = ((conf.text_field,) if conf.text_field else ()) + TEXT_KEYS
    text = _first(layers, text_keys)
    link = _first(layers, LINK_KEYS)
    if link and not link.startswith(("http://", "https://")):
        link = ""  # «source» бывает путём к файлу — ссылкой его не выдаём
    file_id = _first(layers, ID_KEYS)
    if not link and re.fullmatch(r"[\w-]{20,}", file_id or ""):
        link = f"https://drive.google.com/open?id={file_id}"
    title = _first(layers, TITLE_KEYS) or _first(layers, ("source",)) or "(без названия)"
    result: dict[str, Any] = {
        "source": "Квант",
        "collection": collection,
        "title": title,
        "text": text[:3000],
        "link": link or None,
        "date": _first(layers, DATE_KEYS) or None,
        "point_id": point.get("id"),
    }
    if point.get("score") is not None:
        result["score"] = round(float(point["score"]), 3)
    if not text:
        # Не нашли текст по известным именам — отдаём поля как есть, пусть модель разберётся.
        result["payload"] = {k: v for k, v in payload.items() if k != "_node_content"}
    return result


# --- поиск -------------------------------------------------------------------


def embed(query: str, conf: Config, model: str) -> list[float]:
    headers = {"Authorization": f"Bearer {conf.embedding_key}"} if conf.embedding_key else {}
    try:
        response = httpx.post(
            conf.embedding_url + "/embeddings",
            json={"model": model, "input": query},
            headers=headers,
            timeout=TIMEOUT,
            **http_options(),
        )
    except httpx.HTTPError as exc:
        raise QuantError(f"Модель эмбеддингов не отвечает: {explain(exc)}") from exc
    if response.status_code >= 400:
        raise QuantError(f"Модель эмбеддингов {model}: ответ {response.status_code} {response.text[:200]}")
    try:
        return [float(x) for x in response.json()["data"][0]["embedding"]]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise QuantError(f"Модель эмбеддингов {model} вернула неожиданный ответ.") from exc


def _embedding_model(conf: Config, info: CollectionInfo) -> str:
    return conf.embedding_model or KNOWN_SIZES.get(info.size, "")


def _vector_search(query_vector: list[float], info: CollectionInfo, conf: Config, limit: int) -> list[dict]:
    vector: Any = {"name": info.vector_name, "vector": query_vector} if info.vector_name else query_vector
    result = _request(
        "POST",
        f"/collections/{info.name}/points/search",
        conf,
        {"vector": vector, "limit": limit, "with_payload": True},
    )
    return result if isinstance(result, list) else []


_WORD = re.compile(r"[\wё]+", re.IGNORECASE)
_STOP = {
    "какие", "какой", "какая", "каких", "что", "это", "для", "про", "как", "где", "когда", "есть",
    "наши", "наш", "наша", "нас", "мне", "все", "всё", "или", "the", "and",
}


def keywords(query: str) -> list[str]:
    """Основы слов: «партнёрам» и «партнёров» должны найти друг друга."""
    words = []
    for word in _WORD.findall(query.lower()):
        if len(word) < 3 or word in _STOP:
            continue
        stem = word[:6] if len(word) > 6 else word
        if stem not in words:
            words.append(stem)
    return words[:8]


def _text_search(query: str, info: CollectionInfo, conf: Config, limit: int) -> list[dict]:
    words = keywords(query)
    if not words:
        return []
    fields = [conf.text_field] if conf.text_field else ["page_content", "content", "text", "metadata.text"]
    should = [{"key": field, "match": {"text": word}} for field in fields for word in words]
    result = _request(
        "POST",
        f"/collections/{info.name}/points/scroll",
        conf,
        {"filter": {"should": should}, "limit": 200, "with_payload": True, "with_vector": False},
    )
    points = (result or {}).get("points", []) if isinstance(result, dict) else []
    scored = []
    for point in points:
        text = json.dumps(point.get("payload") or {}, ensure_ascii=False).lower()
        hits = sum(1 for word in words if word in text)
        if hits:
            scored.append((hits, point))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{**p, "score": hits / len(words)} for hits, p in scored[:limit]]


def search(query: str, limit: int = 6) -> dict[str, Any]:
    conf = config()
    if not conf.enabled:
        return {"status": "not_configured"}
    names = collection_names(conf)
    if not names:
        return {"status": "empty", "hint": "В Кванте нет ни одной коллекции."}

    found: list[dict[str, Any]] = []
    modes: list[str] = []
    problems: list[str] = []
    for name in names:
        info = collection_info(name, conf)
        model = _embedding_model(conf, info)
        points: list[dict] = []
        if model and info.size:
            try:
                vector = embed(query, conf, model)
                if len(vector) != info.size:
                    raise QuantError(
                        f"модель {model} даёт вектор {len(vector)}, а в коллекции «{name}» — {info.size}. "
                        "Укажите в QDRANT_EMBEDDING_MODEL ту модель, которой наполняли базу."
                    )
                points = _vector_search(vector, info, conf, limit)
                modes.append(f"{name}: по смыслу ({model})")
            except QuantError as exc:
                problems.append(str(exc))
        if not points:
            points = _text_search(query, info, conf, limit)
            modes.append(f"{name}: по словам")
        found.extend(describe_point(p, name, conf) for p in points)

    found.sort(key=lambda r: r.get("score") or 0, reverse=True)
    response: dict[str, Any] = {
        "status": "ok" if found else "not_found",
        "query": query,
        "search_mode": "; ".join(modes),
        "results": found[:limit],
    }
    if problems:
        response["warnings"] = problems
    return response


# --- проверка ----------------------------------------------------------------


def probe(query: str = "тарифы") -> list[str]:
    """Что видно в Кванте — для самопроверки и `python -m app.quant`."""
    conf = config()
    if not conf.enabled:
        return ["QDRANT_URL не задан — Квант не подключён."]
    lines = [f"Адрес: {conf.url}", f"Ключ: {'задан' if conf.api_key else 'НЕ задан'}"]
    reset_cache()
    names = collection_names(conf)
    lines.append(f"Коллекции: {', '.join(names) or 'нет'}")
    for name in names:
        info = collection_info(name, conf)
        vector = f"вектор «{info.vector_name}»" if info.vector_name else "вектор без имени"
        lines.append(f"[{name}] записей: {info.points}, {vector}, размер {info.size or '?'}")
        model = _embedding_model(conf, info)
        lines.append(
            f"[{name}] модель эмбеддингов: {model or 'НЕ определена — будет поиск по словам'}"
            + ("" if conf.embedding_model or not model else " (угадана по размеру — задайте QDRANT_EMBEDDING_MODEL)")
        )
        sample = _request(
            "POST", f"/collections/{name}/points/scroll", conf,
            {"limit": 1, "with_payload": True, "with_vector": False},
        )
        points = (sample or {}).get("points", []) if isinstance(sample, dict) else []
        if points:
            payload = points[0].get("payload") or {}
            keys = sorted(payload)
            meta = payload.get("metadata")
            lines.append(f"[{name}] поля записи: {', '.join(keys)}")
            if isinstance(meta, dict):
                lines.append(f"[{name}] поля metadata: {', '.join(sorted(meta))}")
            view = describe_point(points[0], name, conf)
            lines.append(
                f"[{name}] пример: «{view['title']}», дата {view['date'] or '—'}, "
                f"ссылка {'есть' if view['link'] else 'нет'}, текст {len(view['text'])} симв."
            )
    result = search(query, limit=3)
    lines.append(f"Пробный поиск «{query}»: {result.get('status')}, {result.get('search_mode', '')}")
    for warning in result.get("warnings", []):
        lines.append(f"  ! {warning}")
    for item in result.get("results", []):
        lines.append(f"  — {item['title']} (score {item.get('score')})")
    return lines


def main() -> int:
    from . import console

    console.setup()
    query = " ".join(sys.argv[1:]) or "тарифы"
    try:
        for line in probe(query):
            print(line)
    except QuantError as exc:
        print(f"ОШИБКА: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
