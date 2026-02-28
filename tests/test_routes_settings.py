# app/tests/test_routes_settings.py
"""
Tests fuer api/routes_settings.py

GET  /settings : liefert alle User-Settings + max_valves (readonly)
POST /settings : validiert + setzt State + persistiert sofort

Neue Felder (zusaetzlich zu max_history_items):
  navbar_title       : 1–50 Zeichen, darf nicht leer sein
  accent_color       : 6-stelliger Hex-Farbwert (#rrggbb)
  default_duration   : int 1–120
  default_time_unit  : "Sekunden" | "Minuten"
"""

import pytest
from unittest.mock import patch

from core.state import state, state_lock
from core.config import (
    MAX_HISTORY_ITEMS, NAVBAR_TITLE, ACCENT_COLOR,
    DEFAULT_DURATION, DEFAULT_TIME_UNIT,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helfer
# ─────────────────────────────────────────────────────────────────────────────

def _full_payload(**overrides) -> dict:
    """Gueltiges POST /settings Payload mit optionalen Overrides."""
    base = {
        "max_history_items": 20,
        "navbar_title":      "Bewaesserungscomputer",
        "accent_color":      "#82372a",
        "default_duration":  5,
        "default_time_unit": "Minuten",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# GET /settings
# ─────────────────────────────────────────────────────────────────────────────

def test_get_settings_returns_all_keys(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("max_history_items", "navbar_title", "accent_color",
                "default_duration", "default_time_unit", "max_valves"):
        assert key in data, f"Key fehlt: {key}"


def test_get_settings_types(client):
    resp = client.get("/settings")
    data = resp.json()
    assert isinstance(data["max_history_items"], int)
    assert isinstance(data["navbar_title"], str)
    assert isinstance(data["accent_color"], str)
    assert isinstance(data["default_duration"], int)
    assert isinstance(data["default_time_unit"], str)
    assert isinstance(data["max_valves"], int)


def test_get_settings_reflects_state(client):
    with state_lock:
        state.max_history_items = 42
        state.navbar_title      = "Hof Muster"
        state.accent_color      = "#123456"
        state.default_duration  = 15
        state.default_time_unit = "Sekunden"
    resp = client.get("/settings")
    data = resp.json()
    assert data["max_history_items"] == 42
    assert data["navbar_title"]      == "Hof Muster"
    assert data["accent_color"]      == "#123456"
    assert data["default_duration"]  == 15
    assert data["default_time_unit"] == "Sekunden"


def test_get_settings_max_valves_readonly(client):
    with state_lock:
        state.max_valves = 8
    assert client.get("/settings").json()["max_valves"] == 8


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – Erfolg
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_updates_all_state_fields(client):
    payload = _full_payload(
        max_history_items=50,
        navbar_title="Test Anlage",
        accent_color="#aabbcc",
        default_duration=10,
        default_time_unit="Sekunden",
    )
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"]                is True
    assert data["max_history_items"] == 50
    assert data["navbar_title"]      == "Test Anlage"
    assert data["accent_color"]      == "#aabbcc"
    assert data["default_duration"]  == 10
    assert data["default_time_unit"] == "Sekunden"
    with state_lock:
        assert state.max_history_items == 50
        assert state.navbar_title      == "Test Anlage"
        assert state.accent_color      == "#aabbcc"
        assert state.default_duration  == 10
        assert state.default_time_unit == "Sekunden"


def test_post_settings_calls_persist(client):
    with patch("api.routes_settings.save_user_settings_to_disk") as mock_save:
        client.post("/settings", json=_full_payload())
    mock_save.assert_called_once()


def test_post_settings_roundtrip(client):
    payload = _full_payload(navbar_title="Anlage Nord", accent_color="#001122")
    with patch("api.routes_settings.save_user_settings_to_disk"):
        client.post("/settings", json=payload)
    resp = client.get("/settings")
    assert resp.json()["navbar_title"] == "Anlage Nord"
    assert resp.json()["accent_color"] == "#001122"


def test_post_settings_only_max_history_required(client):
    """Nur max_history_items ist required; andere Felder haben Defaults."""
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json={"max_history_items": 30})
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – max_history_items Validierung
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_zero_rejected(client):
    resp = client.post("/settings", json=_full_payload(max_history_items=0))
    assert resp.status_code == 422

def test_post_settings_negative_rejected(client):
    resp = client.post("/settings", json=_full_payload(max_history_items=-1))
    assert resp.status_code == 422

def test_post_settings_over_cap_rejected(client):
    resp = client.post("/settings", json=_full_payload(max_history_items=501))
    assert resp.status_code == 422

def test_post_settings_cap_boundary_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        assert client.post("/settings", json=_full_payload(max_history_items=500)).status_code == 200

def test_post_settings_min_boundary_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        assert client.post("/settings", json=_full_payload(max_history_items=1)).status_code == 200

def test_post_settings_missing_field_rejected(client):
    """max_history_items fehlt → 422."""
    resp = client.post("/settings", json={})
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – navbar_title Validierung
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_empty_title_rejected(client):
    resp = client.post("/settings", json=_full_payload(navbar_title=""))
    assert resp.status_code == 422

def test_post_settings_whitespace_only_title_rejected(client):
    resp = client.post("/settings", json=_full_payload(navbar_title="   "))
    assert resp.status_code == 422

def test_post_settings_too_long_title_rejected(client):
    resp = client.post("/settings", json=_full_payload(navbar_title="x" * 51))
    assert resp.status_code == 422

def test_post_settings_max_length_title_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json=_full_payload(navbar_title="x" * 50))
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – accent_color Validierung
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_invalid_color_rejected(client):
    resp = client.post("/settings", json=_full_payload(accent_color="rot"))
    assert resp.status_code == 422

def test_post_settings_short_color_rejected(client):
    resp = client.post("/settings", json=_full_payload(accent_color="#fff"))
    assert resp.status_code == 422

def test_post_settings_no_hash_color_rejected(client):
    resp = client.post("/settings", json=_full_payload(accent_color="82372a"))
    assert resp.status_code == 422

def test_post_settings_uppercase_color_accepted(client):
    """Uppercase Hex ist gueltig (wird zu lowercase normalisiert)."""
    with patch("api.routes_settings.save_user_settings_to_disk"):
        resp = client.post("/settings", json=_full_payload(accent_color="#AABBCC"))
    assert resp.status_code == 200
    with state_lock:
        assert state.accent_color == "#aabbcc"  # normalisiert

def test_post_settings_valid_color_boundary(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        assert client.post("/settings", json=_full_payload(accent_color="#000000")).status_code == 200
        assert client.post("/settings", json=_full_payload(accent_color="#ffffff")).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – default_duration Validierung
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_duration_zero_rejected(client):
    resp = client.post("/settings", json=_full_payload(default_duration=0))
    assert resp.status_code == 422

def test_post_settings_duration_over_cap_rejected(client):
    resp = client.post("/settings", json=_full_payload(default_duration=121))
    assert resp.status_code == 422

def test_post_settings_duration_boundary_ok(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        assert client.post("/settings", json=_full_payload(default_duration=1)).status_code == 200
        assert client.post("/settings", json=_full_payload(default_duration=120)).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /settings – default_time_unit Validierung
# ─────────────────────────────────────────────────────────────────────────────

def test_post_settings_invalid_time_unit_rejected(client):
    resp = client.post("/settings", json=_full_payload(default_time_unit="hours"))
    assert resp.status_code == 422

def test_post_settings_valid_time_units_accepted(client):
    with patch("api.routes_settings.save_user_settings_to_disk"):
        assert client.post("/settings", json=_full_payload(default_time_unit="Sekunden")).status_code == 200
        assert client.post("/settings", json=_full_payload(default_time_unit="Minuten")).status_code == 200
