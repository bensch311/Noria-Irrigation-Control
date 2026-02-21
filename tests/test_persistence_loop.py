"""
Tests fuer den persistence_loop in services/persistence.py

persistence_loop laeuft alle 2s und schreibt dirty-State-Felder auf Disk.
Aktuell nicht getestet (Zeilen 375-399 ungecovered).

Getestet werden:
  - Speichern bei schedules_dirty=True
  - Speichern bei queue_dirty=True
  - Speichern bei history_dirty=True
  - Nichts gespeichert wenn alle Flags False
  - Kombination mehrerer dirty Flags
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from core.state import state, state_lock


def _run_persistence_once():
    """Fuehrt exakt einen Durchlauf des persistence_loop aus."""
    from core.state import shutdown_event
    from services.persistence import persistence_loop

    call_count = 0

    def mock_is_set():
        nonlocal call_count
        call_count += 1
        return call_count > 1

    with (
        patch.object(shutdown_event, "is_set", side_effect=mock_is_set),
        patch.object(shutdown_event, "wait", return_value=False),
    ):
        persistence_loop()


class TestPersistenceLoop:
    def test_saves_schedules_when_dirty(self, tmp_path, monkeypatch):
        import services.persistence as pers
        monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.schedules = []
            state.schedules_dirty = True
            state.queue_dirty = False
            state.history_dirty = False

        _run_persistence_once()

        assert os.path.exists(str(tmp_path / "schedules.json"))
        with state_lock:
            assert state.schedules_dirty is False

    def test_saves_queue_when_dirty(self, tmp_path, monkeypatch):
        import services.persistence as pers
        monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.queue = []
            state.schedules_dirty = False
            state.queue_dirty = True
            state.history_dirty = False

        _run_persistence_once()

        assert os.path.exists(str(tmp_path / "queue.json"))
        with state_lock:
            assert state.queue_dirty is False

    def test_saves_history_when_dirty(self, tmp_path, monkeypatch):
        import services.persistence as pers
        monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.run_history = []
            state.schedules_dirty = False
            state.queue_dirty = False
            state.history_dirty = True

        _run_persistence_once()

        assert os.path.exists(str(tmp_path / "history.json"))
        with state_lock:
            assert state.history_dirty is False

    def test_does_not_save_when_all_clean(self, tmp_path, monkeypatch):
        import services.persistence as pers
        monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.schedules_dirty = False
            state.queue_dirty = False
            state.history_dirty = False

        _run_persistence_once()

        assert not os.path.exists(str(tmp_path / "schedules.json"))
        assert not os.path.exists(str(tmp_path / "queue.json"))
        assert not os.path.exists(str(tmp_path / "history.json"))

    def test_saves_all_when_all_dirty(self, tmp_path, monkeypatch, make_schedule):
        import services.persistence as pers
        from core.state import QueueItem, HistoryItem
        monkeypatch.setattr(pers, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.schedules = [make_schedule(zone=1)]
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
            state.run_history = [
                HistoryItem(ts_end="2025-01-01T06:00:00+01:00", zone=1, duration_s=60,
                            source="manual", time_unit="Sekunden")
            ]
            state.schedules_dirty = True
            state.queue_dirty = True
            state.history_dirty = True

        _run_persistence_once()

        assert os.path.exists(str(tmp_path / "schedules.json"))
        assert os.path.exists(str(tmp_path / "queue.json"))
        assert os.path.exists(str(tmp_path / "history.json"))

        with state_lock:
            assert state.schedules_dirty is False
            assert state.queue_dirty is False
            assert state.history_dirty is False

    def test_schedules_content_is_valid_json(self, tmp_path, monkeypatch, make_schedule):
        import services.persistence as pers
        sched_path = str(tmp_path / "schedules.json")
        monkeypatch.setattr(pers, "SCHEDULES_FILE", sched_path)
        monkeypatch.setattr(pers, "QUEUE_FILE", str(tmp_path / "queue.json"))
        monkeypatch.setattr(pers, "HISTORY_FILE", str(tmp_path / "history.json"))

        with state_lock:
            state.schedules = [make_schedule(zone=2, rule_id="test99")]
            state.schedules_dirty = True
            state.queue_dirty = False
            state.history_dirty = False

        _run_persistence_once()

        with open(sched_path) as f:
            data = json.load(f)
        assert data["items"][0]["id"] == "test99"
        assert data["items"][0]["zone"] == 2
