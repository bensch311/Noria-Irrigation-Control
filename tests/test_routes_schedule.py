"""
Tests für api/routes_schedule.py

Getestet werden:
  GET    /schedule
  POST   /schedule/add
  POST   /schedule/enable/{id}
  POST   /schedule/disable/{id}
  DELETE /schedule
"""

import pytest

from core.state import state, state_lock
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# GET /schedule
# ─────────────────────────────────────────────────────────────────────────────


def test_get_schedules_empty(client):
    resp = client.get("/schedule")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


def test_get_schedules_with_entries(client, make_schedule):
    with state_lock:
        state.schedules = [
            make_schedule(zone=1, duration_s=60),
            make_schedule(zone=2, duration_s=120),
        ]

    resp = client.get("/schedule")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["items"][0]["zone"] == 1
    assert data["items"][1]["zone"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# POST /schedule/add
# ─────────────────────────────────────────────────────────────────────────────


def test_schedule_add_basic(client):
    payload = {
        "zone": 1,
        "weekdays": [0, 1, 2, 3, 4],
        "start_times": ["06:00", "18:00"],
        "duration_s": 120,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "id" in data

    with state_lock:
        assert len(state.schedules) == 1
        rule = state.schedules[0]
        assert rule.zone == 1
        assert rule.weekdays == [0, 1, 2, 3, 4]
        assert rule.start_times == ["06:00", "18:00"]
        assert rule.duration_s == 120
        assert rule.repeat is True
        assert rule.enabled is True


def test_schedule_add_zone_all(client):
    """zone=0 bedeutet alle Ventile."""
    payload = {
        "zone": 0,
        "weekdays": [0],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 200


def test_schedule_add_repeat_false_sets_once_pending(client):
    payload = {
        "zone": 1,
        "weekdays": [0, 1],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": False,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 200

    with state_lock:
        rule = state.schedules[0]
        assert rule.repeat is False
        assert rule.once_pending is not None
        assert "0 06:00" in rule.once_pending
        assert "1 06:00" in rule.once_pending


def test_schedule_add_invalid_zone_negative(client):
    payload = {
        "zone": -1,
        "weekdays": [0],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code in (400, 422)


def test_schedule_add_zone_exceeds_max(client):
    with state_lock:
        state.max_valves = 3
    payload = {
        "zone": 4,
        "weekdays": [0],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_invalid_weekday(client):
    payload = {
        "zone": 1,
        "weekdays": [7],  # Ungültig: 0-6
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_invalid_time_format(client):
    payload = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["6:00"],  # Fehlendes führendes Null
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_invalid_time_hours(client):
    payload = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["25:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_invalid_time_minutes(client):
    payload = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["06:60"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_duration_exceeds_max(client):
    with state_lock:
        state.hard_max_runtime_s = 300
    payload = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["06:00"],
        "duration_s": 301,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 400


def test_schedule_add_empty_weekdays_rejected(client):
    """Pydantic-Validation: min_length=1 für weekdays."""
    payload = {
        "zone": 1,
        "weekdays": [],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    resp = client.post("/schedule/add", json=payload)
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# POST /schedule/enable & /schedule/disable
# ─────────────────────────────────────────────────────────────────────────────


def test_schedule_enable(client, make_schedule):
    rule = make_schedule(enabled=False, rule_id="test01")
    with state_lock:
        state.schedules = [rule]

    resp = client.post("/schedule/enable/test01")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    with state_lock:
        assert state.schedules[0].enabled is True


def test_schedule_enable_not_found(client):
    resp = client.post("/schedule/enable/nonexistent")
    assert resp.status_code == 404


def test_schedule_disable(client, make_schedule):
    rule = make_schedule(enabled=True, rule_id="test02")
    with state_lock:
        state.schedules = [rule]

    resp = client.post("/schedule/disable/test02")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    with state_lock:
        assert state.schedules[0].enabled is False


def test_schedule_disable_not_found(client):
    resp = client.post("/schedule/disable/nonexistent")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /schedule
# ─────────────────────────────────────────────────────────────────────────────


def test_schedule_delete_single(client, make_schedule):
    rule = make_schedule(rule_id="del01")
    with state_lock:
        state.schedules = [rule]

    resp = client.request("DELETE", "/schedule", json=["del01"])
    assert resp.status_code == 200
    assert "del01" in resp.json()["deleted"]

    with state_lock:
        assert state.schedules == []


def test_schedule_delete_multiple(client, make_schedule):
    rules = [make_schedule(rule_id=f"r{i}") for i in range(3)]
    with state_lock:
        state.schedules = rules

    resp = client.request("DELETE", "/schedule", json=["r0", "r2"])
    assert resp.status_code == 200

    with state_lock:
        assert len(state.schedules) == 1
        assert state.schedules[0].id == "r1"


def test_schedule_delete_not_found_returns_404(client):
    resp = client.request("DELETE", "/schedule", json=["notexist"])
    assert resp.status_code == 404


def test_schedule_delete_sets_dirty_flag(client, make_schedule):
    rule = make_schedule(rule_id="dirty01")
    with state_lock:
        state.schedules = [rule]
        state.schedules_dirty = False

    client.request("DELETE", "/schedule", json=["dirty01"])

    with state_lock:
        assert state.schedules_dirty is True
