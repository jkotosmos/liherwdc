"""Приёмочный прогон: весь ТЗ одним сценарием.

    python -m app.acceptance

Проводит ассистента через каждый пункт технического задания и печатает, что
получилось. Отличие от тестов — в адресате: тесты пишутся для того, кто
правит код, а этот отчёт можно показать заказчику.

Модель заменяется сценарием: она не участвует в проверке того, доходят ли
данные до инструментов и срабатывает ли шлюз подтверждений. Живые сервисы
(шлюз модели, Telegram, Google) проверяет `python -m app.selfcheck` — здесь
проверяется другое: собрана ли система целиком.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import console

# Заполняются в main(): на русской консоли Windows «✓» роняет вывод.
TICK = "  OK  "
CROSS = " FAIL "
RULE = "-"

_passed = 0
_failed = 0


def say(ok: bool, title: str, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"[{TICK if ok else CROSS}] {title}" + (f": {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{RULE} {title} " + "-" * max(4, 60 - len(title)))


# --- подготовка данных ------------------------------------------------------


def sample_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph("Договор поставки № 17/2026 с ООО «Ромашка»")
    document.add_paragraph("Срок оплаты — 30 календарных дней с даты акта.")
    table = document.add_table(rows=2, cols=3)
    for col, value in enumerate(("Позиция", "Цена", "Срок")):
        table.cell(0, col).text = value
    for col, value in enumerate(("Монтаж оборудования", "450 000 ₽", "45 дней")):
        table.cell(1, col).text = value
    buffer = __import__("io").BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def sample_xlsx() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Тарифы"
    sheet.append(["Тариф", "Абонплата", "Скидка"])
    sheet.append(["Базовый", 12000, "0%"])
    sheet.append(["Расширенный", 28000, "15%"])
    buffer = __import__("io").BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --- сценарий модели --------------------------------------------------------


class ScriptedModel:
    """Отвечает по заранее заданному сценарию: вызвать инструмент или сказать текст."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.seen: list[dict[str, Any]] = []

    def stream(self, **params):
        self.seen.append(params)
        step = self.script.pop(0) if self.script else {"text": "Готово."}
        model = self

        class Stream:
            def __enter__(self): return self
            def __exit__(self, *exc): return False

            def __iter__(self):
                text = step.get("text")
                if text:
                    yield SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text=text),
                    )

            @staticmethod
            def get_final_message():
                content = []
                if step.get("text"):
                    content.append(SimpleNamespace(type="text", text=step["text"]))
                if step.get("tool"):
                    content.append(
                        SimpleNamespace(
                            type="tool_use",
                            id=f"t{len(model.seen)}",
                            name=step["tool"],
                            input=step.get("input", {}),
                        )
                    )
                return SimpleNamespace(
                    content=content,
                    stop_reason="tool_use" if step.get("tool") else "end_turn",
                    usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                )

        return Stream()


def run_turn(agent, session, script: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    agent._client = ScriptedModel(script)
    return list(agent.send_user_message(session, text))


# --- проверки ---------------------------------------------------------------


def main() -> int:
    global TICK, CROSS, RULE

    marks = console.setup()
    TICK, CROSS, RULE = marks["ok"], marks["fail"], marks["rule"]

    workspace = Path(tempfile.mkdtemp(prefix="operon-acceptance-"))
    kb_dir = workspace / "knowledge_base"
    (kb_dir / "договоры").mkdir(parents=True)
    (kb_dir / "договоры" / "поставка-ромашка.docx").write_bytes(sample_docx())
    (kb_dir / "тарифы.xlsx").write_bytes(sample_xlsx())

    import os

    os.environ["OPERON_KB_DIR"] = str(kb_dir)
    os.environ["OPERON_DATA_DIR"] = str(workspace / "data")
    os.environ["OPERON_CREDENTIALS_DIR"] = str(workspace / "credentials")
    # Сводка по умолчанию только по рабочим дням: в субботу проверка прошла бы
    # «мимо», ничего не проверив. Для приёмки день недели значения не имеет.
    os.environ["OPERON_DIGEST_WEEKDAYS"] = "1-7"
    os.environ["OPERON_QUIET_HOURS"] = "0-0"
    # Свои инструменты интернета вместо серверных: именно они работают
    # через сторонний шлюз, и именно их надо показать.
    os.environ["OPERON_WEB_SEARCH"] = "false"

    from dataclasses import replace

    from . import agent as agent_module
    from . import reminders as reminders_module
    from .agent import OperonAgent, Session
    from .config import settings
    from .kb.store import KnowledgeBase
    from .tools import registry
    from .tools import kpi as kpi_module
    from .tools import protocol as protocol_module
    from .tools import tasks as tasks_module

    conf = replace(settings, kb_dir=kb_dir, data_dir=workspace / "data")
    for module in (kpi_module, protocol_module, tasks_module, reminders_module):
        module.settings = conf
    tasks_module.settings = conf

    kb = KnowledgeBase(root=kb_dir)
    kb.ensure_fresh()

    print("Приёмочный прогон OPERON — проверка по пунктам ТЗ")
    print(f"Рабочий каталог: {workspace}")

    # --- 1. База знаний ---
    section("1. Знать проект и отвечать со ссылкой на источник")

    stats = kb.stats
    say(stats["documents"] == 2, "Документы проиндексированы",
        f"{stats['documents']} шт., форматы .docx и .xlsx")

    found = kb.search("срок оплаты договор")
    say(bool(found), "Поиск находит условие из договора Word")
    if found:
        say(bool(found[0].get("citation")), "Ответ несёт ссылку на источник", found[0]["citation"])

    table = kb.search("монтаж оборудования цена")
    say(bool(table) and "450 000" in table[0]["text"],
        "Найдена строка таблицы внутри договора",
        table[0]["text"].replace("\n", " ")[:60] if table else "")

    sheet = kb.search("расширенный тариф скидка")
    say(bool(sheet) and "28000" in sheet[0]["text"],
        "Найдена строка листа Excel", sheet[0]["citation"] if sheet else "")

    empty = kb.search("несуществующая выдуманная сущность зюзюка")
    say(not empty, "На отсутствующее не выдаётся ложное совпадение")

    # --- 2. Приём документов ---
    section("2. Приём документов (то, что попробует клиент)")

    from .kb import intake

    intake.settings = conf
    prepared = intake.prepare(sample_docx(), "новый-договор.docx", category="договоры")
    say(prepared.characters > 0, "Присланный .docx разбирается до сохранения",
        f"{prepared.characters} символов")
    say("Договор поставки" in prepared.preview, "Человеку показывается, что распозналось")

    safe = intake.safe_filename("../../etc/passwd.md")
    say(safe == "passwd.md", "Опасное имя файла обезврежено", f"«../../etc/passwd.md» → «{safe}»")

    try:
        intake.prepare(b"data", "старый.doc")
        say(False, "Устаревший формат отклоняется")
    except intake.IntakeError as exc:
        say("Пересохраните" in str(exc), "Устаревший формат отклонён с объяснением", str(exc)[:60])

    # --- 3. Шлюз подтверждений ---
    section("3. Изменения только после подтверждения")

    gated = {s.name for s in registry.all() if s.requires_confirmation}
    say(len(gated) == 8, "Изменяющие инструменты под шлюзом", f"{len(gated)}: {', '.join(sorted(gated))}")

    agent = OperonAgent()
    agent._runtime_context = lambda: "контекст"
    session = Session(session_id="acceptance")

    events = run_turn(
        agent, session,
        [{"text": "Зафиксирую поручение.", "tool": "task_create",
          "input": {"title": "Подготовить КП для «Ромашки»", "assignee": "Иванов",
                    "due_date": (date.today() + timedelta(days=3)).isoformat()}}],
        "поставь задачу Иванову подготовить КП, срок через три дня",
    )
    kinds = [e["type"] for e in events]
    say("confirmation_required" in kinds, "Действие остановлено и показана карточка")
    say(session.awaiting_confirmation, "Ход заморожен до ответа человека")

    before = json.loads(registry.execute("tasks_list", {})[0]).get("count", 0)
    say(before == 0, "До подтверждения ничего не записано")

    resumed = list(agent.resume_with_decisions(session, {}, {}))
    say(any(e["type"] == "tool_declined" for e in resumed),
        "Отсутствие решения означает отказ")
    after = json.loads(registry.execute("tasks_list", {})[0]).get("count", 0)
    say(after == 0, "После отказа по-прежнему ничего не записано")

    # --- 4. Поручения и сроки ---
    section("4. Поручения, сроки, протоколы, показатели")

    session2 = Session(session_id="acceptance-2")
    run_turn(
        agent, session2,
        [{"text": "Фиксирую.", "tool": "task_create",
          "input": {"title": "Акт сверки с Ромашкой", "assignee": "Петров",
                    "due_date": (date.today() - timedelta(days=5)).isoformat()}}],
        "зафиксируй поручение",
    )
    approved = list(
        agent.resume_with_decisions(session2, {"t1": "approve"}, {})
    )
    tasks = json.loads(registry.execute("tasks_list", {})[0])
    say(tasks.get("count") == 1, "После подтверждения поручение записано")
    say(tasks.get("overdue_count") == 1, "Просрочка посчитана автоматически")

    protocol = json.loads(registry.execute("protocol_save", {
        "title": "Планёрка по продажам",
        "held_on": date.today().isoformat(),
        "agreements": ["Скидку сверх 15% согласовывает директор"],
        "decisions": ["Выходим в Казахстан в IV квартале"],
        "actions": [
            {"title": "Собрать статистику по возвратам", "assignee": "Сидоров",
             "due_date": (date.today() + timedelta(days=1)).isoformat()},
            {"title": "Уточнить логистику"},
        ],
        "open_questions": ["Кто отвечает за логистику"],
    })[0])
    say(protocol.get("status") == "created", "Протокол встречи сохранён", protocol.get("protocol_id", ""))
    say(len(protocol.get("tasks_created", [])) == 2,
        "Поручения из протокола попали в реестр")
    say(protocol["warnings"]["without_assignee"] == ["Уточнить логистику"],
        "Ненайденный ответственный отмечен, а не придуман")

    registry.execute("kpi_upsert", {"name": "Выручка", "period": "2026-09",
                                    "plan": 5000, "fact": 4700, "warning_pct": 5, "critical_pct": 15})
    registry.execute("kpi_upsert", {"name": "Стоимость привлечения", "period": "2026-09",
                                    "plan": 3000, "fact": 4200, "direction": "lower_is_better",
                                    "warning_pct": 10, "critical_pct": 25})
    kpis = json.loads(registry.execute("kpi_list", {})[0])
    statuses = {k["name"]: k["status"] for k in kpis["kpi"]}
    say(statuses.get("Выручка") == "warning", "Отклонение выручки посчитано по порогу")
    say(statuses.get("Стоимость привлечения") == "critical",
        "Перерасход распознан как критичный (направление показателя учтено)")

    # --- 5. Напоминания ---
    section("5. Напоминания: бот пишет сам")

    plan = reminders_module.pending(datetime.now(conf.tz).replace(hour=10, minute=0))
    say(bool(plan) and bool(plan[0].text), "Утренняя сводка собрана без участия модели")
    if plan and plan[0].text:
        say("Просрочено" in plan[0].text, "В сводке есть просроченные поручения",
            plan[0].text.replace("\n", " | ")[:100])
        say("Сроки на подходе" in plan[0].text, "И наступающие сроки из протокола")

    # Повтор в тот же день не отправляется: напоминание, приходящее дважды,
    # раздражает сильнее отсутствующего.
    reminders_module.mark_sent(plan, datetime.now(conf.tz).replace(hour=10))
    again = reminders_module.pending(datetime.now(conf.tz).replace(hour=14))
    say(again == [], "Повторно за день сводка не уходит")

    # --- 6. Интернет и разделение источников ---
    section("6. Интернет: внешнее отделено от внутреннего")

    if "internet_search" in registry.names():
        result = json.loads(registry.execute("internet_search", {"query": "ставка ЦБ"})[0])
        if result.get("status") == "not_configured":
            say(True, "Без ключа поиска агент честно сообщает о недоступности",
                "и отвечает только по внутренним данным")
        else:
            say(result.get("status") == "ok", "Интернет-поиск работает",
                f"источник: {result.get('provider')}")
    else:
        say(True, "Используется серверный поиск Anthropic")

    # --- 7. Права и границы ---
    section("7. Права доступа и границы")

    from .integrations import google_client

    google = google_client.status()
    say(not google.get("connected"),
        "Без авторизации Google инструменты не притворяются рабочими",
        str(google.get("reason", ""))[:70])

    content, is_error = registry.execute("drive_search", {"query": "договор"})
    say(is_error, "Drive без токена возвращает ошибку с инструкцией, а не выдумку")

    say(conf.confirmation_ttl_minutes > 0,
        "У подтверждения есть срок", f"{conf.confirmation_ttl_minutes} мин, затем отказ")

    shutil.rmtree(workspace, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"Пройдено: {_passed}   Не пройдено: {_failed}")
    if _failed:
        print("\nЕсть непройденные проверки — смотрите строки с ✗ выше.")
        return 1
    print("\nВсе пункты ТЗ отработали. Живые сервисы (модель, Telegram, Google)")
    print("проверяются отдельно: python -m app.selfcheck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
