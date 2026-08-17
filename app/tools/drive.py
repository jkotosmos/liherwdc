"""Инструменты Google Drive: поиск, чтение, сравнение версий, создание документов."""

from __future__ import annotations

import difflib
import io
import re
from typing import Any

from ..integrations.google_client import (
    HttpError,
    authorized_session,
    describe_http_error,
    get_service,
)
from .base import IntegrationUnavailable, Preview, ToolError, ToolSpec, registry

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"

EXPORT_AS_TEXT = {
    GOOGLE_DOC: "text/plain",
    GOOGLE_SLIDES: "text/plain",
    GOOGLE_SHEET: "text/csv",
}

READABLE_BINARY_PREFIXES = ("text/", "application/json", "application/xml")
MAX_READ_CHARS = 60_000

FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,size,webViewLink,owners(displayName,emailAddress),"
    "lastModifyingUser(displayName),parents,description,trashed"
)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drive():
    return get_service("drive", "v3")


def _humanize(file: dict[str, Any]) -> dict[str, Any]:
    owners = ", ".join(o.get("displayName") or o.get("emailAddress", "") for o in file.get("owners", []))
    return {
        "file_id": file.get("id"),
        "name": file.get("name"),
        "mime_type": file.get("mimeType"),
        "modified_time": file.get("modifiedTime"),
        "created_time": file.get("createdTime"),
        "size_bytes": file.get("size"),
        "owners": owners,
        "last_modified_by": (file.get("lastModifyingUser") or {}).get("displayName", ""),
        "link": file.get("webViewLink"),
        "description": file.get("description", ""),
    }


# --- поиск -----------------------------------------------------------------


def _drive_search(tool_input: dict[str, Any]) -> Any:
    query = (tool_input.get("query") or "").strip()
    limit = min(max(int(tool_input.get("limit") or 10), 1), 50)

    clauses = ["trashed = false"]
    if query:
        clauses.append(f"(name contains '{_escape(query)}' or fullText contains '{_escape(query)}')")
    mime_type = (tool_input.get("mime_type") or "").strip()
    if mime_type:
        clauses.append(f"mimeType = '{_escape(mime_type)}'")
    modified_after = (tool_input.get("modified_after") or "").strip()
    if modified_after:
        stamp = modified_after if "T" in modified_after else f"{modified_after}T00:00:00"
        clauses.append(f"modifiedTime > '{_escape(stamp)}'")
    folder_id = (tool_input.get("folder_id") or "").strip()
    if folder_id:
        clauses.append(f"'{_escape(folder_id)}' in parents")

    try:
        response = (
            _drive()
            .files()
            .list(
                q=" and ".join(clauses),
                pageSize=limit,
                orderBy="modifiedTime desc",
                fields=f"files({FILE_FIELDS})",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Поиск в Google Drive")) from exc

    files = [_humanize(f) for f in response.get("files", [])]
    if not files:
        return {
            "status": "not_found",
            "query": query,
            "hint": (
                "В Google Drive пользователя ничего не найдено по этому запросу. Не выдумывай "
                "содержимое файлов — сообщи, что документ не найден, и предложи уточнить название."
            ),
        }
    return {"status": "ok", "found": len(files), "files": files}


# --- чтение ----------------------------------------------------------------


def _read_spreadsheet(file_id: str, sheet_range: str | None) -> str:
    """Таблицы читаем через Sheets API — так доступны все листы, а не только первый."""
    sheets = get_service("sheets", "v4")
    meta = sheets.spreadsheets().get(spreadsheetId=file_id, fields="sheets(properties(title))").execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]

    ranges = [sheet_range] if sheet_range else titles
    blocks: list[str] = []
    for target in ranges:
        try:
            values = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=file_id, range=target, valueRenderOption="FORMATTED_VALUE")
                .execute()
                .get("values", [])
            )
        except HttpError as exc:
            raise ToolError(describe_http_error(exc, f"Чтение диапазона «{target}»")) from exc
        rendered = "\n".join(" | ".join(str(cell) for cell in row) for row in values)
        blocks.append(f"### Лист: {target}\n{rendered or '(пусто)'}")
    return "\n\n".join(blocks)


def _export_text(file_id: str, mime_type: str) -> str:
    drive = _drive()
    if mime_type in EXPORT_AS_TEXT:
        data = drive.files().export(fileId=file_id, mimeType=EXPORT_AS_TEXT[mime_type]).execute()
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    if mime_type.startswith(READABLE_BINARY_PREFIXES):
        data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    raise ToolError(
        f"Файл имеет тип {mime_type}, текст из него извлечь нельзя (например, PDF, изображение "
        "или архив). Сообщи пользователю тип файла и предложи прислать текстовую версию или "
        "экспортировать документ в Google Docs."
    )


def _drive_read(tool_input: dict[str, Any]) -> Any:
    file_id = (tool_input.get("file_id") or "").strip()
    if not file_id:
        raise ToolError("Не указан file_id. Сначала найдите файл через drive_search.")

    try:
        meta = (
            _drive()
            .files()
            .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Чтение файла Google Drive")) from exc

    mime_type = meta.get("mimeType", "")
    if mime_type == GOOGLE_SHEET:
        text = _read_spreadsheet(file_id, tool_input.get("sheet_range"))
    else:
        try:
            text = _export_text(file_id, mime_type)
        except HttpError as exc:
            raise ToolError(describe_http_error(exc, "Экспорт файла Google Drive")) from exc

    truncated = len(text) > MAX_READ_CHARS
    return {
        "status": "ok",
        **_humanize(meta),
        "citation": f"Google Drive → «{meta.get('name')}» ({meta.get('webViewLink')}), версия от {meta.get('modifiedTime')}",
        "truncated": truncated,
        "content": text[:MAX_READ_CHARS],
    }


# --- версии ----------------------------------------------------------------


def _drive_list_revisions(tool_input: dict[str, Any]) -> Any:
    file_id = (tool_input.get("file_id") or "").strip()
    if not file_id:
        raise ToolError("Не указан file_id.")
    try:
        response = (
            _drive()
            .revisions()
            .list(
                fileId=file_id,
                pageSize=min(max(int(tool_input.get("limit") or 20), 1), 100),
                fields="revisions(id,modifiedTime,lastModifyingUser(displayName),size,keepForever)",
            )
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Список версий файла")) from exc

    revisions = [
        {
            "revision_id": r.get("id"),
            "modified_time": r.get("modifiedTime"),
            "modified_by": (r.get("lastModifyingUser") or {}).get("displayName", ""),
            "size_bytes": r.get("size"),
        }
        for r in response.get("revisions", [])
    ]
    if not revisions:
        return {
            "status": "not_found",
            "hint": "У файла нет доступной истории версий (её может не быть для файлов не из редакторов Google).",
        }
    return {"status": "ok", "revisions_count": len(revisions), "revisions": revisions}


def _revision_text(file_id: str, revision_id: str, mime_type: str) -> str:
    drive = _drive()
    if mime_type in EXPORT_AS_TEXT:
        revision = (
            drive.revisions()
            .get(fileId=file_id, revisionId=revision_id, fields="exportLinks")
            .execute()
        )
        links = revision.get("exportLinks", {})
        url = links.get(EXPORT_AS_TEXT[mime_type]) or links.get("text/plain")
        if not url:
            raise ToolError(
                f"Для версии {revision_id} Google не отдаёт текстовый экспорт "
                f"(доступные форматы: {sorted(links)})."
            )
        response = authorized_session().get(url)
        if response.status_code != 200:
            raise ToolError(
                f"Не удалось скачать версию {revision_id}: HTTP {response.status_code}."
            )
        return response.text
    data = drive.revisions().get_media(fileId=file_id, revisionId=revision_id).execute()
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


def _drive_compare_revisions(tool_input: dict[str, Any]) -> Any:
    file_id = (tool_input.get("file_id") or "").strip()
    rev_a = (tool_input.get("revision_id_a") or "").strip()
    rev_b = (tool_input.get("revision_id_b") or "").strip()
    if not (file_id and rev_a and rev_b):
        raise ToolError(
            "Нужны file_id, revision_id_a и revision_id_b. Список версий даёт drive_list_revisions."
        )

    try:
        meta = _drive().files().get(fileId=file_id, fields="name,mimeType,webViewLink").execute()
        text_a = _revision_text(file_id, rev_a, meta.get("mimeType", ""))
        text_b = _revision_text(file_id, rev_b, meta.get("mimeType", ""))
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Сравнение версий файла")) from exc

    diff = list(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=f"версия {rev_a}",
            tofile=f"версия {rev_b}",
            lineterm="",
            n=2,
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    return {
        "status": "ok",
        "file_name": meta.get("name"),
        "link": meta.get("webViewLink"),
        "lines_added": added,
        "lines_removed": removed,
        "identical": not diff,
        "diff_truncated": len(diff) > 400,
        "diff": "\n".join(diff[:400]) or "Различий нет.",
    }


# --- создание (с подтверждением) -------------------------------------------

CREATE_MIME_BY_FORMAT = {
    "document": (GOOGLE_DOC, "text/plain"),
    "spreadsheet": (GOOGLE_SHEET, "text/csv"),
    "markdown": ("text/markdown", "text/markdown"),
    "text": ("text/plain", "text/plain"),
}


def _drive_create_file(tool_input: dict[str, Any]) -> Any:
    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise IntegrationUnavailable(
            "Библиотеки Google API не установлены. Выполните: pip install -r requirements.txt"
        ) from exc

    name = (tool_input.get("name") or "").strip()
    content = tool_input.get("content") or ""
    fmt = (tool_input.get("format") or "document").strip().lower()
    if not name:
        raise ToolError("Не указано имя файла (name).")
    if not content.strip():
        raise ToolError("Пустое содержимое (content).")
    if fmt not in CREATE_MIME_BY_FORMAT:
        raise ToolError(f"Неизвестный формат «{fmt}». Допустимо: {sorted(CREATE_MIME_BY_FORMAT)}")

    target_mime, upload_mime = CREATE_MIME_BY_FORMAT[fmt]
    body: dict[str, Any] = {"name": name, "mimeType": target_mime}
    folder_id = (tool_input.get("folder_id") or "").strip()
    if folder_id:
        body["parents"] = [folder_id]

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype=upload_mime, resumable=False
    )
    try:
        created = (
            _drive()
            .files()
            .create(body=body, media_body=media, fields="id,name,webViewLink,mimeType", supportsAllDrives=True)
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Создание файла в Google Drive")) from exc

    return {
        "status": "created",
        "file_id": created.get("id"),
        "name": created.get("name"),
        "mime_type": created.get("mimeType"),
        "link": created.get("webViewLink"),
    }


def _preview_create(tool_input: dict[str, Any]) -> Preview:
    content = tool_input.get("content") or ""
    excerpt = re.sub(r"\n{3,}", "\n\n", content.strip())[:1200]
    return Preview(
        title="Создать файл в Google Drive",
        summary=f"Будет создан файл «{tool_input.get('name', 'без имени')}» "
        f"({tool_input.get('format', 'document')}) на вашем Google Диске.",
        details={
            "Имя файла": tool_input.get("name", ""),
            "Формат": tool_input.get("format", "document"),
            "Папка": tool_input.get("folder_id") or "Мой диск (корень)",
            "Объём": f"{len(content)} символов",
            "Содержимое (начало)": excerpt + ("…" if len(content) > 1200 else ""),
        },
    )


# --- регистрация -----------------------------------------------------------

registry.register(
    ToolSpec(
        name="drive_search",
        description=(
            "Ищет файлы в Google Drive пользователя по названию и содержимому. Возвращает "
            "идентификаторы, тип, владельца, дату изменения и ссылку. Используй, чтобы найти "
            "документы, таблицы и презентации перед чтением. Доступны только файлы, к которым "
            "у пользователя уже есть доступ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова из названия или текста файла."},
                "mime_type": {
                    "type": "string",
                    "description": (
                        "Фильтр по типу: application/vnd.google-apps.document (Документы), "
                        "application/vnd.google-apps.spreadsheet (Таблицы), "
                        "application/vnd.google-apps.presentation (Презентации), "
                        "application/vnd.google-apps.folder (Папки)."
                    ),
                },
                "modified_after": {
                    "type": "string",
                    "description": "Изменён после указанной даты, формат YYYY-MM-DD.",
                },
                "folder_id": {"type": "string", "description": "Искать только внутри этой папки."},
                "limit": {"type": "integer", "description": "Сколько файлов вернуть (1–50), по умолчанию 10."},
            },
            "required": ["query"],
        },
        handler=_drive_search,
        activity="Ищу файлы в Google Drive",
    )
)

registry.register(
    ToolSpec(
        name="drive_read",
        description=(
            "Читает содержимое файла Google Drive: Документы и Презентации — как текст, Таблицы — "
            "полистно со всеми значениями, текстовые файлы — как есть. Для таблиц можно указать "
            "диапазон. Всегда ссылайся на прочитанный файл (название, ссылка, дата изменения)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Идентификатор файла из drive_search."},
                "sheet_range": {
                    "type": "string",
                    "description": "Только для Таблиц: диапазон вида «Лист1!A1:F100». Без него читаются все листы.",
                },
            },
            "required": ["file_id"],
        },
        handler=_drive_read,
        activity="Читаю файл из Google Drive",
    )
)

registry.register(
    ToolSpec(
        name="drive_list_revisions",
        description=(
            "Показывает историю версий файла Google Drive: кто и когда менял. Нужен, чтобы затем "
            "сравнить две версии через drive_compare_revisions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Идентификатор файла."},
                "limit": {"type": "integer", "description": "Сколько версий вернуть (1–100), по умолчанию 20."},
            },
            "required": ["file_id"],
        },
        handler=_drive_list_revisions,
        activity="Смотрю историю версий файла",
    )
)

registry.register(
    ToolSpec(
        name="drive_compare_revisions",
        description=(
            "Сравнивает две версии одного файла Google Drive и возвращает построчные различия "
            "(что добавлено, что удалено). Используй для вопросов «что изменилось в договоре / "
            "коммерческом предложении между редакциями»."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Идентификатор файла."},
                "revision_id_a": {"type": "string", "description": "Более ранняя версия."},
                "revision_id_b": {"type": "string", "description": "Более поздняя версия."},
            },
            "required": ["file_id", "revision_id_a", "revision_id_b"],
        },
        handler=_drive_compare_revisions,
        activity="Сравниваю версии документа",
    )
)

registry.register(
    ToolSpec(
        name="drive_create_file",
        description=(
            "Создаёт новый файл на Google Диске пользователя: документ, таблицу или текстовый файл. "
            "Используй для готовых коммерческих предложений, расчётов, протоколов встреч и проектов "
            "документов. ВАЖНО: файл создаётся только после явного подтверждения пользователя — "
            "сначала покажи содержимое в чате и убедись, что оно согласовано."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Название будущего файла."},
                "content": {
                    "type": "string",
                    "description": "Полное содержимое. Для формата spreadsheet передавай CSV с заголовком.",
                },
                "format": {
                    "type": "string",
                    "enum": ["document", "spreadsheet", "markdown", "text"],
                    "description": "document — Google Документ, spreadsheet — Google Таблица из CSV, markdown/text — файл.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Необязательный идентификатор папки назначения.",
                },
            },
            "required": ["name", "content", "format"],
        },
        handler=_drive_create_file,
        requires_confirmation=True,
        preview=_preview_create,
        activity="Создаю файл в Google Drive",
    )
)
