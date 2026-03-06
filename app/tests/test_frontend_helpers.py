"""
Tests für app_helpers.py (reine Hilfsfunktionen des Frontends).

app_helpers.py hat keine Shiny-Abhängigkeiten und kann direkt importiert
werden. Kein Mocking von Shiny nötig.

Getestet:
  fmt_mmss()                            – Sekunden → "M:SS"-String
  fmt_duration()                        – Dauer-Formatierung (Sek/Min)
  fmt_weekdays()                        – Wochentags-Liste → lesbarer String
  _json_or_none()                       – sichere JSON-Extraktion aus Response/None
  _load_frontend_config()               – Config-Laden mit Fallback bei fehlendem File
  _read_max_valves_from_device_config() – MAX_VALVES aus device_config.json
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

import app_helpers as h


# ─────────────────────────────────────────────────────────────────────────────
# fmt_mmss
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtMmss:
    """fmt_mmss(total_s) → 'M:SS'"""

    def test_zero_seconds(self):
        assert h.fmt_mmss(0) == "0:00"

    def test_one_second(self):
        assert h.fmt_mmss(1) == "0:01"

    def test_59_seconds(self):
        assert h.fmt_mmss(59) == "0:59"

    def test_exactly_one_minute(self):
        assert h.fmt_mmss(60) == "1:00"

    def test_90_seconds(self):
        assert h.fmt_mmss(90) == "1:30"

    def test_one_hour(self):
        assert h.fmt_mmss(3600) == "60:00"

    def test_negative_becomes_zero(self):
        """Negative Sekunden werden auf 0 geclampt."""
        assert h.fmt_mmss(-10) == "0:00"

    def test_seconds_always_two_digits(self):
        """Sekunden-Teil immer zweistellig mit führender Null."""
        assert h.fmt_mmss(61) == "1:01"

    def test_large_value(self):
        """2h = 7200 s → 120:00"""
        assert h.fmt_mmss(7200) == "120:00"


# ─────────────────────────────────────────────────────────────────────────────
# fmt_duration
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtDuration:
    """fmt_duration(duration_s, time_unit) → lesbarer String"""

    def test_minuten_unit_shows_min(self):
        assert h.fmt_duration(120, "Minuten") == "2 Min"

    def test_sekunden_divisible_by_60_shows_min(self):
        """Auch im Sekunden-Modus: bei glatter Minute → Min-Anzeige."""
        assert h.fmt_duration(120, "Sekunden") == "2 Min"

    def test_sekunden_not_divisible_shows_sek(self):
        assert h.fmt_duration(90, "Sekunden") == "90 Sek"

    def test_sekunden_single_second(self):
        assert h.fmt_duration(1, "Sekunden") == "1 Sek"

    def test_minuten_fractional_shows_floor(self):
        """Minuten-Modus: ganzzahlige Division (90s → 1 Min)."""
        assert h.fmt_duration(90, "Minuten") == "1 Min"

    def test_60_seconds_as_minuten_shows_1_min(self):
        assert h.fmt_duration(60, "Minuten") == "1 Min"


# ─────────────────────────────────────────────────────────────────────────────
# fmt_weekdays
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtWeekdays:
    """fmt_weekdays(weekdays) → kommaseparierter Kurzname-String"""

    def test_monday_only(self):
        assert h.fmt_weekdays([0]) == "Mo"

    def test_full_week(self):
        result = h.fmt_weekdays([0, 1, 2, 3, 4, 5, 6])
        assert "Mo" in result
        assert "So" in result
        assert result.count(",") == 6

    def test_weekend_only(self):
        result = h.fmt_weekdays([5, 6])
        assert "Sa" in result
        assert "So" in result

    def test_output_sorted_regardless_of_input_order(self):
        """Reihenfolge im Output entspricht der Wochentag-Sortierung."""
        result = h.fmt_weekdays([6, 0, 3])
        parts = [p.strip() for p in result.split(",")]
        assert parts == ["Mo", "Do", "So"]

    def test_empty_list(self):
        """Leere Liste → leerer String, kein Crash."""
        assert h.fmt_weekdays([]) == ""

    def test_unknown_weekday_falls_back_to_str(self):
        """Unbekannter Index → Zahl als Fallback."""
        result = h.fmt_weekdays([99])
        assert "99" in result


# ─────────────────────────────────────────────────────────────────────────────
# _json_or_none
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonOrNone:
    """_json_or_none(response) → dict oder None"""

    def test_none_input_returns_none(self):
        assert h._json_or_none(None) is None

    def test_valid_response_returns_dict(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "zone": 1}
        assert h._json_or_none(mock_resp) == {"ok": True, "zone": 1}

    def test_response_json_raises_returns_none(self):
        """Wenn r.json() eine Exception wirft, wird None zurückgegeben."""
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("invalid json")
        assert h._json_or_none(mock_resp) is None

    def test_response_json_decode_error_returns_none(self):
        import requests as req
        mock_resp = MagicMock()
        mock_resp.json.side_effect = req.exceptions.JSONDecodeError("x", "", 0)
        assert h._json_or_none(mock_resp) is None


# ─────────────────────────────────────────────────────────────────────────────
# _load_frontend_config
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadFrontendConfig:
    """_load_frontend_config() lädt die Konfig oder gibt sichere Defaults zurück."""

    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        """Fehlende frontend_config.json → sichere Defaults."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        cfg = h._load_frontend_config()
        assert "base_url" in cfg
        assert isinstance(cfg["poll_status_s"], int)

    def test_valid_config_overrides_defaults(self, tmp_path, monkeypatch):
        """Gültige frontend_config.json überschreibt Default-Werte."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "frontend_config.json").write_text(
            json.dumps({"base_url": "http://192.168.1.10:8000", "poll_status_s": 2}),
            encoding="utf-8",
        )
        cfg = h._load_frontend_config()
        assert cfg["base_url"] == "http://192.168.1.10:8000"
        assert cfg["poll_status_s"] == 2

    def test_corrupt_json_returns_defaults(self, tmp_path, monkeypatch):
        """Kaputtes JSON → Defaults, kein Crash."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "frontend_config.json").write_text(
            "{corrupt json!!!", encoding="utf-8"
        )
        cfg = h._load_frontend_config()
        assert "base_url" in cfg

    def test_private_keys_filtered(self, tmp_path, monkeypatch):
        """Keys mit '_'-Präfix (Kommentare) werden nicht übernommen."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "frontend_config.json").write_text(
            json.dumps({"_comment": "ignored", "poll_status_s": 3}),
            encoding="utf-8",
        )
        cfg = h._load_frontend_config()
        assert "_comment" not in cfg
        assert cfg["poll_status_s"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# _read_max_valves_from_device_config
# ─────────────────────────────────────────────────────────────────────────────

class TestReadMaxValves:
    """_read_max_valves_from_device_config(fallback) liest MAX_VALVES."""

    def test_missing_file_returns_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        assert h._read_max_valves_from_device_config(fallback=6) == 6

    def test_valid_config_returns_max_valves(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"device": {"MAX_VALVES": 4}}), encoding="utf-8"
        )
        assert h._read_max_valves_from_device_config(fallback=6) == 4

    def test_max_valves_minimum_is_1(self, tmp_path, monkeypatch):
        """MAX_VALVES=0 in Konfig → min. 1 (max(1, ...))."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"device": {"MAX_VALVES": 0}}), encoding="utf-8"
        )
        assert h._read_max_valves_from_device_config(fallback=6) == 1

    def test_corrupt_json_returns_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )
        assert h._read_max_valves_from_device_config(fallback=6) == 6

    def test_missing_device_key_returns_fallback(self, tmp_path, monkeypatch):
        """Konfig ohne 'device'-Schlüssel → fallback."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"other_key": 42}), encoding="utf-8"
        )
        assert h._read_max_valves_from_device_config(fallback=6) == 6
