"""
Tests für app_helpers.py (reine Hilfsfunktionen des Frontends).

app_helpers.py hat keine Shiny-Abhängigkeiten und kann direkt importiert
werden. Kein Mocking von Shiny nötig.

Getestet:
  fmt_mmss()                                    – Sekunden → "M:SS"-String
  fmt_duration()                                – Dauer-Formatierung (Sek/Min)
  fmt_weekdays()                                – Wochentags-Liste → lesbarer String
  fmt_uptime()                                  – Uptime-Sekunden → lesbarer String
  fmt_disk()                                    – Disk-Nutzung → lesbarer String
  fmt_memory()                                  – RAM-Nutzung → lesbarer String
  fmt_signal()                                  – WLAN-Signal → lesbarer String mit Qualität
  _json_or_none()                               – sichere JSON-Extraktion aus Response/None
  _load_frontend_config()                       – Config-Laden mit Fallback bei fehlendem File
  _read_max_valves_from_device_config()         – MAX_VALVES aus device_config.json
  _read_sensors_enabled_from_device_config()    – Sensoren aktiv aus device_config.json
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
# fmt_uptime
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtUptime:
    """fmt_uptime(seconds) → lesbarer Uptime-String"""

    def test_zero_seconds(self):
        assert h.fmt_uptime(0) == "0 Min"

    def test_under_one_minute(self):
        """Weniger als 60s → '0 Min' (Minuten-Granularität)."""
        assert h.fmt_uptime(45) == "0 Min"

    def test_exactly_one_minute(self):
        assert h.fmt_uptime(60) == "1 Min"

    def test_90_seconds(self):
        assert h.fmt_uptime(90) == "1 Min"

    def test_exactly_one_hour(self):
        assert h.fmt_uptime(3600) == "1 Std"

    def test_one_hour_30_min(self):
        assert h.fmt_uptime(5400) == "1 Std 30 Min"

    def test_exactly_one_day(self):
        assert h.fmt_uptime(86400) == "1 Tag"

    def test_singular_tag(self):
        """1 Tag (nicht 'Tage')."""
        result = h.fmt_uptime(86400)
        assert "Tag" in result
        assert "Tage" not in result

    def test_plural_tage(self):
        """2 Tage (nicht 'Tag')."""
        result = h.fmt_uptime(172800)
        assert "2 Tage" in result

    def test_full_components(self):
        """1 Tag + 1 Std + 1 Min → alle drei Parts."""
        result = h.fmt_uptime(86400 + 3600 + 60)
        assert "1 Tag" in result
        assert "1 Std" in result
        assert "1 Min" in result

    def test_no_minutes_if_zero_and_hours_present(self):
        """Wenn Stunden vorhanden aber Minuten = 0: keine '0 Min' ausgeben."""
        result = h.fmt_uptime(7200)  # genau 2 Stunden
        assert "Min" not in result

    def test_no_hours_if_zero_and_days_present(self):
        """Wenn Tage vorhanden aber Stunden = 0: keine '0 Std' ausgeben."""
        result = h.fmt_uptime(86400 + 30)  # 1 Tag + 30 Sek
        assert "Std" not in result

    def test_negative_clamped_to_zero(self):
        assert h.fmt_uptime(-100) == "0 Min"


# ─────────────────────────────────────────────────────────────────────────────
# fmt_disk
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtDisk:
    """fmt_disk(free_gb, total_gb, used_pct) → lesbarer String"""

    def test_all_none_returns_dash(self):
        assert h.fmt_disk(None, None, None) == "–"

    def test_partial_none_returns_dash(self):
        assert h.fmt_disk(10.0, None, 50.0) == "–"

    def test_valid_values(self):
        result = h.fmt_disk(12.3, 29.8, 58.7)
        assert "12" in result
        assert "29" in result
        assert "59" in result  # gerundete Prozentangabe
        assert "GB" in result

    def test_full_disk_zero_free(self):
        result = h.fmt_disk(0.0, 16.0, 100.0)
        assert "100" in result
        assert "GB" in result

    def test_contains_frei(self):
        result = h.fmt_disk(5.0, 16.0, 68.75)
        assert "frei" in result


# ─────────────────────────────────────────────────────────────────────────────
# fmt_memory
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtMemory:
    """fmt_memory(used_mb, total_mb, used_pct) → lesbarer String"""

    def test_all_none_returns_dash(self):
        assert h.fmt_memory(None, None, None) == "–"

    def test_partial_none_returns_dash(self):
        assert h.fmt_memory(512, None, 50.0) == "–"

    def test_valid_values(self):
        result = h.fmt_memory(312, 1024, 30.5)
        assert "312" in result
        assert "1024" in result
        assert "31" in result  # gerundete Prozentangabe
        assert "MB" in result

    def test_full_ram(self):
        result = h.fmt_memory(1024, 1024, 100.0)
        assert "100" in result

    def test_zero_used(self):
        result = h.fmt_memory(0, 1024, 0.0)
        assert "0" in result


# ─────────────────────────────────────────────────────────────────────────────
# fmt_signal
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtSignal:
    """fmt_signal(signal_pct) → Qualitätsstufe + Prozentzahl"""

    def test_none_returns_dash(self):
        assert h.fmt_signal(None) == "–"

    def test_strong_signal(self):
        result = h.fmt_signal(75)
        assert "Gut" in result
        assert "75" in result

    def test_medium_signal(self):
        result = h.fmt_signal(50)
        assert "Mittel" in result
        assert "50" in result

    def test_weak_signal(self):
        result = h.fmt_signal(20)
        assert "Schwach" in result
        assert "20" in result

    def test_boundary_gut_67(self):
        """Genau 67 % → Gut."""
        assert "Gut" in h.fmt_signal(67)

    def test_boundary_mittel_66(self):
        """66 % → noch Mittel."""
        assert "Mittel" in h.fmt_signal(66)

    def test_boundary_mittel_34(self):
        """34 % → noch Mittel."""
        assert "Mittel" in h.fmt_signal(34)

    def test_boundary_schwach_33(self):
        """33 % → Schwach."""
        assert "Schwach" in h.fmt_signal(33)

    def test_zero_signal(self):
        result = h.fmt_signal(0)
        assert "Schwach" in result
        assert "0" in result

    def test_100_signal(self):
        result = h.fmt_signal(100)
        assert "Gut" in result
        assert "100" in result


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


# ─────────────────────────────────────────────────────────────────────────────
# _read_sensors_enabled_from_device_config
# ─────────────────────────────────────────────────────────────────────────────

class TestReadSensorsEnabled:
    """_read_sensors_enabled_from_device_config() → True/False"""

    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        """Fehlende device_config.json → False (kein Sensor-Tab)."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        assert h._read_sensors_enabled_from_device_config() is False

    def test_sensors_with_pins_returns_true(self, tmp_path, monkeypatch):
        """IRRIGATION_SENSOR_PINS mit Eintraegen → True."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({
                "sensors": {
                    "IRRIGATION_SENSOR_PINS": {"1": 14, "2": 15}
                }
            }),
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is True

    def test_sensors_empty_pins_returns_false(self, tmp_path, monkeypatch):
        """IRRIGATION_SENSOR_PINS ist leeres Objekt → False (kein Sensor installiert)."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({
                "sensors": {
                    "IRRIGATION_SENSOR_DRIVER": "sim",
                    "IRRIGATION_SENSOR_PINS": {}
                }
            }),
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is False

    def test_sensors_key_missing_returns_false(self, tmp_path, monkeypatch):
        """Kein 'sensors'-Schluessel in device_config.json → False."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"device": {"MAX_VALVES": 6}}),
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is False

    def test_sensor_pins_key_missing_returns_false(self, tmp_path, monkeypatch):
        """'sensors' vorhanden aber 'IRRIGATION_SENSOR_PINS' fehlt → False."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({
                "sensors": {
                    "IRRIGATION_SENSOR_DRIVER": "sim"
                    # IRRIGATION_SENSOR_PINS fehlt absichtlich
                }
            }),
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is False

    def test_corrupt_json_returns_false(self, tmp_path, monkeypatch):
        """Korruptes JSON → False, kein Crash."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            "{nicht: valides json!!!",
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is False

    def test_single_sensor_returns_true(self, tmp_path, monkeypatch):
        """Ein einzelner Sensor genuegt fuer True."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({
                "sensors": {
                    "IRRIGATION_SENSOR_PINS": {"1": 14}
                }
            }),
            encoding="utf-8",
        )
        assert h._read_sensors_enabled_from_device_config() is True

    def test_full_device_config_with_sensors(self, tmp_path, monkeypatch):
        """Vollstaendige device_config.json wie von install.sh erzeugt – mit Sensoren."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        full_cfg = {
            "version": 1,
            "device": {
                "MAX_VALVES": 6,
                "IRRIGATION_VALVE_DRIVER": "rpi",
                "IRRIGATION_RELAY_ACTIVE_LOW": True,
                "IRRIGATION_GPIO_PINS": {"1": 17, "2": 18}
            },
            "sensors": {
                "IRRIGATION_SENSOR_DRIVER": "rpi_switch",
                "IRRIGATION_SENSOR_INTERNAL_PULL_UP": False,
                "IRRIGATION_SENSOR_PINS": {"1": 14, "2": 15},
                "IRRIGATION_SENSOR_POLLING_INTERVAL_S": 10,
                "IRRIGATION_SENSOR_COOLDOWN_S": 60,
                "IRRIGATION_SENSOR_DEFAULT_DURATION_S": 30
            },
            "hard_limits": {
                "MAX_RUNTIME_S": 3600,
                "MAX_CONCURRENT_VALVES": 2
            }
        }
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps(full_cfg), encoding="utf-8"
        )
        assert h._read_sensors_enabled_from_device_config() is True

    def test_full_device_config_without_sensors(self, tmp_path, monkeypatch):
        """Vollstaendige device_config.json wie von install.sh erzeugt – ohne Sensoren."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        full_cfg = {
            "version": 1,
            "device": {
                "MAX_VALVES": 6,
                "IRRIGATION_VALVE_DRIVER": "rpi",
                "IRRIGATION_RELAY_ACTIVE_LOW": True,
                "IRRIGATION_GPIO_PINS": {"1": 17, "2": 18}
            },
            "sensors": {
                "IRRIGATION_SENSOR_DRIVER": "sim",
                "IRRIGATION_SENSOR_INTERNAL_PULL_UP": False,
                "IRRIGATION_SENSOR_PINS": {},
                "IRRIGATION_SENSOR_POLLING_INTERVAL_S": 10,
                "IRRIGATION_SENSOR_COOLDOWN_S": 60,
                "IRRIGATION_SENSOR_DEFAULT_DURATION_S": 30
            },
            "hard_limits": {
                "MAX_RUNTIME_S": 3600,
                "MAX_CONCURRENT_VALVES": 2
            }
        }
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps(full_cfg), encoding="utf-8"
        )
        assert h._read_sensors_enabled_from_device_config() is False


# ─────────────────────────────────────────────────────────────────────────────
# _read_sensor_ids_from_device_config
# ─────────────────────────────────────────────────────────────────────────────

class TestReadSensorIds:
    """_read_sensor_ids_from_device_config() → sortierte Liste von Sensor-IDs."""

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """Fehlende device_config.json → leere Liste."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        assert h._read_sensor_ids_from_device_config() == []

    def test_two_sensors_returns_sorted_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"sensors": {"IRRIGATION_SENSOR_PINS": {"2": 15, "1": 14}}}),
            encoding="utf-8",
        )
        result = h._read_sensor_ids_from_device_config()
        assert result == [1, 2]

    def test_single_sensor_returns_list_of_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"sensors": {"IRRIGATION_SENSOR_PINS": {"1": 14}}}),
            encoding="utf-8",
        )
        assert h._read_sensor_ids_from_device_config() == [1]

    def test_empty_pins_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"sensors": {"IRRIGATION_SENSOR_PINS": {}}}),
            encoding="utf-8",
        )
        assert h._read_sensor_ids_from_device_config() == []

    def test_missing_sensors_key_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"device": {"MAX_VALVES": 6}}),
            encoding="utf-8",
        )
        assert h._read_sensor_ids_from_device_config() == []

    def test_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            "{corrupt!!!", encoding="utf-8"
        )
        assert h._read_sensor_ids_from_device_config() == []

    def test_result_is_sorted(self, tmp_path, monkeypatch):
        """IDs werden sortiert zurückgegeben, egal wie sie in der Datei stehen."""
        monkeypatch.setattr(h, "__file__", str(tmp_path / "app_helpers.py"))
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "device_config.json").write_text(
            json.dumps({"sensors": {"IRRIGATION_SENSOR_PINS": {"3": 16, "1": 14, "2": 15}}}),
            encoding="utf-8",
        )
        assert h._read_sensor_ids_from_device_config() == [1, 2, 3]
