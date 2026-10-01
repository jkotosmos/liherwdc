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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

# Импорт config загружает .env — до первого чтения QDRANT_* из окружения.
from .config import ROUTERAI_BASE_URL, settings
from .net import explain, http_options

TIMEOUT = httpx.Timeout(25.0, connect=15.0)
# Сведения о коллекции Qdrant отдаёт не сразу, пока идёт индексация. Ждать
# их долго незачем: размер вектора видно и по одной записи.
INFO_TIMEOUT = httpx.Timeout(12.0, connect=10.0)
INFO_TTL_SECONDS = 600

TEXT_KEYS = ("page_content", "pageContent", "content", "text", "chunk", "chunk_text", "document", "body")
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
    # Роли, документы которых боту можно показывать (metadata.accessibleByRoles).
    # Пусто — без фильтра.
    roles: tuple[str, ...] = ()
    roles_field: str = "metadata.accessibleByRoles"

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
        roles=tuple(r.strip() for r in env("QDRANT_ROLES").split(",") if r.strip()),
        roles_field=env("QDRANT_ROLES_FIELD") or "metadata.accessibleByRoles",
    )


# --- HTTP --------------------------------------------------------------------


def _c(name: str) -> str:
    """Имя коллекции в адресе: кириллица и пробелы — в %-кодировке."""
    return "/collections/" + quote(name, safe="")


def _request(
    method: str, path: str, conf: Config, body: dict | None = None, timeout: httpx.Timeout = TIMEOUT
) -> Any:
    headers = {"api-key": conf.api_key} if conf.api_key else {}
    try:
        response = httpx.request(
            method, conf.url + path, json=body, headers=headers, timeout=timeout, **http_options()
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
    try:
        result = _request("GET", _c(name), conf, timeout=INFO_TIMEOUT) or {}
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
    except QuantError:
        info = _info_from_sample(name, conf)
    with _lock:
        _info_cache[name] = (time.monotonic(), info)
    return info


def _info_from_sample(name: str, conf: Config) -> CollectionInfo:
    """Размер и имя вектора — по одной записи, когда сведения о коллекции не пришли."""
    sample = _request(
        "POST", _c(name) + "/points/scroll", conf, {"limit": 1, "with_payload": False, "with_vector": True}
    )
    points = (sample or {}).get("points", []) if isinstance(sample, dict) else []
    vector = points[0].get("vector") if points else None
    vector_name, size = "", 0
    if isinstance(vector, list):
        size = len(vector)
    elif isinstance(vector, dict) and vector:
        vector_name = conf.vector_name if conf.vector_name in vector else next(iter(vector))
        value = vector.get(vector_name)
        size = len(value) if isinstance(value, list) else 0
    return CollectionInfo(name=name, points=-1, vector_name=vector_name, size=size)


def reset_cache() -> None:
    global _names_cache
    with _lock:
        _info_cache.clear()
        _names_cache = None
        _model_checks.clear()


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


def _access_filter(conf: Config) -> list[dict]:
    """Только документы, доступные ролям из QDRANT_ROLES: ТЗ — «в рамках прав»."""
    if not conf.roles:
        return []
    return [{"key": conf.roles_field, "match": {"any": list(conf.roles)}}]


def _vector_search(query_vector: list[float], info: CollectionInfo, conf: Config, limit: int) -> list[dict]:
    vector: Any = {"name": info.vector_name, "vector": query_vector} if info.vector_name else query_vector
    result = _request(
        "POST",
        _c(info.name) + "/points/search",
        conf,
        {"vector": vector, "limit": limit, "with_payload": True}
        | ({"filter": {"must": _access_filter(conf)}} if conf.roles else {}),
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


def _scroll(name: str, conf: Config, *, with_vector: bool = False, cap: int = 2000) -> list[dict]:
    """Все записи коллекции (до cap) — постранично."""
    points: list[dict] = []
    offset: Any = None
    while len(points) < cap:
        body: dict[str, Any] = {"limit": min(256, cap - len(points)), "with_payload": True, "with_vector": with_vector}
        if offset is not None:
            body["offset"] = offset
        result = _request("POST", _c(name) + "/points/scroll", conf, body)
        if not isinstance(result, dict):
            break
        points.extend(result.get("points") or [])
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return points


def _text_search(query: str, info: CollectionInfo, conf: Config, limit: int) -> list[dict]:
    """Поиск по словам без полнотекстового индекса: перебираем записи и считаем совпадения.

    Фильтр Qdrant «match text» без индекса на поле ведёт себя по-разному в
    разных версиях, а база OPERON невелика — надёжнее посчитать самим.
    """
    words = keywords(query)
    if not words:
        return []
    allowed = set(conf.roles)
    scored = []
    for point in _scroll(info.name, conf):
        payload = point.get("payload") or {}
        if allowed:
            roles = (payload.get("metadata") or {}).get("accessibleByRoles") or payload.get("accessibleByRoles") or []
            roles = roles if isinstance(roles, list) else [roles]
            if not allowed & {str(r) for r in roles}:
                continue
        text = json.dumps(payload, ensure_ascii=False).lower()
        hits = sum(1 for word in words if word in text)
        if hits:
            scored.append((hits, point))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{**p, "score": round(hits / len(words), 3)} for hits, p in scored[:limit]]


# --- та ли модель эмбеддингов ------------------------------------------------

# Вектор того же текста той же моделью совпадает почти полностью (≈1.0).
# Другая модель того же размера даёт около нуля — поиск тогда случаен.
SAME_MODEL_COSINE = 0.9
_model_checks: dict[tuple[str, str], tuple[float, float | None]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return dot / norm if norm else 0.0


def _stored_sample(info: CollectionInfo, conf: Config) -> tuple[str, list[float]] | None:
    result = _request(
        "POST", _c(info.name) + "/points/scroll", conf, {"limit": 5, "with_payload": True, "with_vector": True}
    )
    for point in (result or {}).get("points", []) if isinstance(result, dict) else []:
        vector = point.get("vector")
        if isinstance(vector, dict):
            vector = vector.get(info.vector_name) if info.vector_name else next(iter(vector.values()), None)
        text = describe_point(point, info.name, conf)["text"]
        if isinstance(vector, list) and vector and text:
            return text, vector
    return None


def model_match(info: CollectionInfo, conf: Config, model: str) -> float | None:
    """Насколько модель совпадает с той, что наполняла базу: 1.0 — та же. None — проверить нечем."""
    key = (info.name, model)
    cached = _model_checks.get(key)
    if cached and time.monotonic() - cached[0] < INFO_TTL_SECONDS:
        return cached[1]
    sample = _stored_sample(info, conf)
    if sample is None:
        value = None
    else:
        text, stored = sample
        mine = embed(text, conf, model)
        value = round(_cosine(mine, stored), 3) if len(mine) == len(stored) else 0.0
    _model_checks[key] = (time.monotonic(), value)
    return value


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
                match = model_match(info, conf, model)
                if match is not None and match < SAME_MODEL_COSINE:
                    raise QuantError(
                        f"база «{name}» наполнена другой моделью эмбеддингов, не {model} "
                        f"(совпадение {match}). Укажите в QDRANT_EMBEDDING_MODEL ту, что использовал "
                        "загрузчик; пока ищу по словам."
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


# Модели размером 3072, которые стоит проверить, если своя не подошла.
CANDIDATES_3072 = ("openai/text-embedding-3-large", "google/gemini-embedding-001")


def probe(query: str = "тарифы", extra_models: tuple[str, ...] = ()) -> Iterator[str]:
    """Что видно в Кванте — для самопроверки и `python -m app.quant`.

    Строки отдаются по мере готовности, с временем шага: если сервер
    задумается, будет видно, на чём именно.
    """
    conf = config()
    if not conf.enabled:
        yield "QDRANT_URL не задан — Квант не подключён."
        return
    yield f"Адрес: {conf.url}"
    yield f"Ключ: {'задан' if conf.api_key else 'НЕ задан'}"
    reset_cache()

    started = time.monotonic()

    def took() -> str:
        nonlocal started
        now = time.monotonic()
        text = f" ({now - started:.1f} с)"
        started = now
        return text

    names = collection_names(conf)
    yield f"Коллекции: {', '.join(names) or 'нет'}{took()}"
    for name in names:
        yield f"[{name}] читаю настройки коллекции…"
        info = collection_info(name, conf)
        if not extra_models and info.size == 3072:
            extra_models = CANDIDATES_3072
        vector = f"вектор «{info.vector_name}»" if info.vector_name else "вектор без имени"
        points = "?" if info.points < 0 else info.points
        note = " — настройки не ответили, размер взят по записи" if info.points < 0 else ""
        yield f"[{name}] записей: {points}, {vector}, размер {info.size or '?'}{note}{took()}"
        model = _embedding_model(conf, info)
        yield (
            f"[{name}] модель эмбеддингов: {model or 'НЕ определена — будет поиск по словам'}"
            + ("" if conf.embedding_model or not model else " (угадана по размеру — задайте QDRANT_EMBEDDING_MODEL)")
        )
        sample = _request(
            "POST", _c(name) + "/points/scroll", conf,
            {"limit": 1, "with_payload": True, "with_vector": False},
        )
        points_list = (sample or {}).get("points", []) if isinstance(sample, dict) else []
        if points_list:
            payload = points_list[0].get("payload") or {}
            meta = payload.get("metadata")
            yield f"[{name}] поля записи: {', '.join(sorted(payload))}{took()}"
            if isinstance(meta, dict):
                yield f"[{name}] поля metadata: {', '.join(sorted(meta))}"
            view = describe_point(points_list[0], name, conf)
            yield (
                f"[{name}] пример: «{view['title']}», дата {view['date'] or '—'}, "
                f"ссылка {'есть' if view['link'] else 'нет'}, текст {len(view['text'])} симв."
            )
        else:
            # Пустая выборка: либо записей нет, либо ответ не того вида — покажем как есть.
            yield f"[{name}] выборка одной записи пуста; ответ сервера: {json.dumps(sample, ensure_ascii=False)[:300]}"
            try:
                counted = _request("POST", _c(name) + "/points/count", conf, {"exact": True})
                total = (counted or {}).get("count") if isinstance(counted, dict) else counted
                yield f"[{name}] точный подсчёт записей: {total}{took()}"
                if total == 0:
                    yield f"[{name}] коллекция ПУСТА — документы в Квант ещё не загружены, искать нечего."
            except QuantError as exc:
                yield f"[{name}] подсчёт записей не удался: {exc}"
        if points_list:
            everything = _scroll(name, conf)
            docs: dict[str, int] = {}
            roles: set[str] = set()
            for point in everything:
                title = describe_point(point, name, conf)["title"]
                docs[title] = docs.get(title, 0) + 1
                for layer in _layers(point.get("payload") or {}):
                    value = layer.get("accessibleByRoles")
                    for role in value if isinstance(value, list) else ([value] if value else []):
                        roles.add(str(role))
            listed = ", ".join(f"{title} ({count})" for title, count in sorted(docs.items(), key=lambda x: -x[1])[:20])
            yield f"[{name}] документов: {len(docs)} — {listed}"
            yield f"[{name}] роли в accessibleByRoles: {', '.join(sorted(roles)) or 'нет'}"

            candidates = list(dict.fromkeys([m for m in (_embedding_model(conf, info), *extra_models) if m]))
            for candidate in candidates:
                try:
                    match = model_match(info, conf, candidate)
                except QuantError as exc:
                    yield f"[{name}] модель {candidate}: проверить не удалось — {exc}"
                    continue
                if match is None:
                    yield f"[{name}] модель {candidate}: нет записи с текстом и вектором для сверки"
                elif match >= SAME_MODEL_COSINE:
                    yield f"[{name}] модель {candidate}: ✔ ТА ЖЕ, что у базы (совпадение {match})"
                else:
                    yield f"[{name}] модель {candidate}: ✘ другая (совпадение {match}, у той же было бы ≈1.0)"
    yield f"Пробный поиск «{query}»…"
    result = search(query, limit=3)
    yield f"Пробный поиск: {result.get('status')}, {result.get('search_mode', '')}{took()}"
    for warning in result.get("warnings", []):
        yield f"  ! {warning}"
    for item in result.get("results", []):
        yield f"  — {item['title']} (score {item.get('score')})"


def main() -> int:
    from . import console

    console.setup()
    args = sys.argv[1:]
    models: tuple[str, ...] = ()
    if "--models" in args:
        at = args.index("--models")
        models = tuple(m.strip() for m in (args[at + 1] if at + 1 < len(args) else "").split(",") if m.strip())
        del args[at:at + 2]
    query = " ".join(args) or "тарифы"
    try:
        for line in probe(query, models):
            print(line, flush=True)
    except QuantError as exc:
        print(f"ОШИБКА: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
