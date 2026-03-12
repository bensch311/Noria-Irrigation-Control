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


# ─────────────────────────────────────────────────────────────────────────────
# GET /system/logs/download
# ─────────────────────────────────────────────────────────────────────────────

import io
import zipfile
from pathlib import Path
from unittest.mock import patch


def test_download_logs_returns_zip(client, tmp_path):
    """Endpunkt liefert eine gültige ZIP-Datei."""
    # Temporäre Log-Datei anlegen
    log_file = tmp_path / "irrigation.jsonl"
    log_file.write_text('{"event": "test"}\n', encoding="utf-8")

    with patch("api.routes_system.Path") as mock_path_cls:
        # LOG_DIR → tmp_path simulieren
        mock_log_dir = mock_path_cls.return_value
        mock_log_dir.__truediv__ = lambda self, name: tmp_path / name

        # Kandidaten direkt patchen: nur die aktuelle Datei ist vorhanden
        with patch("api.routes_system.LOG_DIR", str(tmp_path)):
            resp = client.get("/system/logs/download")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    assert "noria-logs-" in resp.headers["content-disposition"]


def test_download_logs_zip_content(client, tmp_path):
    """ZIP enthält die erwarteten Log-Dateien mit korrektem Inhalt."""
    log_file = tmp_path / "irrigation.jsonl"
    log_content = '{"event": "test_entry"}\n'
    log_file.write_text(log_content, encoding="utf-8", newline="")

    backup = tmp_path / "irrigation.jsonl.1"
    backup_content = '{"event": "older_entry"}\n'
    backup.write_text(backup_content, encoding="utf-8", newline="")

    with patch("api.routes_system.LOG_DIR", str(tmp_path)):
        resp = client.get("/system/logs/download")

    assert resp.status_code == 200
    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert "irrigation.jsonl" in names
        assert "irrigation.jsonl.1" in names
        assert zf.read("irrigation.jsonl").decode() == log_content
        assert zf.read("irrigation.jsonl.1").decode() == backup_content


def test_download_logs_empty_dir(client, tmp_path):
    """Kein Log-File vorhanden: leere ZIP wird zurückgegeben (kein Fehler)."""
    with patch("api.routes_system.LOG_DIR", str(tmp_path)):
        resp = client.get("/system/logs/download")

    assert resp.status_code == 200
    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf) as zf:
        assert zf.namelist() == []


def test_download_logs_only_existing_files_included(client, tmp_path):
    """Nur vorhandene Backup-Dateien werden eingepackt, fehlende übersprungen."""
    (tmp_path / "irrigation.jsonl").write_text("aktuelle\n", encoding="utf-8")
    (tmp_path / "irrigation.jsonl.3").write_text("backup3\n", encoding="utf-8")
    # .1 und .2 existieren NICHT

    with patch("api.routes_system.LOG_DIR", str(tmp_path)):
        resp = client.get("/system/logs/download")

    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert "irrigation.jsonl" in names
        assert "irrigation.jsonl.3" in names
        assert "irrigation.jsonl.1" not in names
        assert "irrigation.jsonl.2" not in names


def test_download_logs_filename_contains_date(client, tmp_path):
    """Dateiname im Content-Disposition enthält heutiges Datum (YYYY-MM-DD)."""
    import re
    from datetime import datetime

    with patch("api.routes_system.LOG_DIR", str(tmp_path)):
        resp = client.get("/system/logs/download")

    cd = resp.headers["content-disposition"]
    match = re.search(r"noria-logs-(\d{4}-\d{2}-\d{2})\.zip", cd)
    assert match, f"Kein Datum in Content-Disposition: {cd}"
    # Datum muss heute sein
    assert match.group(1) == datetime.now().strftime("%Y-%m-%d")


def test_download_logs_requires_api_key(client):
    """Ohne API-Key: 401 Unauthorized."""
    resp = client.get("/system/logs/download", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_download_logs_wrong_api_key(client):
    """Mit falschem API-Key: 401 Unauthorized."""
    resp = client.get(
        "/system/logs/download",
        headers={"X-API-Key": "completely_wrong_key"},
    )
    assert resp.status_code == 401
