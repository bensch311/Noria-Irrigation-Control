"""
Tests für die reinen Hilfsfunktionen in app.py (Shiny Express Frontend).

Da app.py ein Shiny Express Modul ist, werden alle Shiny-Abhängigkeiten
vor dem Import durch MagicMocks ersetzt. Nur reine Hilfsfunktionen ohne
Shiny-Abhängigkeiten werden getestet. Das ist der einzig sinnvolle
Unit-Test-Ansatz für Shiny Express ohne laufende Shiny-Session.

Getestet:
  fmt_mmss()                        – Sekunden → "MM:SS"-String
  fmt_duration()                    – Dauer-Formatierung (Sek/Min)
  fmt_weekdays()                    – Wochentags-Liste → lesbarer String
  _json_or_none()                   – sichere JSON-Extraktion aus Response/None
  _load_frontend_config()           – Config-Laden mit Fallback bei fehlendem File
  _read_max_valves_from_device_config() – MAX_VALVES aus device_config.json
"""

# ---------------------------------------------------------------------------
# Shiny und faicons VOR dem Import von app.py mocken.
#
# app.py führt beim Import den kompletten Shiny-Express-Modul-Body aus –
# das schließt UI-Rendering-Code (with ui.page_navbar: ...) und reaktive
# Werte (reactive.Value) ein. Ohne Mocking würde der Import fehlschlagen
# oder eine laufende Shiny-Session voraussetzen.
#
# MagicMock unterstützt Context-Manager-Protokoll (__enter__/__exit__)
# und beliebige Attributzugriffe → alle with-Blöcke und Dekoratoren
# in app.py arbeiten problemlos mit MagicMock-Objekten.
# ---------------------------------------------------------------------------
import sys
from unittest.mock import MagicMock

for _shiny_mod in [
    "shiny",
    "shiny.express",
    "shiny.express.ui",
    "faicons",
]:
    if _shiny_mod not in sys.modules:
        sys.modules[_shiny_mod] = MagicMock()

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock as _MagicMock

import app as _app  # noqa: E402 – sicher nach Mocking


# ─────────────────────────────────────────────────────────────────────────────
# fmt_mmss
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtMmss:
    """fmt_mmss(total_s) → 'MM:SS'"""

    def test_zero_seconds(self):
        assert _app.fmt_mmss(0) == "0:00"

    def test_one_second(self):
        assert _app.fmt_mmss(1) == "0:01"

    def test_59_seconds(self):
        assert _app.fmt_mmss(59) == "0:59"

    def test_exactly_one_minute(self):
        assert _app.fmt_mmss(60) == "1:00"

    def test_90_seconds(self):
        assert _app.fmt_mmss(90) == "1:30"

    def test_one_hour(self):
        assert _app.fmt_mmss(3600) == "60:00"

    def test_negative_becomes_zero(self):
        """Negative Sekunden werden auf 0 geclampt (max(0, ...) in Impl)."""
        assert _app.fmt_mmss(-10) == "0:00"

    def test_seconds_always_two_digits(self):
        """Sekunden-Teil immer zweistellig mit führender Null."""
        result = _app.fmt_mmss(61)  # 1 Minute, 1 Sekunde
        assert result == "1:01"

    def test_large_values(self):
        """Großer Wert: 2h = 7200 s → 120:00"""
        assert _app.fmt_mmss(7200) == "120:00"


# ─────────────────────────────────────────────────────────────────────────────
# fmt_duration
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtDuration:
    """fmt_duration(duration_s, time_unit) → lesbarer String"""

    def test_minuten_unit_shows_min(self):
        assert _app.fmt_duration(120, "Minuten") == "2 Min"

    def test_sekunden_divisible_by_60_shows_min(self):
        """Auch im Sekunden-Modus: bei glatter Minute → Min-Anzeige."""
        assert _app.fmt_duration(120, "Sekunden") == "2 Min"

    def test_sekunden_not_divisible_shows_sek(self):
        assert _app.fmt_duration(90, "Sekunden") == "90 Sek"

    def test_sekunden_single_second(self):
        assert _app.fmt_duration(1, "Sekunden") == "1 Sek"

    def test_minuten_fractional_minutes(self):
        """Minuten-Modus zeigt immer Min, auch wenn nicht ganzzahlig."""
        assert _app.fmt_duration(90, "Minuten") == "1 Min"

    def test_60_seconds_as_minuten_shows_1_min(self):
        assert _app.fmt_duration(60, "Minuten") == "1 Min"


# ─────────────────────────────────────────────────────────────────────────────
# fmt_weekdays
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtWeekdays:
    """fmt_weekdays(weekdays) → kommaseparierter Kurzname-String"""

    def test_monday_only(self):
        assert _app.fmt_weekdays([0]) == "Mo"

    def test_full_week(self):
        result = _app.fmt_weekdays([0, 1, 2, 3, 4, 5, 6])
        # Alle 7 Tage, kommasepariert, sortiert
        assert "Mo" in result
        assert "So" in result
        assert result.count(",") == 6

    def test_weekend_only(self):
        result = _app.fmt_weekdays([5, 6])
        assert "Sa" in result
        assert "So" in result

    def test_output_is_sorted_by_weekday(self):
        """Reihenfolge im Output entspricht der Wochentag-Sortierung."""
        result = _app.fmt_weekdays([6, 0, 3])  # unsortiert übergeben
        parts = [p.strip() for p in result.split(",")]
        assert parts == ["Mo", "Do", "So"]

    def test_empty_list(self):
        """Leere Liste → leerer String (kein Crash)."""
        assert _app.fmt_weekdays([]) == ""

    def test_unknown_weekday_falls_back_to_str(self):
        """Unbekannter Wochentag wird als Zahl ausgegeben (Fallback)."""
        result = _app.fmt_weekdays([99])
        assert "99" in result


# ─────────────────────────────────────────────────────────────────────────────
# _json_or_none
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonOrNone:
    """_json_or_none(response) → dict oder None"""

    def test_none_input_returns_none(self):
        assert _app._json_or_none(None) is None

    def test_valid_response_returns_dict(self):
        mock_resp = _MagicMock()
        mock_resp.json.return_value = {"ok": True, "zone": 1}
        result = _app._json_or_none(mock_resp)
        assert result == {"ok": True, "zone": 1}

    def test_response_json_raises_returns_none(self):
        """Wenn r.json() eine Exception wirft, wird None zurückgegeben."""
        mock_resp = _MagicMock()
        mock_resp.json.side_effect = ValueError("invalid json")
        result = _app._json_or_none(mock_resp)
        assert result is None

    def test_response_json_raises_request_exception_returns_none(self):
        """Auch RequestException / andere Exceptions werden abgefangen."""
        import requests
        mock_resp = _MagicMock()
        mock_resp.json.side_effect = requests.exceptions.JSONDecodeError("x", "", 0)
        result = _app._json_or_none(mock_resp)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# _load_frontend_config
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadFrontendConfig:
    """_load_frontend_config() lädt die Konfig oder gibt sichere Defaults zurück."""

    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        """Fehlende frontend_config.json → sichere Defaults."""
        monkeypatch.chdir(tmp_path)
        # Kein frontend_config.json vorhanden
        cfg = _app._load_frontend_config()
        assert "base_url" in cfg
        assert "poll_status_s" in cfg
        assert isinstance(cfg["poll_status_s"], int)

    def test_valid_config_overrides_defaults(self, tmp_path, monkeypatch):
        """Gültige frontend_config.json überschreibt Default-Werte."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        config_data = {"base_url": "http://192.168.1.10:8000", "poll_status_s": 2}
        (tmp_path / "data" / "frontend_config.json").write_text(
            json.dumps(config_data), encoding="utf-8"
        )
        cfg = _app._load_frontend_config()
        assert cfg["base_url"] == "http://192.168.1.10:8000"
        assert cfg["poll_status_s"] == 2

    def test_corrupt_json_returns_defaults(self, tmp_path, monkeypatch):
        """Kaputtes JSON → Defaults, kein Crash."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "frontend_config.json").write_text(
            "{corrupt json!!!", encoding="utf-8"
        )
        cfg = _app._load_frontend_config()
        assert "base_url" in cfg  # Defaults zurückgegeben

    def test_private_keys_filtered(self, tmp_path, monkeypatch):
        """Keys mit '_'-Präfix (Kommentare) werden nicht übernommen."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        config_data = {"_comment": "This is a comment", "poll_status_s": 3}
        (tmp_path / "data" / "frontend_config.json").write_text(
            json.dumps(config_data), encoding="utf-8"
        )
        cfg = _app._load_frontend_config()
        assert "_comment" not in cfg
        assert cfg["poll_status_s"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# _read_max_valves_from_device_config
# ─────────────────────────────────────────────────────────────────────────────

class TestReadMaxValves:
    """_read_max_valves_from_device_config(fallback) liest MAX_VALVES aus device_config.json."""

    def test_missing_file_returns_fallback(self, tmp_path, monkeypatch):
        """Fehlende Datei → fallback-Wert."""
        monkeypatch.chdir(tmp_path)
        result = _app._read_max_valves_from_device_config(fallback=6)
        assert result == 6

    def test_valid_config_returns_max_valves(self, tmp_path, monkeypatch):
        """Gültige Konfig → korrekte MAX_VALVES."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        cfg = {"device": {"MAX_VALVES": 4}}
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )
        result = _app._read_max_valves_from_device_config(fallback=6)
        assert result == 4

    def test_max_valves_minimum_is_1(self, tmp_path, monkeypatch):
        """MAX_VALVES=0 in Konfig → min. 1 (max(1, ...))."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        cfg = {"device": {"MAX_VALVES": 0}}
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )
        result = _app._read_max_valves_from_device_config(fallback=6)
        assert result == 1

    def test_corrupt_json_returns_fallback(self, tmp_path, monkeypatch):
        """Kaputtes JSON → fallback, kein Crash."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )
        result = _app._read_max_valves_from_device_config(fallback=6)
        assert result == 6

    def test_missing_device_key_returns_fallback(self, tmp_path, monkeypatch):
        """Konfig ohne 'device'-Schlüssel → fallback."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        cfg = {"other_key": 42}
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )
        result = _app._read_max_valves_from_device_config(fallback=6)
        assert result == 6
