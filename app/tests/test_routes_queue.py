"""
Tests für api/routes_queue.py

Getestet werden:
  GET  /queue
  POST /queue/add
  POST /queue/start
  POST /queue/pause
  POST /queue/clear
"""

import time
import pytest

from core.state import state, state_lock, ActiveRun, QueueItem
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# GET /queue
# ─────────────────────────────────────────────────────────────────────────────


def test_get_queue_empty(client):
    resp = client.get("/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_length"] == 0
    assert data["items"] == []
    assert data["queue_state"] == "bereit"


def test_get_queue_with_items(client):
    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=2, duration=120, time_unit="Sekunden", source="sensor"),
        ]

    resp = client.get("/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_length"] == 2
    assert data["items"][0]["zone"] == 1
    assert data["items"][1]["zone"] == 2
    # source muss im Response enthalten sein (wird in der Queue-Tabelle als
    # "Quelle"-Spalte angezeigt)
    assert data["items"][0]["source"] == "queue"
    assert data["items"][1]["source"] == "sensor"


def test_get_queue_source_field_present(client):
    """GET /queue liefert source-Feld fuer jedes Item."""
    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="manual"),
        ]
    data = client.get("/queue").json()
    assert "source" in data["items"][0]


# ─────────────────────────────────────────────────────────────────────────────
# POST /queue/add
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_add_success(client):
    resp = client.post("/queue/add", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["queue_length"] == 1

    with state_lock:
        assert len(state.queue) == 1
        assert state.queue[0].zone == 1
        assert state.queue_dirty is True


def test_queue_add_multiple_items(client):
    client.post("/queue/add", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    client.post("/queue/add", json={"zone": 2, "duration": 90, "time_unit": "Sekunden"})
    client.post("/queue/add", json={"zone": 3, "duration": 120, "time_unit": "Sekunden"})

    with state_lock:
        assert len(state.queue) == 3
        assert [i.zone for i in state.queue] == [1, 2, 3]


def test_queue_add_zone_zero_adds_all_valves(client):
    """zone=0 (Alle Zonen) fügt max_valves Items in die Queue ein."""
    with state_lock:
        state.max_valves = 3
    resp = client.post("/queue/add", json={"zone": 0, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["queue_length"] == 3
    assert data["zones_added"] == 3

    with state_lock:
        zones = [item.zone for item in state.queue]
    assert zones == [1, 2, 3]


def test_queue_add_zone_zero_correct_duration(client):
    """Alle Items von zone=0 haben dieselbe Dauer und Zeiteinheit."""
    with state_lock:
        state.max_valves = 2
    client.post("/queue/add", json={"zone": 0, "duration": 120, "time_unit": "Sekunden"})

    with state_lock:
        for item in state.queue:
            assert item.duration == 120
            assert item.time_unit == "Sekunden"


def test_queue_add_zone_minus1_rejected(client):
    """Negative zone-Werte werden von Pydantic (ge=0) mit 422 abgelehnt."""
    resp = client.post("/queue/add", json={"zone": -1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 422


def test_queue_add_zone_exceeds_max(client):
    with state_lock:
        state.max_valves = 3
    resp = client.post("/queue/add", json={"zone": 4, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_queue_add_duration_zero(client):
    resp = client.post("/queue/add", json={"zone": 1, "duration": 0, "time_unit": "Sekunden"})
    assert resp.status_code in (400, 422)


def test_queue_add_duration_exceeds_max(client):
    with state_lock:
        state.hard_max_runtime_s = 300
    resp = client.post("/queue/add", json={"zone": 1, "duration": 301, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_queue_add_resets_state_from_fertig(client):
    with state_lock:
        state.queue_state = "fertig"

    client.post("/queue/add", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})

    with state_lock:
        assert state.queue_state == "bereit"


# ─────────────────────────────────────────────────────────────────────────────
# POST /queue/start
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_start_empty_returns_400(client):
    resp = client.post("/queue/start")
    assert resp.status_code == 400


def test_queue_start_serial_mode_starts_first_item(client, mock_io):
    with state_lock:
        state.parallel_enabled = False
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
        ]

    resp = client.post("/queue/start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["started_count"] == 1

    with state_lock:
        assert 1 in state.active_runs        # Zone 1 läuft
        assert len(state.queue) == 1         # Zone 2 noch in Queue
        assert state.queue[0].zone == 2


def test_queue_start_parallel_mode_starts_multiple(client, mock_io):
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
        ]

    resp = client.post("/queue/start")
    assert resp.status_code == 200
    assert resp.json()["started_count"] == 2

    with state_lock:
        assert len(state.active_runs) == 2   # 2 parallel gestartet
        assert len(state.queue) == 1         # 1 noch in Queue


def test_queue_start_sets_state_lauft(client, mock_io):
    with state_lock:
        state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]

    client.post("/queue/start")
    with state_lock:
        assert state.queue_state == "läuft"


def test_queue_start_hw_faulted_starts_nothing(client):
    with state_lock:
        state.hw_faulted = True
        state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]

    resp = client.post("/queue/start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["started_count"] == 0

    with state_lock:
        assert len(state.active_runs) == 0
        assert len(state.queue) == 1


# ─────────────────────────────────────────────────────────────────────────────
# POST /queue/pause
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_pause(client):
    resp = client.post("/queue/pause")
    assert resp.status_code == 200
    with state_lock:
        assert state.queue_state == "pausiert"


# ─────────────────────────────────────────────────────────────────────────────
# POST /queue/clear
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_clear(client):
    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
        ]
        state.queue_state = "läuft"

    resp = client.post("/queue/clear")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["queue_length"] == 0

    with state_lock:
        assert state.queue == []
        assert state.queue_state == "bereit"
        assert state.queue_dirty is True


# ─────────────────────────────────────────────────────────────────────────────
# POST /queue/add – Kapazitätslimit (MAX_QUEUE_ITEMS)
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_add_at_capacity_returns_400(client):
    """
    Wenn die Queue bereits MAX_QUEUE_ITEMS Einträge hat, muss POST /queue/add
    mit 400 abgelehnt werden.

    DoS-Schutz: unbegrenzte Queue würde bei gezielten Requests Arbeitsspeicher
    und Persistenz-I/O belasten.
    """
    from core.config import MAX_QUEUE_ITEMS

    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")
            for _ in range(MAX_QUEUE_ITEMS)
        ]

    resp = client.post("/queue/add", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400
    assert "voll" in resp.json()["detail"].lower() or str(MAX_QUEUE_ITEMS) in resp.json()["detail"]


def test_queue_add_zone0_at_capacity_returns_400(client):
    """
    zone=0 würde max_valves Items hinzufügen. Wenn Queue + max_valves > MAX_QUEUE_ITEMS,
    muss 400 zurückgegeben werden.
    """
    from core.config import MAX_QUEUE_ITEMS

    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))
        # Queue so befüllen, dass zone=0 das Limit überschreiten würde
        fill_count = MAX_QUEUE_ITEMS - max_v + 1  # ein Item zu viel
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")
            for _ in range(fill_count)
        ]

    resp = client.post("/queue/add", json={"zone": 0, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_queue_add_just_below_capacity_succeeds(client):
    """
    Queue mit MAX_QUEUE_ITEMS - 1 Einträgen → ein weiterer Eintrag darf hinzugefügt werden.
    """
    from core.config import MAX_QUEUE_ITEMS

    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")
            for _ in range(MAX_QUEUE_ITEMS - 1)
        ]

    resp = client.post("/queue/add", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
