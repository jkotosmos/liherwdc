"""Вывод не должен падать там, где его читают.

Русская консоль Windows работает в cp866, и «✓» там не портит вид, а бросает
UnicodeEncodeError. Диагностический инструмент, обрывающийся на выводе
диагностики, бесполезен вдвойне.
"""

from __future__ import annotations

import io

from app import console


class FixedEncodingStream(io.TextIOWrapper):
    """Поток без reconfigure — так ведёт себя перенаправленный вывод."""

    reconfigure = None


def cp866_stream() -> FixedEncodingStream:
    return FixedEncodingStream(io.BytesIO(), encoding="cp866", errors="strict")


class TestMarkSelection:
    def test_utf8_console_gets_readable_marks(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        assert console.setup() == console.FANCY

    def test_cp866_console_falls_back_to_ascii(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdout", cp866_stream())
        assert console.setup() == console.PLAIN

    def test_chosen_marks_actually_encode(self, monkeypatch) -> None:
        """Главное: выбранные знаки обязаны пройти в ту кодировку, что есть."""
        stream = cp866_stream()
        monkeypatch.setattr("sys.stdout", stream)
        marks = console.setup()

        for value in marks.values():
            value.encode("cp866")  # упадёт, если выбор неверен

    def test_unknown_encoding_does_not_crash(self, monkeypatch) -> None:
        class Weird:
            encoding = "не-такой-кодировки-нет"
            reconfigure = None

        monkeypatch.setattr("sys.stdout", Weird())
        assert console.setup() == console.PLAIN

    def test_stream_without_encoding_attribute(self, monkeypatch) -> None:
        class Bare:
            reconfigure = None

        monkeypatch.setattr("sys.stdout", Bare())
        assert console.setup() == console.PLAIN

    def test_both_sets_have_the_same_keys(self) -> None:
        """Иначе подстановка знаков упадёт по KeyError на нестандартной консоли."""
        assert set(console.FANCY) == set(console.PLAIN)
