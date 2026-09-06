"""Прямые зависимости должны быть объявлены прямо.

Первая же установка на чистой машине упала на `ModuleNotFoundError: httpx`.
Модуль был нашей прямой зависимостью — на нём построены шлюз к модели,
клиент Telegram и интернет-поиск, — но в requirements.txt не значился:
раньше он приезжал попутно с anthropic. Та сменила мажорную версию, перешла
на httpx2, и попутчик исчез.

Этот тест сверяет то, что код импортирует, с тем, что проект просит
установить. Он не про стиль: незаявленная зависимость ломает установку
не у того, кто её писал, а у того, кто разворачивает.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Соответствие «имя пакета в requirements» → «имя модуля при импорте».
DISTRIBUTION_TO_MODULE = {
    "python-dotenv": "dotenv",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "google-api-python-client": "googleapiclient",
    "google-auth": "google",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "uvicorn[standard]": "uvicorn",
}


def declared_modules() -> set[str]:
    modules = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~;]", line)[0].strip()
        modules.add(DISTRIBUTION_TO_MODULE.get(name, name.replace("-", "_")))
    return modules


def imported_modules() -> set[str]:
    """Верхнеуровневые модули, которые импортирует наш код."""
    pattern = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", re.MULTILINE)
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1))
    return found


def test_every_imported_package_is_declared() -> None:
    external = imported_modules() - set(sys.stdlib_module_names) - {"app"}
    missing = external - declared_modules()

    assert not missing, (
        f"Код импортирует {sorted(missing)}, но requirements.txt их не просит. "
        "На чистой машине установка сломается."
    )


def test_httpx_is_declared_explicitly() -> None:
    """Именно на этом упала первая установка у заказчика."""
    assert "httpx" in declared_modules()


def test_requirements_are_not_pinned_to_a_dead_end() -> None:
    """Верхних границ нет намеренно: они устаревают тихо и ломают установку позже."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert "<" not in line, f"верхняя граница версии в «{line}» состарится незаметно"
