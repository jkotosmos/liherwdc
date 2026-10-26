"""Проверка «работает ли у всех»: общая часть один раз и затем каждый пользователь.

/check в Telegram показывает состояние того, кто спросил. Здесь — сразу все из
белого списка (TELEGRAM_ALLOWED_USERS и добавленные /adduser):

* Telegram — может ли бот ему писать (нажимал ли он «Старт»): иначе утренняя
  сводка и напоминания до него не дойдут;
* Google — подключён ли его календарь и Диск и каким способом;
* «Квант» — какая у него роль на сайте базы знаний (что ему можно показывать).

Запуск на сервере (ничего не меняет и секретов не печатает):
    docker compose exec bot python -m app.users_check
"""

from __future__ import annotations

from typing import Any

from . import console
from .config import settings
from .integrations import accounts
from .selfcheck import FAIL, MARK, OK, WARN


def _telegram(api: Any, user_id: int) -> tuple[str, str]:
    from .telegram.api import TelegramError

    try:
        chat = api.get_chat(user_id)
    except TelegramError as exc:
        text = str(exc)
        if "chat not found" in text.lower():
            return FAIL, "не нажимал «Старт» — бот не может ему писать (сводка и напоминания не дойдут)"
        if "blocked" in text.lower():
            return FAIL, "заблокировал бота — сообщения не доходят"
        return WARN, f"не удалось проверить: {text[:120]}"
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
    username = f"@{chat['username']}" if chat.get("username") else ""
    return OK, "чат открыт" + (f" ({', '.join(p for p in (name, username) if p)})" if name or username else "")


def _google(user_id: int) -> tuple[str, str]:
    from .integrations import google_client

    with accounts.use(str(user_id)):
        try:
            status = google_client.status()
        except Exception as exc:  # noqa: BLE001 — проверка не должна падать
            return WARN, f"не удалось проверить: {type(exc).__name__}: {str(exc)[:120]}"
    if status.get("connected"):
        how = {"delegated": "делегирование организации", "shared": "открытый боту календарь"}.get(
            status.get("mode") or "", "вход через Google"
        )
        return OK, f"подключён: {status.get('account_hint') or 'почта определена'} ({how})"
    return FAIL, "не подключён — пусть отправит боту /auth"


def _quant(user_id: int) -> tuple[str, str]:
    from . import quant, site_db

    conf = quant.config()
    if not conf.enabled:
        return WARN, "«Квант» не подключён на сервере"
    if not site_db.enabled():
        return OK, "роли с сайта не используются — " + (
            "всем роли из QDRANT_ROLES" if conf.roles else "видит все документы"
        )
    found = site_db.access(str(user_id))
    if found.state == "role":
        return OK, f"роль на сайте: {found.role_name or found.role_id}"
    if conf.roles and found.state != "blocked":
        return OK, "роли на сайте нет — действует роль по умолчанию (QDRANT_ROLES)"
    return FAIL, site_db.explain(found, str(user_id))


def users() -> list[tuple[int, str]]:
    """Все, кому открыт бот: (ID, откуда доступ)."""
    from .telegram import access

    owner = accounts.owner_id()
    result = []
    for uid in sorted(access.admins()):
        result.append((uid, "владелец" if str(uid) == owner else "белый список"))
    for uid in sorted(access.extra()):
        result.append((uid, "добавлен /adduser"))
    return result


def common() -> list[tuple[str, str, str]]:
    """То, что одинаково для всех: бот, модель, вход Google, «Квант», база сайта."""
    from . import selfcheck

    rows: list[tuple[str, str, str]] = []
    for title, runner in (
        ("Telegram", selfcheck.check_telegram),
        ("Модель", selfcheck.check_model),
        ("Google", selfcheck.check_google),
        ("Квант", selfcheck.check_quant),
    ):
        try:
            checks = runner()
        except Exception as exc:  # noqa: BLE001
            rows.append((FAIL, title, f"проверка сорвалась: {type(exc).__name__}: {exc}"))
            continue
        for check in checks:
            # В общей части — только то, что не зависит от человека.
            if title == "Google" and check.name not in ("Google: вход через Google", "Google: сервисный аккаунт",
                                                        "Google: OAuth-клиент"):
                continue
            rows.append((check.status, check.name, check.detail))
    return rows


def main() -> int:
    marks = console.setup()
    rule = marks["rule"]
    print("Проверка бота OPERON по всем пользователям\n")
    print(f"{rule} Общее " + "-" * 52)
    failures = 0
    for status, name, detail in common():
        print(f"[{MARK[status]}] {name}: {detail}")
        failures += status == FAIL
    print()

    if not settings.telegram_token:
        print("TELEGRAM_BOT_TOKEN не задан — пользователей проверить нельзя.")
        return 2
    from .telegram.api import TelegramAPI

    api = TelegramAPI(settings.telegram_token, timeout=20.0)
    people = users()
    try:
        for uid, source in people:
            print(f"{rule} {uid} ({source}) " + "-" * max(4, 44 - len(str(uid)) - len(source)))
            for title, (status, detail) in (
                ("Telegram", _telegram(api, uid)),
                ("Google", _google(uid)),
                ("Квант", _quant(uid)),
            ):
                print(f"[{MARK[status]}] {title}: {detail}")
                failures += status == FAIL
            print()
    finally:
        api.close()

    print("=" * 62)
    print(f"Пользователей: {len(people)}. " + ("Сбоев нет." if not failures else f"Сбоев: {failures} — см. строки СБОЙ."))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
