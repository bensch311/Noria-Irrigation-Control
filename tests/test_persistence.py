"""
Tests für services/persistence.py

Getestet werden:
  - save/load schedules      (Roundtrip, fehlende Datei, corrupt JSON,
                               automation_block_run_key-Setzung)
  - save/load queue          (Roundtrip, fehlende Datei, corrupt JSON)
  - save/load history        (Roundtrip, Limit, fehlende Datei, corrupt JSON)
  - load_device_config       (valide, fehlende, korrupte Datei, Normalisierung)
  - load_user_settings       (valide, fehlende, korrupte Datei, Roundtrip)
  - load_runtime_state       (valide, fehlende, korrupte Datei, Clamping, Roundtrip)
  - _atomic_write_json       (atomares Schreiben)
  - _backup_corrupt_file     (erstellt .corrupt-Datei)
  - Deserializer-Defaults    (fehlende optionale Felder)
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
        assert state.schedules == []


def test_save_schedules_sets_not_dirty(tmp_path, monkeypatch, make_schedule):
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    with state_lock:
        state.schedules = [make_schedule(zone=1)]
        state.schedules_dirty = True

    pers.save_schedules_to_disk()

    with state_lock:
        assert state.schedules_dirty is False


def test_load_schedules_sets_automation_block_run_key_when_enabled(tmp_path, monkeypatch, make_schedule):
    """
    Wenn automation_enabled=True beim Laden, muss automation_block_run_key
    auf die aktuelle Minute gesetzt werden, damit keine doppelten Starts
    nach einem Neustart passieren (Crash-Safety).
    """
    import services.persistence as pers
    from datetime import datetime
    from core.config import TZ

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    with state_lock:
        state.schedules = [make_schedule(zone=1)]
        state.automation_enabled = True

    pers.save_schedules_to_disk()

    with state_lock:
        state.automation_block_run_key = None
        state.automation_enabled = False

    pers.load_schedules_from_disk()

    with state_lock:
        assert state.automation_block_run_key is not None
        # Format muss "YYYY-MM-DD HH:MM" sein
        assert len(state.automation_block_run_key) == 16

def test_load_schedules_block_run_key_none_when_disabled(tmp_path, monkeypatch, make_schedule):
    """
    Wenn automation_enabled=False beim Laden, bleibt automation_block_run_key=None.
    """
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    with state_lock:
        state.schedules = [make_schedule(zone=1)]
        state.automation_enabled = False

    pers.save_schedules_to_disk()

    with state_lock:
        state.automation_block_run_key = "should_be_cleared"

    pers.load_schedules_from_disk()

    with state_lock:
        assert state.automation_block_run_key is None


def test_schedule_roundtrip_preserves_once_pending(tmp_path, monkeypatch, make_schedule):
    """once_pending wird korrekt gespeichert und geladen."""
    import services.persistence as pers

    monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))

    rule = make_schedule(zone=1, weekdays=[0, 2], start_times=["06:00", "18:00"], repeat=False)

    with state_lock:
        state.schedules = [rule]
        state.automation_enabled = False

    pers.save_schedules_to_disk()

    with state_lock:
        state.schedules = []

    pers.load_schedules_from_disk()

    with state_lock:
        loaded = state.schedules[0]
        assert loaded.repeat is False
        assert loaded.once_pending is not None
        assert "0 06:00" in loaded.once_pending


def test_deserialize_schedule_defaults_for_optional_fields():
    """
    _deserialize_schedule muss auch mit minimalen Dicts umgehen können
    (ältere Datenbankversionen ohne alle Felder).
    """
    from services.persistence import _deserialize_schedule

    minimal = {
        "id": "abc123",
        "zone": 2,
    }
    rule = _deserialize_schedule(minimal)
    assert rule.id == "abc123"
    assert rule.zone == 2
    assert rule.weekdays == []
    assert rule.start_times == []
    assert rule.duration_s == 0
    assert rule.repeat is True
    assert rule.enabled is True
    assert rule.last_run_on is None
    assert rule.once_pending is None


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
        state.queue_state = "läuft"

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


def test_deserialize_queue_item_source_default():
    """_deserialize_queue_item setzt source='queue' wenn nicht vorhanden."""
    from services.persistence import _deserialize_queue_item

    item = _deserialize_queue_item({"zone": 1, "duration": 30})
    assert item.source == "queue"
    assert item.time_unit == "Minuten"


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_history_roundtrip(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

    with state_lock:
        state.run_history = [
            HistoryItem(ts_end="2025-01-01T06:01:00+01:00", zone=1, duration_s=60, source="manual"),
            HistoryItem(ts_end="2025-01-01T07:02:00+01:00", zone=2, duration_s=120, source="schedule"),
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
            HistoryItem(ts_end=f"2025-01-01T0{i}:00:00+01:00", zone=1, duration_s=60, source="manual")
            for i in range(5)
        ]
        state.max_history_items = 3

    pers.save_history_to_disk()

    with state_lock:
        state.run_history = []

    pers.load_history_from_disk()

    with state_lock:
        assert len(state.run_history) == 3


def test_load_history_missing_file(tmp_path, monkeypatch):
    import services.persistence as pers

    monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "nope.json"))

    pers.load_history_from_disk()  # Darf keinen Fehler werfen

    with state_lock:
        assert state.run_history == []


def test_deserialize_history_item_defaults():
    """_deserialize_history_item liefert sinnvolle Defaults für fehlende Felder."""
    from services.persistence import _deserialize_history_item

    item = _deserialize_history_item({})
    assert item.ts_end == ""
    assert item.zone == 0
    assert item.duration_s == 0
    assert item.source == "manual"
    assert item.time_unit == "Sekunden"


# ─────────────────────────────────────────────────────────────────────────────
# device_config
# ─────────────────────────────────────────────────────────────────────────────


def test_load_device_config_valid(tmp_path, monkeypatch):
    import services.persistence as pers

    cfg_file = tmp_path / "device_config.json"
    cfg_file.write_text(json.dumps({
        "version": 1,
        "device": {
            "MAX_VALVES": 4,
            "IRRIGATION_VALVE_DRIVER": "sim",
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},
        },
        "hard_limits": {
            "MAX_RUNTIME_S": 3600,
            "MAX_CONCURRENT_VALVES": 2,
        },
    }), encoding="utf-8")

    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    with state_lock:
        assert state.max_valves == 4
        assert state.valve_driver_mode == "sim"


def test_load_device_config_missing_creates_template(tmp_path, monkeypatch):
    import services.persistence as pers

    cfg_file = tmp_path / "device_config.json"
    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    # Template muss erstellt worden sein
    assert cfg_file.exists()
    payload = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "device" in payload


def test_load_device_config_corrupt_uses_defaults(tmp_path, monkeypatch):
    import services.persistence as pers
    from core.config import MAX_VALVES

    cfg_file = tmp_path / "device_config.json"
    cfg_file.write_text("{ KAPUTT JSON !!!", encoding="utf-8")

    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    with state_lock:
        # Defaults müssen verwendet werden
        assert state.max_valves == MAX_VALVES
        assert state.valve_driver_mode == "sim"


def test_load_device_config_corrupt_creates_backup(tmp_path, monkeypatch):
    """Korrupte Datei muss als .corrupt-* umbenannt werden."""
    import services.persistence as pers

    cfg_file = tmp_path / "device_config.json"
    cfg_file.write_text("{ KAPUTT JSON !!!", encoding="utf-8")

    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    # Originaldatei darf nicht mehr existieren (umbenannt)
    corrupt_files = list(tmp_path.glob("device_config.json.corrupt-*"))
    assert len(corrupt_files) == 1


def test_load_device_config_invalid_driver_normalized_to_sim(tmp_path, monkeypatch):
    """Unbekannter Treiber-Name muss auf 'sim' normalisiert werden."""
    import services.persistence as pers

    cfg_file = tmp_path / "device_config.json"
    cfg_file.write_text(json.dumps({
        "version": 1,
        "device": {
            "MAX_VALVES": 2,
            "IRRIGATION_VALVE_DRIVER": "unknown_driver_xyz",
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},
        },
        "hard_limits": {"MAX_RUNTIME_S": 3600, "MAX_CONCURRENT_VALVES": 2},
    }), encoding="utf-8")

    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    with state_lock:
        assert state.valve_driver_mode == "sim"


def test_load_device_config_hard_concurrent_clamped_to_max_valves(tmp_path, monkeypatch):
    """
    MAX_CONCURRENT_VALVES darf MAX_VALVES nicht überschreiten.
    """
    import services.persistence as pers

    cfg_file = tmp_path / "device_config.json"
    cfg_file.write_text(json.dumps({
        "version": 1,
        "device": {
            "MAX_VALVES": 2,
            "IRRIGATION_VALVE_DRIVER": "sim",
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},
        },
        "hard_limits": {
            "MAX_RUNTIME_S": 3600,
            "MAX_CONCURRENT_VALVES": 10,  # höher als MAX_VALVES=2
        },
    }), encoding="utf-8")

    monkeypatch.setattr(pers, "DEVICE_CONFIG_FILE", str(cfg_file))

    pers.load_device_config_from_disk()

    with state_lock:
        # Muss auf max_valves=2 geclampt werden
        assert state.hard_max_concurrent_valves <= 2


# ─────────────────────────────────────────────────────────────────────────────
# user_settings
# ─────────────────────────────────────────────────────────────────────────────


def test_load_user_settings_valid(tmp_path, monkeypatch):
    import services.persistence as pers

    settings_file = tmp_path / "user_settings.json"
    settings_file.write_text(json.dumps({
        "version": 1,
        "user": {"MAX_HISTORY_ITEMS": 50},
    }), encoding="utf-8")

    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))

    pers.load_user_settings_from_disk()

    with state_lock:
        assert state.max_history_items == 50


def test_load_user_settings_missing_creates_defaults(tmp_path, monkeypatch):
    """Fehlende user_settings.json → Defaults laden und Template schreiben."""
    import services.persistence as pers
    from core.config import MAX_HISTORY_ITEMS

    settings_file = tmp_path / "user_settings.json"
    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))

    pers.load_user_settings_from_disk()

    # Template muss erstellt worden sein
    assert settings_file.exists()
    with state_lock:
        assert state.max_history_items == MAX_HISTORY_ITEMS


def test_load_user_settings_corrupt_uses_defaults(tmp_path, monkeypatch):
    """Korrupte user_settings.json → Defaults, Backup."""
    import services.persistence as pers
    from core.config import MAX_HISTORY_ITEMS

    settings_file = tmp_path / "user_settings.json"
    settings_file.write_text("{ KAPUTT !!!", encoding="utf-8")

    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))

    pers.load_user_settings_from_disk()

    with state_lock:
        assert state.max_history_items == MAX_HISTORY_ITEMS

    # Backup muss erstellt worden sein
    corrupt_files = list(tmp_path.glob("user_settings.json.corrupt-*"))
    assert len(corrupt_files) == 1


def test_save_load_user_settings_roundtrip(tmp_path, monkeypatch):
    """save_user_settings_to_disk + load_user_settings_from_disk Roundtrip."""
    import services.persistence as pers

    settings_file = tmp_path / "user_settings.json"
    monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))

    with state_lock:
        state.max_history_items = 42

    pers.save_user_settings_to_disk()

    with state_lock:
        state.max_history_items = 99  # Anderen Wert setzen

    pers.load_user_settings_from_disk()

    with state_lock:
        assert state.max_history_items == 42


# ─────────────────────────────────────────────────────────────────────────────
# runtime_state
# ─────────────────────────────────────────────────────────────────────────────


def test_save_load_runtime_state_roundtrip(tmp_path, monkeypatch):
    import services.persistence as pers

    rt_file = tmp_path / "runtime_state.json"
    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", str(rt_file))

    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        state.max_valves = 4
        state.hard_max_concurrent_valves = 4

    pers.save_runtime_state_to_disk()

    with state_lock:
        state.parallel_enabled = False
        state.max_concurrent_valves = 1

    pers.load_runtime_state_from_disk()

    with state_lock:
        assert state.parallel_enabled is True
        assert state.max_concurrent_valves == 2


def test_load_runtime_state_clamped_to_hard_limit(tmp_path, monkeypatch):
    """max_concurrent_valves wird auf hard_max_concurrent_valves geclampt."""
    import services.persistence as pers

    rt_file = tmp_path / "runtime_state.json"
    rt_file.write_text(json.dumps({
        "version": 1,
        "runtime": {
            "parallel_enabled": True,
            "max_concurrent_valves": 99,  # Weit über Limit
        },
    }), encoding="utf-8")

    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", str(rt_file))

    with state_lock:
        state.max_valves = 3
        state.hard_max_concurrent_valves = 3

    pers.load_runtime_state_from_disk()

    with state_lock:
        assert state.max_concurrent_valves <= 3


def test_load_runtime_state_missing_creates_defaults(tmp_path, monkeypatch):
    """Fehlende runtime_state.json → Defaults laden und Template schreiben."""
    import services.persistence as pers
    from core.config import DEFAULT_PARALLEL_ENABLED

    rt_file = tmp_path / "runtime_state.json"
    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", str(rt_file))

    pers.load_runtime_state_from_disk()

    # Template muss erstellt worden sein
    assert rt_file.exists()
    with state_lock:
        assert state.parallel_enabled == DEFAULT_PARALLEL_ENABLED


def test_load_runtime_state_corrupt_uses_defaults(tmp_path, monkeypatch):
    """Korrupte runtime_state.json → Defaults, Backup."""
    import services.persistence as pers
    from core.config import DEFAULT_PARALLEL_ENABLED

    rt_file = tmp_path / "runtime_state.json"
    rt_file.write_text("{ KAPUTT !!!", encoding="utf-8")

    monkeypatch.setattr(pers, "RUNTIME_STATE_FILE", str(rt_file))

    pers.load_runtime_state_from_disk()

    with state_lock:
        assert state.parallel_enabled == DEFAULT_PARALLEL_ENABLED

    corrupt_files = list(tmp_path.glob("runtime_state.json.corrupt-*"))
    assert len(corrupt_files) == 1


# ─────────────────────────────────────────────────────────────────────────────
# _atomic_write_json
# ─────────────────────────────────────────────────────────────────────────────


def test_atomic_write_creates_file(tmp_path):
    from services.persistence import _atomic_write_json

    target = tmp_path / "test.json"
    _atomic_write_json(str(target), {"foo": "bar"})

    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["foo"] == "bar"


def test_atomic_write_no_temp_file_remaining(tmp_path):
    """Nach dem Schreiben darf keine .tmp-Datei übrig bleiben."""
    from services.persistence import _atomic_write_json

    target = tmp_path / "test.json"
    _atomic_write_json(str(target), {"key": "value"})

    tmp_file = tmp_path / "test.json.tmp"
    assert not tmp_file.exists()


# ─────────────────────────────────────────────────────────────────────────────
# _backup_corrupt_file
# ─────────────────────────────────────────────────────────────────────────────


def test_backup_corrupt_file_renames_file(tmp_path):
    """_backup_corrupt_file benennt die Datei in .corrupt-<ts> um."""
    from services.persistence import _backup_corrupt_file

    target = tmp_path / "broken.json"
    target.write_text("kaputt", encoding="utf-8")

    _backup_corrupt_file(str(target))

    # Original darf nicht mehr existieren
    assert not target.exists()

    # Backup muss existieren
    backups = list(tmp_path.glob("broken.json.corrupt-*"))
    assert len(backups) == 1


def test_backup_corrupt_file_nonexistent_does_not_raise(tmp_path):
    """_backup_corrupt_file mit nicht-existenter Datei darf nicht crashen (best effort)."""
    from services.persistence import _backup_corrupt_file

    _backup_corrupt_file(str(tmp_path / "nonexistent.json"))  # Darf nicht werfen
