"""
Tests für api/routes_history.py

Getestet werden:
  GET /history
"""

from core.state import state, state_lock, HistoryItem


def test_history_empty(client):
    resp = client.get("/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


def test_history_with_entries(client):
    with state_lock:
        state.run_history = [
            HistoryItem(
                ts_end="2025-01-01T06:01:00+01:00",
                zone=1,
                duration_s=60,
                source="manual",
                time_unit="Sekunden",
            ),
            HistoryItem(
                ts_end="2025-01-01T07:00:00+01:00",
                zone=2,
                duration_s=120,
                source="schedule",
                time_unit="Sekunden",
            ),
        ]

    resp = client.get("/history")
    assert resp.status_code == 200
    data = resp.json()

    assert data["count"] == 2
    assert data["items"][0]["zone"] == 1
    assert data["items"][0]["duration_s"] == 60
    assert data["items"][0]["source"] == "manual"
    assert data["items"][1]["zone"] == 2
    assert data["items"][1]["source"] == "schedule"


def test_history_entry_has_all_fields(client):
    with state_lock:
        state.run_history = [
            HistoryItem(
                ts_end="2025-06-15T08:00:00+02:00",
                zone=3,
                duration_s=45,
                source="queue",
                time_unit="Sekunden",
            )
        ]

    resp = client.get("/history")
    item = resp.json()["items"][0]

    assert "ts_end" in item
    assert "zone" in item
    assert "duration_s" in item
    assert "source" in item
    assert "time_unit" in item
    assert item["zone"] == 3
    assert item["duration_s"] == 45
    assert item["source"] == "queue"
