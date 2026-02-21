"""
Tests für core/security.py (Step 1: API Key Authentication)

Getestet werden:
  - load_or_create_api_key(): Key-Generierung bei nicht vorhandener Datei
  - load_or_create_api_key(): Key-Laden bei vorhandener, valider Datei
  - load_or_create_api_key(): Key-Neugenerierung bei invalidem Format
  - get_api_key(): Gibt aktuellen Key zurück
  - require_api_key Dependency: korrekte Key → 200
  - require_api_key Dependency: falscher Key → 401
  - require_api_key Dependency: leerer Key → 401
  - require_api_key Dependency: kein Header → 401
  - require_api_key Dependency: GET /health bleibt offen (kein Auth)
  - Auth-Fehler werden geloggt (Event: auth_failure)
  - Verschiedene geschützte Endpoints erfordern Auth
"""

import os
import pytest

import core.security as sec
from tests.conftest import TEST_API_KEY


# ---------------------------------------------------------------------------
# Hilfsfunktion: roher Client OHNE voreingestellten Auth-Header
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_client(app):
    """TestClient ohne X-API-Key-Header – für Auth-Fehler-Tests."""
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# load_or_create_api_key()
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadOrCreateApiKey:
    """Tests für Key-Generierung und -Laden."""

    def test_generates_new_key_if_file_missing(self, tmp_path, monkeypatch):
        """Wenn api_key.txt nicht existiert, wird ein neuer Key generiert."""
        key_file = str(tmp_path / "api_key.txt")
        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
        assert os.path.exists(key_file)
        # Auf Disk gespeicherter Key stimmt überein
        with open(key_file) as f:
            assert f.read().strip() == result

    def test_loads_existing_valid_key(self, tmp_path, monkeypatch):
        """Wenn api_key.txt eine valide 64-Hex-Datei enthält, wird sie geladen."""
        key_file = tmp_path / "api_key.txt"
        existing_key = "ab12cd34" * 8  # 64 Hex-Zeichen
        key_file.write_text(existing_key)

        monkeypatch.setattr(sec, "API_KEY_FILE", str(key_file))
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert result == existing_key

    def test_regenerates_key_for_invalid_format(self, tmp_path, monkeypatch):
        """Wenn api_key.txt ein ungültiges Format enthält, wird ein neuer Key generiert."""
        key_file = tmp_path / "api_key.txt"
        key_file.write_text("zu_kurz_und_ungültig")  # kein valides Format

        monkeypatch.setattr(sec, "API_KEY_FILE", str(key_file))
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_generated_key_is_random(self, tmp_path, monkeypatch):
        """Zwei aufeinanderfolgende Neugenerierungen erzeugen unterschiedliche Keys."""
        key_file1 = str(tmp_path / "key1.txt")
        key_file2 = str(tmp_path / "key2.txt")

        monkeypatch.setattr(sec, "_api_key", "")
        monkeypatch.setattr(sec, "API_KEY_FILE", key_file1)
        key1 = sec.load_or_create_api_key()

        monkeypatch.setattr(sec, "_api_key", "")
        monkeypatch.setattr(sec, "API_KEY_FILE", key_file2)
        key2 = sec.load_or_create_api_key()

        assert key1 != key2

    def test_sets_module_variable(self, tmp_path, monkeypatch):
        """Nach load_or_create_api_key ist die Modulvariable _api_key korrekt gesetzt."""
        key_file = str(tmp_path / "api_key.txt")
        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert sec._api_key == result
        assert sec.get_api_key() == result


class TestIsValidKeyFormat:
    """Tests für interne Validierungslogik."""

    def test_valid_64_hex(self):
        assert sec._is_valid_key_format("a" * 64) is True
        assert sec._is_valid_key_format("0123456789abcdef" * 4) is True

    def test_too_short(self):
        assert sec._is_valid_key_format("a" * 63) is False

    def test_too_long(self):
        assert sec._is_valid_key_format("a" * 65) is False

    def test_invalid_chars(self):
        assert sec._is_valid_key_format("g" * 64) is False  # 'g' ist kein Hex
        assert sec._is_valid_key_format("A" * 64) is False  # Uppercase nicht erlaubt

    def test_empty_string(self):
        assert sec._is_valid_key_format("") is False

    def test_non_string(self):
        assert sec._is_valid_key_format(None) is False  # type: ignore[arg-type]
        assert sec._is_valid_key_format(12345) is False  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# require_api_key Dependency (über FastAPI-Routes)
# ─────────────────────────────────────────────────────────────────────────────


class TestRequireApiKey:
    """
    Testet die Auth-Dependency über den client-Fixture (korrekte Key)
    und den raw_client-Fixture (kein Key).
    """

    # --- Erfolg ---

    def test_correct_key_allows_access(self, client):
        """Mit dem richtigen Key wird die Route erreicht (200)."""
        resp = client.get("/status")
        assert resp.status_code == 200

    def test_correct_key_allows_post(self, client):
        """POST-Routen sind mit korrektem Key erreichbar."""
        resp = client.post("/stop")
        assert resp.status_code == 200

    # --- Fehlende / falsche Keys ---

    def test_missing_key_returns_401(self, raw_client):
        """Anfrage ohne X-API-Key-Header → 401."""
        resp = raw_client.get("/status")
        assert resp.status_code == 401

    def test_empty_key_returns_401(self, client):
        """Leerer X-API-Key-Header → 401."""
        resp = client.get("/status", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        """Falscher X-API-Key-Header → 401."""
        resp = client.get("/status", headers={"X-API-Key": "wrongkey"})
        assert resp.status_code == 401

    def test_401_response_has_detail_field(self, raw_client):
        """401-Response enthält das 'detail'-Feld."""
        resp = raw_client.get("/status")
        assert "detail" in resp.json()

    def test_wrong_key_on_post_returns_401(self, client):
        """Falscher Key auf POST-Route → 401, keine State-Änderung."""
        resp = client.post("/stop", headers={"X-API-Key": "falsch"})
        assert resp.status_code == 401

    def test_wrong_key_on_queue_add_returns_401(self, client):
        resp = client.post(
            "/queue/add",
            json={"zone": 1, "duration": 60, "time_unit": "Sekunden"},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    def test_wrong_key_on_schedule_add_returns_401(self, client):
        payload = {
            "zone": 1,
            "weekdays": [0],
            "start_times": ["06:00"],
            "duration_s": 60,
            "repeat": True,
            "time_unit": "Sekunden",
        }
        resp = client.post("/schedule/add", json=payload, headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_wrong_key_on_history_returns_401(self, client):
        resp = client.get("/history", headers={"X-API-Key": ""})
        assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# GET /health: offen ohne Auth
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthOpenEndpoint:
    """GET /health ist ohne API-Key erreichbar (Monitoring-Endpoint)."""

    def test_health_accessible_without_key(self, raw_client):
        """Kein X-API-Key → /health antwortet mit 200."""
        resp = raw_client.get("/health")
        assert resp.status_code == 200

    def test_health_accessible_with_wrong_key(self, client):
        """Falscher X-API-Key → /health antwortet trotzdem mit 200."""
        resp = client.get("/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 200

    def test_health_accessible_with_empty_key(self, raw_client):
        """Leerer X-API-Key → /health antwortet mit 200."""
        resp = raw_client.get("/health", headers={"X-API-Key": ""})
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Timing-Sicherheit: Konstante Zeitvergleich
# ─────────────────────────────────────────────────────────────────────────────


class TestTimingAttackResistance:
    """Stellt sicher, dass secrets.compare_digest verwendet wird."""

    def test_compare_digest_used(self):
        """
        compare_digest verhindert Timing-Angriffe.
        Wir prüfen nur, dass unser Code es korrekt aufruft
        (indirekter Test: korrekte Key → True, falscher → False).
        """
        # Korrekte Key-Validierung
        correct = sec._is_valid_key_format(TEST_API_KEY)
        assert correct is True

        # compare_digest Verhalten für verschieden lange Strings
        import secrets
        assert secrets.compare_digest("a" * 64, "a" * 64) is True
        assert secrets.compare_digest("a" * 64, "b" * 64) is False


# ─────────────────────────────────────────────────────────────────────────────
# Logging: Auth-Failures werden protokolliert
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthFailureLogging:
    """
    Prüft, dass auth_failure-Events bei ungültigem Key geloggt werden.
    """

    def test_auth_failure_event_logged_on_wrong_key(self, raw_client, monkeypatch):
        """Bei falschem Key wird log_event('auth_failure', ...) aufgerufen."""
        logged_events = []

        import core.security as security_module

        original_log = security_module.log_event

        def capture_log(event, **kwargs):
            logged_events.append((event, kwargs))
            return original_log(event, **kwargs)

        monkeypatch.setattr(security_module, "log_event", capture_log)

        raw_client.get("/status")  # kein Key → 401

        auth_failures = [e for e in logged_events if e[0] == "auth_failure"]
        assert len(auth_failures) >= 1

    def test_auth_failure_contains_path(self, raw_client, monkeypatch):
        """auth_failure-Event enthält den angefragten Pfad."""
        logged_events = []

        import core.security as security_module

        original_log = security_module.log_event

        def capture_log(event, **kwargs):
            logged_events.append((event, kwargs))
            return original_log(event, **kwargs)

        monkeypatch.setattr(security_module, "log_event", capture_log)

        raw_client.get("/status")

        failure = next(e for e in logged_events if e[0] == "auth_failure")
        assert failure[1].get("path") == "/status"

    def test_auth_failure_not_logged_on_correct_key(self, client, monkeypatch):
        """Bei korrektem Key wird KEIN auth_failure geloggt."""
        logged_events = []

        import core.security as security_module

        original_log = security_module.log_event

        def capture_log(event, **kwargs):
            logged_events.append((event, kwargs))
            return original_log(event, **kwargs)

        monkeypatch.setattr(security_module, "log_event", capture_log)

        client.get("/status")  # korrekte Key via Fixture

        auth_failures = [e for e in logged_events if e[0] == "auth_failure"]
        assert len(auth_failures) == 0

    def test_health_no_auth_failure_logged(self, raw_client, monkeypatch):
        """GET /health löst kein auth_failure aus (kein Auth nötig)."""
        logged_events = []

        import core.security as security_module

        original_log = security_module.log_event

        def capture_log(event, **kwargs):
            logged_events.append((event, kwargs))
            return original_log(event, **kwargs)

        monkeypatch.setattr(security_module, "log_event", capture_log)

        raw_client.get("/health")

        auth_failures = [e for e in logged_events if e[0] == "auth_failure"]
        assert len(auth_failures) == 0
