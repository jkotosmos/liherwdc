"""Первый запуск Google: невключённые API, таблицы без Sheets API, выданные права."""

from __future__ import annotations

import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import selfcheck
from app.errors import ToolError
from app.integrations import google_client
from app.tools import drive

DISABLED = (
    "Google Sheets API has not been used in project 111111111111 before or it is "
    "disabled. Enable it by visiting https://console.developers.google.com/apis/api/"
    "sheets.googleapis.com/overview?project=111111111111 then retry."
)


def http_error(status: int, message: str) -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.reason = "error"
    return HttpError(resp, json.dumps({"error": {"message": message}}).encode())


class TestDisabledApi:
    def test_names_api_and_link_instead_of_rights(self) -> None:
        text = google_client.describe_http_error(http_error(403, DISABLED), "Чтение")
        assert "не включён Google Sheets API" in text
        assert "https://console.developers.google.com/apis/api/sheets.googleapis.com" in text
        assert "Повторная авторизация не нужна" in text

    def test_ordinary_403_still_about_rights(self) -> None:
        text = google_client.describe_http_error(http_error(403, "The caller does not have permission"), "Чтение")
        assert "доступ запрещён" in text


class _Call:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class TestSpreadsheetFallback:
    def test_disabled_sheets_api_falls_back_to_xlsx_export(self, monkeypatch) -> None:
        class Sheets:
            def spreadsheets(self):
                return self

            def get(self, **kwargs):
                return _Call(error=http_error(403, DISABLED))

        class Files:
            def export(self, fileId, mimeType):
                assert mimeType == drive.XLSX_MIME
                return _Call(result=b"xlsx-bytes")

        class Drive:
            def files(self):
                return Files()

        monkeypatch.setattr(drive, "get_service", lambda api, version: Sheets())
        monkeypatch.setattr(drive, "_drive", lambda: Drive())
        monkeypatch.setattr(drive, "extract_text", lambda data, name: f"все листы из {name}")

        assert drive._read_spreadsheet("id", None) == "все листы из file.xlsx"

    def test_other_errors_are_not_masked(self, monkeypatch) -> None:
        class Sheets:
            def spreadsheets(self):
                return self

            def get(self, **kwargs):
                return _Call(error=http_error(404, "not found"))

        monkeypatch.setattr(drive, "get_service", lambda api, version: Sheets())
        with pytest.raises(HttpError):
            drive._read_spreadsheet("id", None)


class TestGrantedScopes:
    def _run(self, monkeypatch, granted: set[str]):
        monkeypatch.setattr(selfcheck, "_granted_scopes", lambda: granted)

        class Registry:
            def execute(self, name, payload):
                return "{}", False

        import app.tools as tools

        monkeypatch.setattr(tools, "registry", Registry())
        monkeypatch.setattr(google_client, "get_service", lambda *a: (_ for _ in ()).throw(RuntimeError("нет сети")))
        return {c.name: c for c in selfcheck._google_live_checks()}

    def test_unticked_scope_is_a_failure(self, monkeypatch) -> None:
        checks = self._run(monkeypatch, {"https://www.googleapis.com/auth/drive.readonly"})
        assert checks["Google: разрешения"].status == selfcheck.FAIL
        assert "календарь" in checks["Google: разрешения"].detail

    def test_all_scopes_granted(self, monkeypatch) -> None:
        from app.config import settings

        checks = self._run(monkeypatch, set(settings.google_scopes))
        assert checks["Google: разрешения"].status == selfcheck.OK
        assert checks["Google Drive: чтение"].status == selfcheck.OK
