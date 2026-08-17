"""Токенизация и стемминг для поиска по русско-английской базе знаний.

Реализован упрощённый алгоритм Snowball для русского языка — он снимает
падежные и глагольные окончания, поэтому запрос «тарифы партнёров» находит
документ со словами «тариф партнёра». Внешних зависимостей нет намеренно:
база знаний должна индексироваться без ML-стека.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")

VOWELS = set("аеиоуыэюяё")

# Стоп-слова: служебные части речи, которые только зашумляют ранжирование.
STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "with", "as", "by", "it", "this", "that", "at", "from",
}

_PERFECTIVE_GERUND_1 = ("вшись", "вшийся", "вши", "в")
_PERFECTIVE_GERUND_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")
_ADJECTIVE = (
    "ими", "ыми", "его", "ого", "ому", "ему",
    "ее", "ие", "ые", "ое", "ей", "ий", "ый", "ой", "ем", "им", "ым", "ом",
    "их", "ых", "ую", "юю", "ая", "яя", "ою", "ею",
)
_PARTICIPLE_1 = ("ем", "нн", "вш", "ющ", "щ")
_PARTICIPLE_2 = ("ивш", "ывш", "ующ")
_REFLEXIVE = ("ся", "сь")
_VERB_1 = (
    "ла", "на", "ете", "йте", "ли", "й", "л", "ем", "н", "ло", "но", "ет",
    "ют", "ны", "ть", "ешь", "нно",
)
_VERB_2 = (
    "ила", "ыла", "ена", "ейте", "уйте", "ите", "или", "ыли", "ей", "уй",
    "ил", "ыл", "им", "ым", "ен", "ило", "ыло", "ено", "ят", "ует", "уют",
    "ит", "ыт", "ены", "ить", "ыть", "ишь", "ую", "ю",
)
_NOUN = (
    "а", "ев", "ов", "ие", "ье", "е", "иями", "ями", "ами", "еи", "ии", "и",
    "ией", "ей", "ой", "ий", "й", "иям", "ям", "ием", "ем", "ам", "ом", "о",
    "у", "ах", "иях", "ях", "ы", "ь", "ию", "ью", "ю", "ия", "ья", "я",
)
_SUPERLATIVE = ("ейш", "ейше")
_DERIVATIONAL = ("ост", "ость")


def _rv_index(word: str) -> int:
    """RV — область после первой гласной (терминология Snowball)."""
    for i, ch in enumerate(word):
        if ch in VOWELS:
            return i + 1
    return len(word)


def _r2_index(word: str) -> int:
    r1 = len(word)
    for i in range(len(word) - 1):
        if word[i] in VOWELS and word[i + 1] not in VOWELS:
            r1 = i + 2
            break
    r2 = len(word)
    for i in range(r1, len(word) - 1):
        if word[i] in VOWELS and word[i + 1] not in VOWELS:
            r2 = i + 2
            break
    return r2


def _strip(part: str, endings: tuple[str, ...]) -> tuple[str, bool]:
    for ending in sorted(endings, key=len, reverse=True):
        if part.endswith(ending):
            return part[: -len(ending)], True
    return part, False


def stem_ru(word: str) -> str:
    """Возвращает основу русского слова. Короткие слова не трогаем."""
    if len(word) <= 3:
        return word

    rv = _rv_index(word)
    head, tail = word[:rv], word[rv:]

    # Шаг 1: деепричастие -> возвратная частица -> причастие/прилагательное -> глагол -> существительное
    new_tail, done = _strip_group(tail, _PERFECTIVE_GERUND_2, _PERFECTIVE_GERUND_1)
    if done:
        tail = new_tail
    else:
        tail, _ = _strip(tail, _REFLEXIVE)
        adj_tail, adj_done = _strip(tail, _ADJECTIVE)
        if adj_done:
            tail = adj_tail
            part_tail, part_done = _strip_group(tail, _PARTICIPLE_2, _PARTICIPLE_1)
            if part_done:
                tail = part_tail
        else:
            verb_tail, verb_done = _strip_group(tail, _VERB_2, _VERB_1)
            if verb_done:
                tail = verb_tail
            else:
                tail, _ = _strip(tail, _NOUN)

    # Шаг 2: убираем финальную «и»
    if tail.endswith("и"):
        tail = tail[:-1]

    # Шаг 3: словообразовательный суффикс, если он в R2
    stem = head + tail
    r2 = _r2_index(word)
    if len(stem) > r2:
        stripped, changed = _strip(stem[r2:], _DERIVATIONAL)
        if changed:
            stem = stem[:r2] + stripped
            tail = stem[len(head):] if len(stem) >= len(head) else ""

    # Шаг 4: удвоенная «н», превосходная степень, мягкий знак
    if stem.endswith("нн"):
        stem = stem[:-1]
    else:
        sup, changed = _strip(stem, _SUPERLATIVE)
        if changed:
            stem = sup[:-1] if sup.endswith("нн") else sup
    if stem.endswith("ь"):
        stem = stem[:-1]

    return stem or word


def _strip_group(
    part: str, endings_with_prefix: tuple[str, ...], plain_endings: tuple[str, ...]
) -> tuple[str, bool]:
    """Окончания группы 2 требуют предшествующей «а»/«я» — как в Snowball."""
    for ending in sorted(endings_with_prefix, key=len, reverse=True):
        if part.endswith(ending):
            return part[: -len(ending)], True
    for ending in sorted(plain_endings, key=len, reverse=True):
        if part.endswith(ending):
            base = part[: -len(ending)]
            if base.endswith(("а", "я")):
                return base, True
    return part, False


def normalize(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е").lower()


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Разбивает текст на нормализованные основы слов."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalize(text)):
        token = match.group(0)
        if drop_stopwords and token in STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(stem_ru(token) if any(c in "абвгдежзийклмнопрстуфхцчшщъыьэюя" for c in token) else token)
    return tokens
