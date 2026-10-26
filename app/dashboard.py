"""Пульт управления в Mini App: что происходит сегодня — одним экраном.

Только чтение. Любое изменение — встреча, поручение, модель — делается через
чат: там оно попадает в карточку подтверждения, как и раньше. Пульт лишь
готовит текст запроса, человек видит его и сам нажимает «Отправить».

Каждый раздел считается отдельно: если один не отвечает (Google, База сайта,
шлюз), остальные всё равно показываются, а сбой описан в `errors`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable

from . import model_choice, model_families, quant, routerai, site_db
from .config import settings
from .integrations import accounts, google_client

logger = logging.getLogger(__name__)

LIBRARY_TTL = 300
_library_cache: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
_library_lock = threading.Lock()

SOURCE_LABELS = {"google_drive": "Google Drive", "file": "Файл", "topic": "Тема"}
STATUS_ORDER = {"critical": 0, "warning": 1, "ok": 2, "no_data": 3}


def _now() -> datetime:
    return datetime.now(settings.tz)


def _section(errors: dict[str, str], name: str, build: Callable[[], Any], default: Any) -> Any:
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 — один раздел не должен ронять пульт
        logger.warning("Пульт: раздел «%s» не собран: %s", name, exc)
        errors[name] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return default


def _hm(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%H:%M") if value else ""
    except ValueError:
        return ""


def _ru_date(value: date) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"{days[value.weekday()]}, {value.day} {months[value.month - 1]}"


# --- Google: календарь на сегодня ---------------------------------------------


def _google(account: str) -> dict[str, Any]:
    with accounts.use(account):
        status = google_client.status()
    return {
        "connected": bool(status.get("connected")),
        "mode": status.get("mode") or "",
        "account": status.get("account_hint") or "",
        "reason": status.get("reason") or "",
        "hint": status.get("hint") or "",
    }


def _meetings(account: str, day: date, google: dict[str, Any]) -> dict[str, Any]:
    if not google["connected"]:
        return {"available": False, "reason": google["reason"] or "Google не подключён", "items": []}
    from .tools import calendar as calendar_tool

    with accounts.use(account):
        events = calendar_tool.list_events_between(day, day)
    items = []
    for event in events:
        all_day = bool(event.get("all_day"))
        items.append({
            "id": event.get("event_id"),
            "title": event.get("title") or "(без названия)",
            "all_day": all_day,
            "time": "весь день" if all_day else _hm(event.get("start")),
            "end": "" if all_day else _hm(event.get("end")),
            "location": event.get("location") or "",
            "attendees": len(event.get("attendees") or []),
            "organizer": event.get("organizer") or "",
        })
    items.sort(key=lambda m: (not m["all_day"], m["time"]))
    return {"available": True, "items": items}


# --- поручения ----------------------------------------------------------------


def _tasks(today: date) -> dict[str, Any]:
    from .tools import tasks as tasks_tool

    active = tasks_tool.load_active()
    rows = []
    for task in active:
        due = task.get("due_date")
        days = task.get("days_left")
        rows.append({
            "id": task.get("id"),
            "title": task.get("title") or "",
            "assignee": task.get("assignee") or "",
            "due": due or "",
            "due_ru": date.fromisoformat(due).strftime("%d.%m.%Y") if due else "",
            "days_left": days,
            "overdue": bool(task.get("overdue")),
            "priority": task.get("priority") or "normal",
            "status": task.get("status") or "open",
            "status_ru": task.get("status_ru") or "",
        })
    rows.sort(key=lambda t: (not t["overdue"], t["due"] or "9999-12-31", t["id"] or ""))
    return {
        "open": len(rows),
        "overdue": sum(1 for t in rows if t["overdue"]),
        "today": sum(1 for t in rows if t["days_left"] == 0),
        "week": sum(1 for t in rows if t["days_left"] is not None and 0 <= t["days_left"] <= 7),
        "items": rows,
    }


# --- показатели (KPI) -----------------------------------------------------------


def _kpi() -> dict[str, Any]:
    from .tools import kpi as kpi_tool

    everything = kpi_tool._load()
    deviations = kpi_tool.load_deviations()
    items = []
    for item in sorted(deviations, key=lambda k: (STATUS_ORDER.get(k.get("status"), 9), k.get("name") or "")):
        items.append({
            "name": item.get("name") or "",
            "period": item.get("period") or "",
            "status": item.get("status") or "",
            "status_ru": item.get("status_ru") or "",
            "plan": item.get("plan"),
            "fact": item.get("fact"),
            "deviation_pct": item.get("deviation_pct"),
            "unit": item.get("unit") or "",
        })
    return {
        "total": len(everything),
        "critical": sum(1 for k in items if k["status"] == "critical"),
        "warning": sum(1 for k in items if k["status"] == "warning"),
        "items": items[:8],
    }


# --- напоминания ------------------------------------------------------------------


def _reminders(account: str) -> dict[str, Any]:
    from .tools import remind

    mine = [
        r for r in remind._load()
        if r.get("status") == "active" and remind._same_person(r.get("account", ""), account)
    ]
    mine.sort(key=lambda r: r.get("at") or "")
    return {
        "active": len(mine),
        "items": [
            {"id": r.get("id"), "text": r.get("text") or "", "at": (r.get("at") or "")[:16].replace("T", " "),
             "repeat": r.get("repeat") or ""}
            for r in mine[:5]
        ],
    }


# --- база знаний «Квант» ------------------------------------------------------------


def _source_label(payload: dict[str, Any]) -> str:
    raw = quant._first(quant._layers(payload), ("source",))
    return SOURCE_LABELS.get(raw, "Документ")


def _iso_day(text: str) -> str:
    """Дата для сортировки: «dd.mm.yyyy» или «yyyy-mm-dd» → «yyyy-mm-dd»; иначе «»."""
    text = (text or "").strip()
    try:
        if len(text) >= 10 and text[2:3] == "." and text[5:6] == ".":
            return datetime.strptime(text[:10], "%d.%m.%Y").strftime("%Y-%m-%d")
        return datetime.fromisoformat(text[:10]).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _ru_day(text: str) -> str:
    iso = _iso_day(text)
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y") if iso else ""


def _library(account: str) -> dict[str, Any]:
    """Все документы, доступные этому человеку (по его роли на сайте). Кэш 5 минут."""
    conf = quant.config()
    if not conf.enabled:
        return {"enabled": False}
    with accounts.use(account):
        conf, scope = quant._with_access(conf)
        if scope and scope.get("denied"):
            return {"enabled": True, "access": False, "reason": scope["denied"], "documents": 0, "items": []}
        role = (scope or {}).get("role", "")
        cache_key = (account, tuple(conf.roles))
        with _library_lock:
            cached = _library_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < LIBRARY_TTL:
            return cached[1]

        groups: dict[str, dict[str, Any]] = {}
        points_total = 0
        for name in conf.collections or quant.collection_names(conf):
            for point in quant._scroll(name, conf, cap=5000):
                points_total += 1
                if conf.roles and not set(quant._roles_of(point)) & set(conf.roles):
                    continue
                view = quant.describe_point(point, name, conf)
                key = str(view.get("file_id") or view.get("topic_id") or view.get("title"))
                group = groups.get(key)
                if group is None:
                    group = {
                        "title": view.get("title") or "(без названия)",
                        "source": _source_label(point.get("payload") or {}),
                        "link": view.get("link") or "",
                        "date": _ru_day(view.get("date") or ""),
                        "date_meaning": "",
                        "file_id": view.get("file_id") or "",
                        "topic_id": view.get("topic_id") or "",
                        "topic_status": "",
                        "fragments": 0,
                    }
                    groups[key] = group
                group["fragments"] += 1
                if not group["link"] and view.get("link"):
                    group["link"] = view["link"]

        items = list(groups.values())
        try:
            site_db.add_dates(items)  # даты и статусы тем — с сайта базы знаний
        except Exception as exc:  # noqa: BLE001 — без дат список всё равно полезен
            logger.warning("Пульт: даты документов с сайта не получены: %s", exc)
        for item in items:
            item["date"] = _ru_day(item.get("date") or "")
            item["topic_status"] = item.get("topic_status") or ""

        items.sort(key=lambda d: (_iso_day(d["date"]) or "0000-00-00", d["title"]), reverse=True)
        dated = [d for d in items if d["date"]]
        result = {
            "enabled": True,
            "access": True,
            "role": role,
            "documents": len(items),
            "fragments": points_total,
            "dated": len(dated),
            "latest": dated[0]["date"] if dated else "",
            "by_source": {k: sum(1 for d in items if d["source"] == k) for k in sorted({d["source"] for d in items})},
            "items": items[:300],
        }
    with _library_lock:
        _library_cache[cache_key] = (time.monotonic(), result)
    return result


def _knowledge(account: str, errors: dict[str, str]) -> dict[str, Any]:
    lib = _section(errors, "База знаний", lambda: _library(account), {"enabled": False})
    if not lib.get("enabled"):
        return {"enabled": False, "items": []}
    collections = _section(errors, "Квант: коллекции", lambda: _collections(), [])
    return {**lib, "items": lib.get("items", [])[:6], "collections": collections}


def _collections() -> list[dict[str, Any]]:
    conf = quant.config()
    out = []
    for name in conf.collections or quant.collection_names(conf):
        info = quant.collection_info(name, conf)
        out.append({"name": name, "points": info.points, "size": info.size})
    return out


# --- модель и расходы ----------------------------------------------------------------


def _model() -> dict[str, Any]:
    chosen = model_choice.choice()
    model_id = chosen.get("model", "")
    info: dict[str, Any] = {"id": model_id, "family": model_families.label(model_families.family(model_id)),
                            "allowed": model_families.is_allowed(model_id)}
    if routerai.available():
        found = routerai.find_model(model_id)
        if found:
            info.update({"name": found.get("name") or model_id, "price_in": found.get("price_in"),
                         "price_out": found.get("price_out"), "context": found.get("context_length")})
    info.setdefault("name", model_id)
    return info


def _billing() -> dict[str, Any]:
    if not routerai.available():
        return {"available": False}
    data = routerai.billing()
    return {"available": True, "currency": data.get("currency") or routerai.currency(),
            "balance": data.get("balance"), "key_usage": data.get("key_usage"),
            "key_usage_monthly": data.get("key_usage_monthly"), "key_limit": data.get("key_limit"),
            "errors": data.get("errors") or []}


# --- сборка ---------------------------------------------------------------------------


def summary(account: str) -> dict[str, Any]:
    now = _now()
    today = now.date()
    errors: dict[str, str] = {}
    google = _section(errors, "Google", lambda: _google(account), {"connected": False, "reason": "не проверено"})
    return {
        "today": {"iso": today.isoformat(), "label": _ru_date(today), "time": now.strftime("%H:%M")},
        "google": google,
        "meetings": _section(errors, "Календарь", lambda: _meetings(account, today, google),
                             {"available": False, "reason": "не проверено", "items": []}),
        "tasks": _section(errors, "Поручения", lambda: _tasks(today),
                          {"open": 0, "overdue": 0, "today": 0, "week": 0, "items": []}),
        "kpi": _section(errors, "Показатели", _kpi, {"total": 0, "critical": 0, "warning": 0, "items": []}),
        "reminders": _section(errors, "Напоминания", lambda: _reminders(account), {"active": 0, "items": []}),
        "knowledge": _knowledge(account, errors),
        "model": _section(errors, "Модель", _model, {"id": "", "name": "", "allowed": False}),
        "billing": _section(errors, "Баланс", _billing, {"available": False}),
        "errors": errors,
    }


def documents(account: str, limit: int = 300) -> dict[str, Any]:
    errors: dict[str, str] = {}
    lib = _section(errors, "База знаний", lambda: _library(account), {"enabled": False})
    return {**{k: v for k, v in lib.items() if k != "items"},
            "items": lib.get("items", [])[:limit], "errors": errors}
