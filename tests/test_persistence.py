"""
Tests für services/persistence.py

Getestet werden:
  - save/load schedules      (Roundtrip)
  - save/load queue          (Roundtrip)
  - save/load history        (Roundtrip + Limit)
  - load_device_config       (valide Datei, fehlende Datei, korrupte Datei)
  - load_user_settings       (valide, fehlende, korrupte Datei)
  - load_runtime_state       (valide, Clamping)
  - _atomic_write_json       (atomares Schreiben)
"""

import json
import os
import pytest

from core.state import state, state_lock, QueueItem, ScheduleRule, HistoryItem


# ─────────────────────────────────────────────────────────────────────────────
# Schedules
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_schedules_roundtrip(tmp_path, monkeypatch, make_schedule):
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    with state_lock:
        state.schedules = [
            make_schedule(zone=1, weekdays=[0, 1], start_times=["06:00"], duration_s=120, rule_id="s01"),
            make_schedule(zone=2, weekdays=[5, 6], start_times=["08:00", "20:00"], duration_s=60, rule_id="s02"),
        ]
        state.automation_enabled = True

    pers.save_schedules_to_disk()

    # State zurücksetzen
    with state_lock:
        state.schedules = []
        state.automation_enabled = False

    pers.load_schedules_from_disk()

    with state_lock:
        assert len(state.schedules) == 2
        assert state.schedules[0].id == "s01"
        assert state.schedules[0].zone == 1
        assert state.schedules[0].weekdays == [0, 1]
        assert state.schedules[0].start_times == ["06:00"]
        assert state.schedules[0].duration_s == 120
        assert state.schedules[1].id == "s02"
        assert state.automation_enabled is True
        assert state.schedules_dirty is False


def test_load_schedules_missing_file(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "nonexistent.json"))

    pers.load_schedules_from_disk()  # Darf keinen Fehler werfen

    with state_lock:
        assert state.schedules == []  # Unverändert


def test_save_schedules_sets_not_dirty(tmp_path, monkeypatch, make_schedule):
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    with state_lock:
        state.schedules = [make_schedule(zone=1)]
        state.schedules_dirty = True

    pers.save_schedules_to_disk()

    with state_lock:
        assert state.schedules_dirty is False


# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_queue_roundtrip(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))

    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=3, duration=120, time_unit="Minuten", source="schedule"),
        ]
        state.queue_state = "läuft"

    pers.save_queue_to_disk()

    with state_lock:
        state.queue = []
        state.queue_state = "bereit"

    pers.load_queue_from_disk()

    with state_lock:
        assert len(state.queue) == 2
        assert state.queue[0].zone == 1
        assert state.queue[0].duration == 60
        assert state.queue[1].zone == 3
        assert state.queue[1].time_unit == "Minuten"
        assert state.queue_state == "bereit"   # wird beim Laden auf bereit gesetzt
        assert state.queue_dirty is False


def test_load_queue_missing_file(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "nope.json"))

    pers.load_queue_from_disk()  # Darf keinen Fehler werfen
    with state_lock:
        assert state.queue == []


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_history_roundtrip(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

    with state_lock:
        state.run_history = [
            HistoryItem(ts_end="2025-01-01T06:01:00+01:00", zone=1, duration_s=60, source="manual", time_unit="Sekunden"),
            HistoryItem(ts_end="2025-01-01T07:00:00+01:00", zone=2, duration_s=120, source="schedule", time_unit="Sekunden"),
        ]

    pers.save_history_to_disk()

    with state_lock:
        state.run_history = []

    pers.load_history_from_disk()

    with state_lock:
        assert len(state.run_history) == 2
        assert state.run_history[0].zone == 1
        assert state.run_history[1].zone == 2
        assert state.history_dirty is False


def test_load_history_respects_limit(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

    with state_lock:
        state.run_history = [
            HistoryItem(ts_end="2025-01-01T06:00:00+01:00", zone=i, duration_s=10, source="manual", time_unit="Sekunden")
            for i in range(1, 11)
        ]
        state.max_history_items = 5

    pers.save_history_to_disk()
    with state_lock:
        state.run_history = []

    pers.load_history_from_disk()

    with state_lock:
        assert len(state.run_history) == 5


def test_load_history_missing_file(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "nope.json"))

    pers.load_history_from_disk()
    with state_lock:
        assert state.run_history == []


# ─────────────────────────────────────────────────────────────────────────────
# Device Config
# ─────────────────────────────────────────────────────────────────────────────


def test_load_device_config_valid(tmp_path, monkeypatch):
    import services.persistence as pers

    config_path = str(tmp_path / "device_config.json")
    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", config_path)

    payload = {
        "version": 1,
        "device": {
            "MAX_VALVES": 4,
            "IRRIGATION_VALVE_DRIVER": "sim",
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},
        },
        "hard_limits": {
            "MAX_RUNTIME_S": 1800,
            "MAX_CONCURRENT_VALVES": 2,
        },
    }
    with open(config_path, "w") as f:
        json.dump(payload, f)

    pers.load_device_config_from_disk()

    with state_lock:
        assert state.max_valves == 4
        assert state.valve_driver_mode == "sim"
        assert state.hard_max_runtime_s == 1800


def test_load_device_config_missing_creates_template(tmp_path, monkeypatch):
    import services.persistence as pers

    config_path = str(tmp_path / "device_config.json")
    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", config_path)

    pers.load_device_config_from_disk()

    assert os.path.exists(config_path)  # Template wurde erstellt
    with state_lock:
        assert state.max_valves >= 1   # Defaults geladen


def test_load_device_config_corrupt_uses_defaults(tmp_path, monkeypatch):
    import services.persistence as pers

    config_path = str(tmp_path / "device_config.json")
    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", config_path)

    with open(config_path, "w") as f:
        f.write("INVALID JSON {{{")

    pers.load_device_config_from_disk()  # Darf keinen Fehler werfen

    # Backup-Datei sollte erstellt worden sein
    backup_files = [f for f in os.listdir(tmp_path) if "corrupt" in f]
    assert len(backup_files) == 1


# ─────────────────────────────────────────────────────────────────────────────
# User Settings
# ─────────────────────────────────────────────────────────────────────────────


def test_load_user_settings_valid(tmp_path, monkeypatch):
    import services.persistence as pers

    settings_path = str(tmp_path / "user_settings.json")
    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", settings_path)

    payload = {
        "version": 1,
        "user": {"MAX_HISTORY_ITEMS": 50},
    }
    with open(settings_path, "w") as f:
        json.dump(payload, f)

    pers.load_user_settings_from_disk()

    with state_lock:
        assert state.max_history_items == 50


def test_load_user_settings_missing_creates_defaults(tmp_path, monkeypatch):
    import services.persistence as pers

    settings_path = str(tmp_path / "user_settings.json")
    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", settings_path)

    pers.load_user_settings_from_disk()

    assert os.path.exists(settings_path)
    with state_lock:
        assert state.max_history_items >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Runtime State
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_runtime_state_roundtrip(tmp_path, monkeypatch):
    import services.persistence as pers

    rt_path = str(tmp_path / "runtime_state.json")
    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", rt_path)

    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2

    pers.save_runtime_state_to_disk()

    with state_lock:
        state.parallel_enabled = False
        state.max_concurrent_valves = 1

    pers.load_runtime_state_from_disk()

    with state_lock:
        assert state.parallel_enabled is True
        assert state.max_concurrent_valves == 2


def test_load_runtime_state_clamped_to_hard_limit(tmp_path, monkeypatch):
    import services.persistence as pers

    rt_path = str(tmp_path / "runtime_state.json")
    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", rt_path)

    payload = {
        "version": 1,
        "runtime": {
            "parallel_enabled": True,
            "max_concurrent_valves": 99,  # Weit über Hard-Limit
        },
    }
    with open(rt_path, "w") as f:
        json.dump(payload, f)

    with state_lock:
        state.hard_max_concurrent_valves = 2
        state.max_valves = 6

    pers.load_runtime_state_from_disk()

    with state_lock:
        assert state.max_concurrent_valves <= 2  # Geclampter Wert


# ─────────────────────────────────────────────────────────────────────────────
# _atomic_write_json
# ─────────────────────────────────────────────────────────────────────────────


def test_atomic_write_creates_file(tmp_path):
    from services.persistence import _atomic_write_json

    target = str(tmp_path / "test.json")
    _atomic_write_json(target, {"key": "value", "num": 42})

    assert os.path.exists(target)
    with open(target) as f:
        data = json.load(f)
    assert data["key"] == "value"
    assert data["num"] == 42


def test_atomic_write_no_temp_file_remaining(tmp_path):
    from services.persistence import _atomic_write_json

    target = str(tmp_path / "test.json")
    _atomic_write_json(target, {"data": "test"})

    tmp_file = target + ".tmp"
    assert not os.path.exists(tmp_file)  # Temp-Datei wurde umbenannt/gelöscht
