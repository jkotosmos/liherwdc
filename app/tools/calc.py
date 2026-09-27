"""Точные расчёты: суммы, проценты, отклонения, итоги КП.

Языковая модель считает «на глаз» и ошибается в арифметике уверенно — для
расчётов, сводок и коммерческих предложений это недопустимо. Инструмент
вычисляет выражения сам. Шаги можно называть и ссылаться на них дальше:
«сумма = цена * количество», затем «итого = сумма * 1.2».

Выражения разбираются через ast: разрешены только числа, имена шагов,
арифметика и короткий список функций. Никакого eval.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

from .base import ToolError, ToolSpec, registry

MAX_EXPRESSION = 500
MAX_NODES = 300
MAX_POWER = 64

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _avg(*values: float) -> float:
    flat = _flatten(values)
    if not flat:
        raise ToolError("avg: нет значений.")
    return sum(flat) / len(flat)


def _flatten(values: tuple) -> list[float]:
    flat: list[float] = []
    for value in values:
        flat.extend(value if isinstance(value, (list, tuple)) else [value])
    return flat


def _pct(part: float, whole: float) -> float:
    """Доля в процентах: pct(25, 200) = 12.5."""
    if whole == 0:
        raise ToolError("pct: делить на ноль нельзя (целое равно 0).")
    return part / whole * 100


def _change(new: float, old: float) -> float:
    """Изменение в процентах: change(110, 100) = 10."""
    if old == 0:
        raise ToolError("change: базовое значение равно 0 — процент изменения не определён.")
    return (new - old) / abs(old) * 100


FUNCTIONS: dict[str, Any] = {
    "round": round,
    "abs": abs,
    "min": lambda *v: min(_flatten(v)),
    "max": lambda *v: max(_flatten(v)),
    "sum": lambda *v: sum(_flatten(v)),
    "avg": _avg,
    "sqrt": math.sqrt,
    "pct": _pct,
    "change": _change,
}


def _number(text: str) -> float:
    """«1 250 000,50» и «1250000.50» — одно и то же число."""
    cleaned = text.replace(" ", "").replace(" ", "").replace(",", ".")
    return float(cleaned)


def _normalize(expression: str) -> str:
    # Пробелы-разделители тысяч и десятичная запятая внутри чисел: 1 250,5 → 1250.5
    expression = re.sub(r"(?<=\d)[  ](?=\d{3}\b)", "", expression)
    # Запятая между цифрами — десятичная только вне скобок. Внутри скобок это
    # разделитель аргументов: pct(25,200) — два числа, а не 25.2.
    out: list[str] = []
    depth = 0
    for index, char in enumerate(expression):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif (
            char == ","
            and depth == 0
            and 0 < index < len(expression) - 1
            and expression[index - 1].isdigit()
            and expression[index + 1].isdigit()
        ):
            char = "."
        out.append(char)
    return "".join(out).replace("×", "*").replace("÷", "/").replace("−", "-")


def evaluate(expression: str, names: dict[str, float] | None = None) -> float:
    names = names or {}
    source = _normalize((expression or "").strip())
    if not source:
        raise ToolError("Пустое выражение.")
    if len(source) > MAX_EXPRESSION:
        raise ToolError(f"Выражение длиннее {MAX_EXPRESSION} символов — разбейте на шаги.")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Не разобрал выражение «{expression}»: {exc.msg}.") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ToolError("Выражение слишком сложное — разбейте на шаги.")

    def walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            raise ToolError(f"Неизвестное имя «{node.id}». Сначала посчитайте его отдельным шагом.")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_POWER:
                raise ToolError(f"Степень больше {MAX_POWER} не считаю.")
            try:
                return _BINARY[type(node.op)](left, right)
            except ZeroDivisionError as exc:
                raise ToolError(f"Деление на ноль в «{expression}».") from exc
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](walk(node.operand))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [walk(item) for item in node.elts]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS:
            if node.keywords:
                raise ToolError("Именованные аргументы не поддерживаются.")
            return FUNCTIONS[node.func.id](*[walk(arg) for arg in node.args])
        raise ToolError(
            f"В выражении «{expression}» недопустимая конструкция. Можно: числа, + - * / // % **, "
            f"скобки, имена прежних шагов и функции {', '.join(sorted(FUNCTIONS))}."
        )

    result = walk(tree)
    if isinstance(result, list):
        raise ToolError("Результат — список, а нужно число. Оберните его в sum(), avg(), min() или max().")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise ToolError(f"Результат «{expression}» не является конечным числом.")
    return result


def _format(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    rounded = round(value, 6)
    integer, _, fraction = f"{rounded:.6f}".rstrip("0").partition(".")
    grouped = f"{int(integer):,}".replace(",", " ") if integer not in {"-0", ""} else integer
    if integer.startswith("-") and int(integer) == 0:
        grouped = "-0"
    return f"{grouped},{fraction}" if fraction else grouped


def _calculate(tool_input: dict[str, Any]) -> Any:
    steps = tool_input.get("steps")
    if not steps and tool_input.get("expression"):
        steps = [{"name": "result", "expression": tool_input["expression"]}]
    if not steps:
        raise ToolError("Передайте expression или список steps [{name, expression}].")
    if len(steps) > 50:
        raise ToolError("Не больше 50 шагов за раз.")

    names: dict[str, float] = {}
    for key, value in (tool_input.get("variables") or {}).items():
        try:
            names[str(key)] = value if isinstance(value, (int, float)) else _number(str(value))
        except ValueError as exc:
            raise ToolError(f"Значение «{key}» — не число: {value}") from exc

    results = []
    for index, step in enumerate(steps, 1):
        name = str(step.get("name") or f"step{index}")
        if not name.isidentifier():
            raise ToolError(f"Имя шага «{name}» должно быть одним словом латиницей (например, total).")
        value = evaluate(str(step.get("expression", "")), names)
        names[name] = value
        results.append({
            "name": name,
            "label": step.get("label", ""),
            "expression": step.get("expression", ""),
            "value": value,
            "formatted": _format(value),
        })

    return {
        "status": "ok",
        "source_type": "calculation",
        "note": "Посчитано инструментом. Приводи эти значения как есть и укажи формулу.",
        "results": results,
    }


def register_calc_tools() -> None:
    registry.register(
        ToolSpec(
            name="calculate",
            description=(
                "Точно вычисляет выражения: суммы, проценты, отклонения от плана, итоги и скидки "
                "в КП, расчёты по таблицам. Используй для ЛЮБЫХ вычислений вместо счёта в уме. "
                "Шаги можно называть и использовать дальше: steps=[{name:'sum', expression:'1200*15'}, "
                "{name:'total', expression:'sum*1.2'}]. Функции: round, abs, min, max, sum, avg, sqrt, "
                "pct(часть, целое) — доля в %, change(новое, старое) — изменение в %. "
                "Внутри функций десятичный разделитель — точка."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Одно выражение, например 1250*0.85."},
                    "steps": {
                        "type": "array",
                        "description": "Последовательные шаги расчёта.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Имя шага латиницей."},
                                "expression": {"type": "string"},
                                "label": {"type": "string", "description": "Подпись для человека."},
                            },
                            "required": ["expression"],
                        },
                    },
                    "variables": {
                        "type": "object",
                        "description": "Исходные числа по именам, например {\"price\": 1200, \"qty\": 15}.",
                    },
                },
            },
            handler=_calculate,
            activity="Считаю",
        )
    )


register_calc_tools()
