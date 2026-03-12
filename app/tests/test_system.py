"""
Tests für api/routes_system.py

Getestet werden:
  POST /system/ack-restart – Neustart-Quittierung, Auth, Idempotenz
"""

import pytest

from core.state import state, state_lock


# ─────────────────────────────────────────────────────────────────────────────
# POST /system/ack-restart
# ─────────────────────────────────────────────────────────────────────────────

def test_ack_restart_returns_ok(client):
    """Basis-Response: 200 mit ok=True."""
    resp = client.post("/system/ack-restart")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_ack_restart_clears_unclean_flag(client):
    """ACK setzt unclean_restart auf False."""
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2024-06-01T08:00:00+02:00"

    resp = client.post("/system/ack-restart")
    assert resp.status_code == 200

    with state_lock:
        assert state.unclean_restart is False
        assert state.restart_detected_at == ""


def test_ack_restart_clears_restart_detected_at(client):
    """ACK setzt restart_detected_at auf leeren String zurück."""
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2025-01-15T06:00:00+01:00"

    client.post("/system/ack-restart")

    with state_lock:
        assert state.restart_detected_at == ""


def test_ack_restart_idempotent_when_flag_false(client):
    """Mehrfache Aufrufe wenn Flag bereits False: keine Fehler, ok=True."""
    with state_lock:
        state.unclean_restart = False
        state.restart_detected_at = ""

    resp1 = client.post("/system/ack-restart")
    resp2 = client.post("/system/ack-restart")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["ok"] is True
    assert resp2.json()["ok"] is True

    # State bleibt sauber
    with state_lock:
        assert state.unclean_restart is False
        assert state.restart_detected_at == ""


def test_ack_restart_idempotent_when_flag_true(client):
    """Mehrfache Aufrufe wenn Flag True: erster setzt zurück, zweiter ist harmlos."""
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2024-06-01T08:00:00+02:00"

    resp1 = client.post("/system/ack-restart")
    resp2 = client.post("/system/ack-restart")

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    with state_lock:
        assert state.unclean_restart is False
        assert state.restart_detected_at == ""


def test_ack_restart_requires_api_key(client):
    """Ohne API-Key: 401 Unauthorized."""
    resp = client.post("/system/ack-restart", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_ack_restart_wrong_api_key(client):
    """Mit falschem API-Key: 401 Unauthorized."""
    resp = client.post("/system/ack-restart", headers={"X-API-Key": "falscherschluessel"})
    assert resp.status_code == 401


def test_ack_restart_does_not_affect_other_state(client):
    """ACK darf nur unclean_restart und restart_detected_at ändern.

    hw_faulted, queue, active_runs etc. müssen unberührt bleiben.
    """
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2024-06-01T08:00:00+02:00"
        state.hw_faulted = True
        state.hw_fault_reason = "close_failed_max_retries"
        state.queue_state = "läuft"

    client.post("/system/ack-restart")

    with state_lock:
        # Nur Restart-Felder wurden geändert
        assert state.unclean_restart is False
        assert state.restart_detected_at == ""
        # Alles andere bleibt unverändert
        assert state.hw_faulted is True
        assert state.hw_fault_reason == "close_failed_max_retries"
        assert state.queue_state == "läuft"
