"""
Tests für api/routes_system.py

Getestet werden:
  POST /system/ack-restart   – Neustart-Quittierung, Auth, Idempotenz
  GET  /system/logs/download – ZIP-Download, Inhalt, Auth
  GET  /system/info          – OS-Metriken, Struktur, Fehlertoleranz, Auth
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


# ─────────────────────────────────────────────────────────────────────────────
# GET /system/info
# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import patch, MagicMock


def test_system_info_returns_200(client):
    """Endpunkt liefert HTTP 200."""
    resp = client.get("/system/info")
    assert resp.status_code == 200


def test_system_info_response_structure(client):
    """Response enthält alle erwarteten Top-Level-Schlüssel."""
    resp = client.get("/system/info")
    data = resp.json()
    assert "disk"     in data
    assert "memory"   in data
    assert "uptime_s" in data
    assert "network"  in data


def test_system_info_disk_structure(client):
    """disk-Objekt hat die erwarteten Felder."""
    resp = client.get("/system/info")
    disk = resp.json()["disk"]
    assert "total_gb" in disk
    assert "free_gb"  in disk
    assert "used_pct" in disk


def test_system_info_memory_structure(client):
    """memory-Objekt hat die erwarteten Felder."""
    resp = client.get("/system/info")
    mem = resp.json()["memory"]
    assert "total_mb" in mem
    assert "used_mb"  in mem
    assert "used_pct" in mem


def test_system_info_network_is_list(client):
    """network ist eine Liste."""
    resp = client.get("/system/info")
    assert isinstance(resp.json()["network"], list)


def test_system_info_network_entry_structure(client):
    """Wenn Interfaces vorhanden: jeder Eintrag hat name, type, is_up, ip."""
    resp = client.get("/system/info")
    for iface in resp.json()["network"]:
        assert "name"  in iface
        assert "type"  in iface
        assert "is_up" in iface
        assert "ip"    in iface


def test_system_info_uptime_is_non_negative(client):
    """uptime_s ist None oder eine nicht-negative Zahl."""
    resp = client.get("/system/info")
    uptime = resp.json()["uptime_s"]
    if uptime is not None:
        assert uptime >= 0


def test_system_info_disk_used_pct_range(client):
    """disk.used_pct ist None oder liegt zwischen 0 und 100."""
    resp = client.get("/system/info")
    pct = resp.json()["disk"].get("used_pct")
    if pct is not None:
        assert 0 <= pct <= 100


def test_system_info_memory_used_pct_range(client):
    """memory.used_pct ist None oder liegt zwischen 0 und 100."""
    resp = client.get("/system/info")
    pct = resp.json()["memory"].get("used_pct")
    if pct is not None:
        assert 0 <= pct <= 100


def test_system_info_disk_error_returns_null_fields(client):
    """Bei psutil-/shutil-Fehler: disk-Felder sind null, kein HTTP-Fehler."""
    with patch("api.routes_system._collect_disk", return_value={
        "total_gb": None, "free_gb": None, "used_pct": None
    }):
        resp = client.get("/system/info")
    assert resp.status_code == 200
    disk = resp.json()["disk"]
    assert disk["total_gb"] is None
    assert disk["free_gb"]  is None
    assert disk["used_pct"] is None


def test_system_info_memory_error_returns_null_fields(client):
    """Bei psutil-Fehler: memory-Felder sind null, kein HTTP-Fehler."""
    with patch("api.routes_system._collect_memory", return_value={
        "total_mb": None, "used_mb": None, "used_pct": None
    }):
        resp = client.get("/system/info")
    assert resp.status_code == 200
    mem = resp.json()["memory"]
    assert mem["total_mb"] is None


def test_system_info_uptime_error_returns_null(client):
    """Bei psutil-Fehler: uptime_s ist null, kein HTTP-Fehler."""
    with patch("api.routes_system._collect_uptime", return_value=None):
        resp = client.get("/system/info")
    assert resp.status_code == 200
    assert resp.json()["uptime_s"] is None


def test_system_info_network_error_returns_empty_list(client):
    """Bei psutil-Fehler: network ist leere Liste, kein HTTP-Fehler."""
    with patch("api.routes_system._collect_network", return_value=[]):
        resp = client.get("/system/info")
    assert resp.status_code == 200
    assert resp.json()["network"] == []


def test_system_info_wlan_entry_has_ssid_and_signal(client):
    """WLAN-Interface-Eintrag enthält ssid und signal_pct."""
    fake_network = [{
        "name": "wlan0", "type": "WLAN", "is_up": True,
        "ip": "192.168.1.50", "ssid": "TestNetz", "signal_pct": 72,
    }]
    with patch("api.routes_system._collect_network", return_value=fake_network):
        resp = client.get("/system/info")
    iface = resp.json()["network"][0]
    assert iface["ssid"]       == "TestNetz"
    assert iface["signal_pct"] == 72


def test_system_info_requires_api_key(client):
    """Ohne API-Key: 401 Unauthorized."""
    resp = client.get("/system/info", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_system_info_wrong_api_key(client):
    """Mit falschem API-Key: 401 Unauthorized."""
    resp = client.get("/system/info", headers={"X-API-Key": "falsch"})
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# _collect_wlan_details – Unit-Tests (Locale-Bug-Regression)
# ─────────────────────────────────────────────────────────────────────────────

from api.routes_system import _collect_wlan_details


def _make_nmcli_mock(stdout: str, returncode: int = 0):
    """Hilfsfunktion: erstellt einen subprocess.run-Mock mit vorgegebenem stdout."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    return mock


def test_wlan_details_parses_yes_line(monkeypatch):
    """Normale englische Ausgabe: 'yes' wird korrekt geparst."""
    fake = _make_nmcli_mock("no:AndereSSID:30\nyes:MeinNetz:72\n")
    monkeypatch.setattr("api.routes_system.subprocess.run", lambda *a, **kw: fake)
    result = _collect_wlan_details("wlan0")
    assert result["ssid"] == "MeinNetz"
    assert result["signal_pct"] == 72


def test_wlan_details_locale_ja_would_fail_without_fix(monkeypatch):
    """Regression: 'ja' (deutsche Locale ohne LC_ALL=C) darf nicht matchen.

    Dieser Test stellt sicher, dass wir LC_ALL=C setzen und daher 'yes'
    erwarten – 'ja' im Output bedeutet nmcli wurde OHNE unsere Locale-
    Überschreibung aufgerufen (sollte im produktiven Code nicht vorkommen).
    """
    # Simuliert nmcli-Ausgabe mit deutscher Locale (ohne unseren Fix)
    fake = _make_nmcli_mock("nein:AndereSSID:30\nja:MeinNetz:72\n")
    monkeypatch.setattr("api.routes_system.subprocess.run", lambda *a, **kw: fake)
    result = _collect_wlan_details("wlan0")
    # "ja" soll NICHT matchen – das ist korrekt mit LC_ALL=C
    assert result["ssid"] is None
    assert result["signal_pct"] is None


def test_wlan_details_ssid_with_colon(monkeypatch):
    """Regression: SSID mit Doppelpunkt (z.B. 'Fritzbox:5G') wird korrekt geparst.

    nmcli -t (terse mode) escaped Doppelpunkte innerhalb von Feldwerten als \\:.
    Die SSID "Fritzbox:5G" erscheint in der Ausgabe als "yes:Fritzbox\\:5G:65".
    """
    fake = _make_nmcli_mock("yes:Fritzbox\\:5G:65\n")
    monkeypatch.setattr("api.routes_system.subprocess.run", lambda *a, **kw: fake)
    result = _collect_wlan_details("wlan0")
    assert result["ssid"] == "Fritzbox:5G"
    assert result["signal_pct"] == 65


def test_wlan_details_nmcli_fails(monkeypatch):
    """nmcli returncode != 0 → None-Werte, kein Crash."""
    fake = _make_nmcli_mock("", returncode=1)
    monkeypatch.setattr("api.routes_system.subprocess.run", lambda *a, **kw: fake)
    result = _collect_wlan_details("wlan0")
    assert result["ssid"] is None
    assert result["signal_pct"] is None


def test_wlan_details_nmcli_not_found(monkeypatch):
    """nmcli nicht installiert (FileNotFoundError) → None-Werte, kein Crash."""
    def raise_fnf(*a, **kw):
        raise FileNotFoundError("nmcli not found")
    monkeypatch.setattr("api.routes_system.subprocess.run", raise_fnf)
    result = _collect_wlan_details("wlan0")
    assert result["ssid"] is None
    assert result["signal_pct"] is None


def test_wlan_details_lc_all_c_is_set(monkeypatch):
    """LC_ALL=C und LANG=C müssen im subprocess.run-env gesetzt sein."""
    captured_env = {}

    def capture(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return _make_nmcli_mock("yes:TestNetz:80\n")

    monkeypatch.setattr("api.routes_system.subprocess.run", capture)
    _collect_wlan_details("wlan0")
    assert captured_env.get("LC_ALL") == "C"
    assert captured_env.get("LANG") == "C"
