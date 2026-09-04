"""Проверка всей системы одной командой.

    python -m app.selfcheck

Отвечает на единственный вопрос: заработает ли ассистент прямо сейчас — и
если нет, что именно чинить. Проверяются настройки, шлюз модели, Telegram,
Google, интернет-поиск и база знаний.

Зачем отдельно от тестов: тесты доказывают, что код делает задуманное, но
ничего не говорят о том, приняли ли ключ, дошли ли до сети и выдал ли Google
нужные разрешения. Всё это выясняется только живым обращением.

Ничего не меняет: только читает. Запускать можно и локально, и на сервере.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import settings

TIMEOUT = 25.0

OK = "OK"
WARN = "ВНИМАНИЕ"
FAIL = "СБОЙ"
SKIP = "пропуск"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hints: list[str] = field(default_factory=list)
    # Без этого ассистент не работает вообще, а не «работает хуже».
    blocking: bool = False


def _http(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(url, timeout=TIMEOUT, **kwargs)


# --- настройки --------------------------------------------------------------


def check_settings() -> list[Check]:
    checks: list[Check] = []

    if settings.api_key:
        checks.append(Check("Ключ доступа к модели", OK, f"провайдер {settings.provider}"))
    else:
        checks.append(
            Check(
                "Ключ доступа к модели",
                FAIL,
                "не задан",
                ["Задайте ROUTERAI_API_KEY (или OPERON_LLM_API_KEY)."],
                blocking=True,
            )
        )

    if settings.model:
        checks.append(Check("Имя модели", OK, settings.model))
    else:
        checks.append(
            Check(
                "Имя модели",
                FAIL,
                "не задано",
                ["Задайте OPERON_MODEL. Список моделей покажет: python -m app.probe"],
                blocking=True,
            )
        )

    if settings.auth_required:
        checks.append(Check("Пароль на веб-чат", OK, "задан"))
    else:
        checks.append(
            Check(
                "Пароль на веб-чат",
                WARN,
                "не задан",
                [
                    "Локально это допустимо. На сервере приложение откажется "
                    "слушать внешний адрес: OPERON_ACCESS_PASSWORD обязателен."
                ],
            )
        )

    from .integrations import token_store

    if token_store.encryption_enabled():
        checks.append(Check("Шифрование токена Google", OK, "OPERON_TOKEN_KEY задан"))
    else:
        checks.append(
            Check(
                "Шифрование токена Google",
                WARN,
                "OPERON_TOKEN_KEY не задан",
                ["На сервере задайте: доступ к диску иначе равен доступу к Google."],
            )
        )
    return checks


# --- модель -----------------------------------------------------------------


def check_model() -> list[Check]:
    if not (settings.api_key and settings.model):
        return [Check("Шлюз модели", SKIP, "нет ключа или имени модели")]

    from .probe import _try_anthropic, _try_openai

    protocol = settings.llm_protocol
    base = settings.base_url or "https://api.anthropic.com/v1"
    attempt = _try_openai if protocol == "openai" else _try_anthropic

    try:
        reachable, tools_work, note = attempt(base, settings.api_key, settings.model)
    except Exception as exc:  # noqa: BLE001 — сюда попадают и сетевые сбои
        return [
            Check(
                "Шлюз модели",
                FAIL,
                f"{exc.__class__.__name__}: {exc}",
                [f"Проверьте доступность {base} с этой машины."],
                blocking=True,
            )
        ]

    checks = []
    if reachable:
        checks.append(Check("Шлюз модели отвечает", OK, f"{base} ({protocol})"))
    else:
        return [
            Check(
                "Шлюз модели отвечает",
                FAIL,
                note,
                [
                    "Проверьте ключ и имя модели.",
                    "Каталог моделей покажет: python -m app.probe",
                ],
                blocking=True,
            )
        ]

    if tools_work:
        checks.append(Check("Модель вызывает инструменты", OK, "проверено живым вызовом"))
    else:
        checks.append(
            Check(
                "Модель вызывает инструменты",
                FAIL,
                note or "модель не вызвала инструмент",
                [
                    "Без вызова инструментов ассистент бесполезен: он не сможет "
                    "ни искать в базе знаний, ни спрашивать подтверждение.",
                    "Выберите другую модель из каталога провайдера.",
                ],
                blocking=True,
            )
        )
    return checks


# --- Telegram ---------------------------------------------------------------


def check_telegram() -> list[Check]:
    if not settings.telegram_token:
        return [Check("Telegram", SKIP, "TELEGRAM_BOT_TOKEN не задан — работает только веб")]

    checks: list[Check] = []
    try:
        response = _http(f"https://api.telegram.org/bot{settings.telegram_token}/getMe")
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return [
            Check(
                "Telegram: токен",
                FAIL,
                f"{exc.__class__.__name__}: {exc}",
                ["Проверьте доступность api.telegram.org с этой машины."],
            )
        ]

    if payload.get("ok"):
        bot = payload.get("result", {})
        checks.append(Check("Telegram: токен", OK, f"@{bot.get('username', '?')}"))
    else:
        return [
            Check(
                "Telegram: токен",
                FAIL,
                payload.get("description", "отклонён"),
                ["Перевыпустите токен у @BotFather: /mybots → бот → API Token."],
            )
        ]

    allowed = settings.telegram_allowed_users
    if allowed:
        checks.append(
            Check("Telegram: белый список", OK, f"{len(allowed)} чел.: {sorted(allowed)}")
        )
    else:
        checks.append(
            Check(
                "Telegram: белый список",
                FAIL,
                "TELEGRAM_ALLOWED_USERS не задан",
                [
                    "Без белого списка бот НЕ ЗАПУСТИТСЯ — это намеренно: он "
                    "работает с вашим Google-аккаунтом.",
                    "Свой ID узнайте у @userinfobot.",
                ],
            )
        )
    return checks


# --- Google -----------------------------------------------------------------


def check_google() -> list[Check]:
    from .integrations import google_client, google_oauth

    checks: list[Check] = []
    client = google_oauth.describe_client()

    if client["configured"]:
        checks.append(Check("Google: OAuth-клиент", OK, client["source"]))
        checks.append(
            Check(
                "Google: адрес возврата",
                OK if client["callback_mode"] else WARN,
                client["redirect_uri"],
                []
                if client["callback_mode"]
                else [
                    "Режим с копированием кода вручную. Для клиента типа "
                    "«Web application» задайте OPERON_PUBLIC_URL — код придёт "
                    "на сервер сам."
                ],
            )
        )
    else:
        checks.append(
            Check(
                "Google: OAuth-клиент",
                WARN,
                "не настроен",
                [
                    "Задайте GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET, либо "
                    "положите client_secret.json.",
                    "Без этого Диск и календарь недоступны; остальное работает.",
                ],
            )
        )
        return checks

    status = google_client.status()
    if not status.get("connected"):
        checks.append(
            Check(
                "Google: доступ выдан",
                WARN,
                str(status.get("reason", "токен не найден")),
                ["Пройдите авторизацию: команда /auth в боте."],
            )
        )
        return checks

    checks.append(
        Check("Google: доступ выдан", OK, status.get("account_hint") or "учётная запись определена")
    )

    # Живые запросы: выданный токен ещё не значит, что права те, что нужны.
    try:
        from .tools import registry

        content, is_error = registry.execute("drive_search", {"query": "", "max_results": 1})
        checks.append(
            Check("Google Drive: чтение", FAIL if is_error else OK, content[:120] if is_error else "запрос прошёл")
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Google Drive: чтение", FAIL, f"{exc.__class__.__name__}: {exc}"))

    try:
        from .tools import registry

        content, is_error = registry.execute("calendar_list_events", {"max_results": 1})
        checks.append(
            Check("Google Calendar: чтение", FAIL if is_error else OK, content[:120] if is_error else "запрос прошёл")
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Google Calendar: чтение", FAIL, f"{exc.__class__.__name__}: {exc}"))

    return checks


# --- интернет ---------------------------------------------------------------


def check_search() -> list[Check]:
    from .tools import registry
    import json as _json

    if "internet_search" not in registry.names():
        return [
            Check(
                "Интернет-поиск",
                OK,
                "используется серверный поиск Anthropic",
            )
        ]

    content, is_error = registry.execute("internet_search", {"query": "ставка ЦБ РФ"})
    if is_error:
        return [Check("Интернет-поиск", FAIL, content[:150])]

    payload = _json.loads(content)
    status = payload.get("status")
    if status == "not_configured":
        return [
            Check(
                "Интернет-поиск",
                WARN,
                "ключ поиска не задан",
                [
                    "Ассистент честно скажет «интернет недоступен» и ответит "
                    "только по внутренним данным — но пункт ТЗ про внешние "
                    "источники работать не будет.",
                    "Достаточно одного: TAVILY_API_KEY, BRAVE_API_KEY, "
                    "SERPER_API_KEY или GOOGLE_CSE_KEY + GOOGLE_CSE_ID.",
                ],
            )
        ]
    if status == "ok":
        return [
            Check(
                "Интернет-поиск",
                OK,
                f"{payload.get('provider', '?')}: найдено {payload.get('results_count', '?')}",
            )
        ]
    return [Check("Интернет-поиск", WARN, str(payload.get("hint", status))[:150])]


# --- база знаний ------------------------------------------------------------


def check_knowledge_base() -> list[Check]:
    from .kb import knowledge_base

    stats = knowledge_base.stats
    count = stats.get("documents", 0)
    if count:
        return [
            Check(
                "База знаний",
                OK,
                f"{count} документов в {stats.get('root')}"
                + (f", разделы: {', '.join(stats['categories'][:5])}" if stats.get("categories") else ""),
            )
        ]
    return [
        Check(
            "База знаний",
            WARN,
            f"пусто ({stats.get('root')})",
            [
                "Это не поломка: ассистент честно ответит «данных нет».",
                "Положите документы — читаются .md, .csv, .json, .yaml, "
                "а также .docx, .pdf, .xlsx, .pptx.",
            ],
        )
    ]


# --- вывод ------------------------------------------------------------------

MARK = {OK: "  OK  ", WARN: " ВНИМ ", FAIL: " СБОЙ ", SKIP: "  --  "}


def main() -> int:
    groups = [
        ("Настройки", check_settings),
        ("Модель", check_model),
        ("Telegram", check_telegram),
        ("Google", check_google),
        ("Интернет", check_search),
        ("База знаний", check_knowledge_base),
    ]

    print("Проверка ассистента OPERON\n")
    all_checks: list[Check] = []

    for title, runner in groups:
        print(f"— {title} " + "-" * (58 - len(title)))
        try:
            checks = runner()
        except Exception as exc:  # noqa: BLE001 — проверка не должна падать сама
            checks = [Check(title, FAIL, f"проверка сорвалась: {exc.__class__.__name__}: {exc}")]
        for check in checks:
            print(f"[{MARK[check.status]}] {check.name}: {check.detail}")
            for hint in check.hints:
                print(f"           → {hint}")
        all_checks.extend(checks)
        print()

    blocking = [c for c in all_checks if c.status == FAIL and c.blocking]
    failures = [c for c in all_checks if c.status == FAIL]
    warnings = [c for c in all_checks if c.status == WARN]

    print("=" * 62)
    if blocking:
        print("НЕ ЗАПУСТИТСЯ. Сначала почините:")
        for check in blocking:
            print(f"  • {check.name}: {check.detail}")
        return 2
    if failures:
        print("Запустится, но часть функций не работает:")
        for check in failures:
            print(f"  • {check.name}: {check.detail}")
        return 1
    if warnings:
        print("Готов к работе. Замечания (не мешают запуску):")
        for check in warnings:
            print(f"  • {check.name}: {check.detail}")
        return 0
    print("Всё проверено, замечаний нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
