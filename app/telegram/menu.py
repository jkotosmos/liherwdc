"""Меню бота: категории → действия, инлайн-кнопками.

Действие бывает трёх видов:

* ``ask`` — готовый запрос уходит ассистенту, как если бы его написали;
* ``hint`` — действию нужны подробности: бот подсказывает, что написать;
* ``cmd`` — служебная команда (/check, /auth, /app, /new, /status).

Подтверждение изменений меню не обходит: запрос «создай встречу» всё равно
закончится карточкой «Подтвердить / Правки / Отмена».
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    kind: str  # ask | hint | cmd
    payload: str


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    actions: tuple[Action, ...]


CATEGORIES: tuple[Category, ...] = (
    Category("kb", "📚 База знаний", (
        Action("kb_all", "Что есть в базе", "ask",
               "Что есть в базе знаний? Покажи состав по разделам."),
        Action("kb_products", "Продукты и тарифы", "ask",
               "Расскажи о продуктах OPERON и действующих тарифах — со ссылками на источники."),
        Action("kb_partners", "Партнёры и договоры", "ask",
               "Какие у нас партнёры и договоры? Кратко: кто, ключевые условия, сроки."),
        Action("kb_find", "Найти в документах…", "hint",
               "Напишите, что найти в базе знаний и на Google Диске. Например: "
               "«условия договора с Ромашкой» или «тариф Бизнес — цена и лимиты»."),
    )),
    Category("cal", "📅 Календарь", (
        Action("cal_today", "Встречи сегодня", "ask",
               "Какие у меня встречи и дедлайны сегодня?"),
        Action("cal_week", "На неделю", "ask",
               "Покажи мои встречи и дедлайны на ближайшую неделю."),
        Action("cal_new", "Создать встречу…", "hint",
               "Напишите, что запланировать. Например: «встреча с Ивановым завтра в 15:00 "
               "на час, тема — договор». Перед созданием покажу карточку для подтверждения."),
    )),
    Category("tasks", "✅ Поручения", (
        Action("t_overdue", "Просроченные", "ask",
               "Какие поручения просрочены и за кем они закреплены?"),
        Action("t_open", "Все открытые", "ask",
               "Покажи все открытые поручения со сроками и ответственными."),
        Action("t_next", "План действий", "ask",
               "Составь список следующих действий по открытым задачам с приоритетами."),
        Action("t_new", "Добавить поручение…", "hint",
               "Напишите поручение: что сделать, кто отвечает и срок. Например: "
               "«Петров — акт сверки с Ромашкой до пятницы». Запишу после подтверждения."),
    )),
    Category("docs", "📄 Документы и отчёты", (
        Action("d_drive", "Свежее на Диске", "ask",
               "Какие документы на Google Диске менялись за последнюю неделю?"),
        Action("d_kpi", "Показатели (KPI)", "ask",
               "Покажи ключевые показатели и отклонения от плана — со ссылками на источники."),
        Action("d_protocol", "Протокол встречи…", "hint",
               "Пришлите заметки или расшифровку встречи — оформлю протокол: решения, "
               "ответственные, сроки и список следующих действий."),
        Action("d_offer", "Коммерческое предложение…", "hint",
               "Напишите, для кого КП и что предлагаем (продукт, объём, сроки). "
               "Возьму тарифы из базы знаний и пришлю проект файлом Word."),
        Action("d_calc", "Расчёт в Excel…", "hint",
               "Напишите, что посчитать: например, «стоимость 15 лицензий по тарифу Бизнес "
               "со скидкой 10% и НДС». Посчитаю точно и пришлю таблицу Excel."),
    )),
    Category("web", "🌐 Интернет", (
        Action("w_market", "Новости рынка", "ask",
               "Найди свежие новости рынка и конкурентов OPERON за последнюю неделю. "
               "Отдели внешние данные от внутренних, укажи ссылки и даты."),
        Action("w_law", "Законодательство", "ask",
               "Какие изменения законодательства за последний месяц могут касаться "
               "нашего бизнеса? Укажи ссылки и даты."),
        Action("w_find", "Найти в интернете…", "hint",
               "Напишите, что найти в интернете. Например: «средняя цена подписки на "
               "CRM в России в 2026 году». Отвечу со ссылками и датами."),
    )),
    Category("svc", "⚙️ Сервис", (
        Action("s_check", "Проверка подключений", "cmd", "check"),
        Action("s_auth", "Подключить Google", "cmd", "auth"),
        Action("s_app", "Модель и баланс", "cmd", "app"),
        Action("s_status", "Статус", "cmd", "status"),
        Action("s_new", "Новый диалог", "cmd", "new"),
    )),
)

ROOT_TEXT = "Что сделать? Выберите раздел — или просто напишите вопрос."

_BY_CATEGORY = {c.id: c for c in CATEGORIES}
_BY_ACTION = {a.id: a for c in CATEGORIES for a in c.actions}


def root_keyboard() -> dict:
    buttons = [{"text": c.label, "callback_data": f"m:c:{c.id}"} for c in CATEGORIES]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def category_keyboard(category_id: str) -> dict | None:
    category = _BY_CATEGORY.get(category_id)
    if category is None:
        return None
    rows = [[{"text": a.label, "callback_data": f"m:a:{a.id}"}] for a in category.actions]
    rows.append([{"text": "⬅️ Назад", "callback_data": "m:root"}])
    return {"inline_keyboard": rows}


def category(category_id: str) -> Category | None:
    return _BY_CATEGORY.get(category_id)


def action(action_id: str) -> Action | None:
    return _BY_ACTION.get(action_id)


BOT_COMMANDS = [
    {"command": "menu", "description": "Меню по разделам"},
    {"command": "check", "description": "Проверить подключения"},
    {"command": "auth", "description": "Подключить Google"},
    {"command": "app", "description": "Модель и баланс"},
    {"command": "new", "description": "Новый диалог"},
    {"command": "status", "description": "Что подключено"},
    {"command": "help", "description": "Подсказка"},
]
