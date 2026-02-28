# app/tests/test_routes_settings.py
"""
Tests fuer api/routes_settings.py

GET  /settings : liefert max_history_items + max_valves
POST /settings : validiert + setzt State + persistiert

Pydantic-Validierung (→ 422):
  max_history_items=0, -1  (ge=1)
  max_history_items=501    (le=500)
  fehlendes Feld, falscher Typ
"""

import pytest
from unittest.mock import patch

from core.state import state, state_lock
from core.config import MAX_HISTORY_ITEMS


# ─────────────────────────────────────────────────────────────────────────────
# GET /settings
# ─────────────────────────────────────────────────────────────────────────────

def test_get_settings_returns_expected_keys(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_history_items" in data
    assert "max_valves" in data


def test_get_settings_types(client):
    resp = client.get("/settings")
    data = resp.json()
    assert isinstance(data["max_history_items"], int)
    assert isinstance(data["max_valves"], int)


def test_get_settings_reflects_state(client):
    with state_lock:
        state.max_history_items = 42
    resp = client.get("/settings")
    assert resp.json()["max_history_items"] == 42


def test_get_settings_max_valves_from_state(client):
    with state_lock:
        state.max_valves = 8
    resp = client.get("/settings")
    assert resp.json()["max_valves"] == 8


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_updates_state(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json={"max_history_items": 50})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["max_history_items"] == 50
    with state_lock:
        assert state.max_history_items == 50


def test_post_settings_calls_persist(client):
    with patch("api.routes_settings.save_user_settings_to_disk") as mock_save:
        client.post("/settings", json={"max_history_items": 30})
    mock_save.assert_called_once()


def test_post_settings_roundtrip(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        client.post("/settings", json={"max_history_items": 77})
    resp = client.get("/settings")
    assert resp.json()["max_history_items"] == 77


# ─────────────────────────────────────────────────────────────────────────────
# Validierung → 422
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_zero_rejected(client):
    resp = client.post("/settings", json={"max_history_items": 0})
    assert resp.status_code == 422

def test_post_settings_negative_rejected(client):
    resp = client.post("/settings", json={"max_history_items": -1})
    assert resp.status_code == 422

def test_post_settings_over_cap_rejected(client):
    resp = client.post("/settings", json={"max_history_items": 501})
    assert resp.status_code == 422

def test_post_settings_cap_boundary_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json={"max_history_items": 500})
    assert resp.status_code == 200

def test_post_settings_min_boundary_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json={"max_history_items": 1})
    assert resp.status_code == 200

def test_post_settings_missing_field_rejected(client):
    resp = client.post("/settings", json={})
    assert resp.status_code == 422

def test_post_settings_wrong_type_rejected(client):
    resp = client.post("/settings", json={"max_history_items": "viele"})
    assert resp.status_code == 422
