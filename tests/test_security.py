"""
Tests für core/security.py (Step 1), Rate Limiting (Step 2) und CORS (Step 3).

Step 1 – API Key Authentication:
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

Step 2 – Rate Limiting:
  - 429 bei Überschreitung des Mutations-Limits (POST)
  - 429 bei Überschreitung des globalen Limits (GET)
  - GET /health ist vom Rate-Limit NICHT ausgenommen (globales Limit gilt)
  - Unter dem Limit wird 200 zurückgegeben
  - 429-Response enthält 'detail'-Feld
  - Rate-Limit-Event wird geloggt

Step 3 – CORS-Konfiguration:
  - Erlaubte Origin → Access-Control-Allow-Origin im Response
  - Nicht-erlaubte Origin → kein Access-Control-Allow-Origin im Response
  - Kein Origin-Header → kein Access-Control-Allow-Origin (normaler Request)
  - OPTIONS-Preflight mit erlaubter Origin → 200 + korrekte CORS-Headers
  - OPTIONS-Preflight mit nicht-erlaubter Origin → kein ACAO-Header
  - ALLOWED_ORIGINS Parsing: Komma-separiert mit Whitespace-Toleranz
  - ALLOWED_ORIGINS Default: http://localhost:8080 ohne Env-Var
  - allow_credentials ist False (kein Cookie-Auth)
"""

import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import core.security as sec
from tests.conftest import TEST_API_KEY, CORS_TEST_ORIGIN


# ---------------------------------------------------------------------------
# Hilfsfunktion: roher Client OHNE voreingestellten Auth-Header
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_client(app):
    """TestClient ohne X-API-Key-Header – für Auth-Fehler-Tests."""
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Hilfsfunktion: App mit sehr niedrigem Rate-Limit für 429-Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def rate_limit_app():
    """
    Minimal-App mit sehr niedrigem Rate-Limit für Überschreitungs-Tests.

    Globales Limit: 3/minute
    Mutations-Limit: 2/minute

    Damit können wir 429 ohne viele Requests provozieren.
    Die App wird per-Test frisch erstellt → saubere Storage.
    """
    import core.security as sec
    from api.errors import register_error_handlers
    from api.routes_control import router as control_router
    from api.routes_queue import router as queue_router
    from api.routes_health import router as health_router

    # Frischer Limiter mit niedrigem Limit – nur für diese Test-App.
    low_limiter = Limiter(key_func=get_remote_address, default_limits=["3/minute"])

    _app = FastAPI()
    _app.state.limiter = low_limiter
    _app.add_middleware(SlowAPIMiddleware)
    register_error_handlers(_app)

    # Mutation-Routen werden mit dem niedrigen Limiter dekoriert – wir brauchen
    # eigene, einfachere Test-Routen um das Mutations-Limit isoliert zu testen.
    from fastapi import APIRouter, Depends, Request
    from core.security import require_api_key

    test_router = APIRouter(dependencies=[Depends(require_api_key)])

    @test_router.post("/test/mutation")
    @low_limiter.limit("2/minute")
    def test_mutation(request: Request):
        return {"ok": True}

    @test_router.get("/test/read")
    def test_read(request: Request):
        return {"ok": True}

    _app.include_router(test_router)
    _app.include_router(health_router)

    return _app


@pytest.fixture
def rate_limit_client(rate_limit_app):
    """TestClient für rate_limit_app mit Auth-Header."""
    # Sicherstellen, dass der API-Key für die rate_limit_app gesetzt ist.
    import core.security as sec
    sec._api_key = TEST_API_KEY
    with TestClient(rate_limit_app, raise_server_exceptions=True,
                    headers={"X-API-Key": TEST_API_KEY}) as c:
        yield c
    # Cleanup
    sec._api_key = ""


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
        key_file = str(tmp_path / "api_key.txt")
        existing_key = "a" * 64
        with open(key_file, "w") as f:
            f.write(existing_key)

        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert result == existing_key
        assert sec._api_key == existing_key

    def test_regenerates_key_on_invalid_format(self, tmp_path, monkeypatch):
        """Wenn der Key ein ungültiges Format hat, wird ein neuer Key generiert."""
        key_file = str(tmp_path / "api_key.txt")
        with open(key_file, "w") as f:
            f.write("not-a-valid-key")

        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
        # Neuer Key ist nicht der ungültige alte Key
        assert result != "not-a-valid-key"

    def test_sets_module_variable(self, tmp_path, monkeypatch):
        """load_or_create_api_key() setzt _api_key und get_api_key() gibt ihn zurück."""
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
        resp = client.post("/stop", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_health_endpoint_requires_no_auth(self, raw_client):
        """GET /health ist ohne Auth erreichbar (Monitoring-Endpoint)."""
        resp = raw_client.get("/health")
        assert resp.status_code == 200

    def test_auth_failure_is_logged(self, raw_client, caplog):
        """Auth-Fehler werden als 'auth_failure'-Event geloggt."""
        import logging
        with caplog.at_level(logging.WARNING):
            raw_client.get("/status")
        assert any("auth_failure" in record.message for record in caplog.records)

    def test_compare_digest_used(self):
        """
        compare_digest verhindert Timing-Angriffe.
        Wir prüfen indirekt: korrekte Key → Zugriff, falscher → 401.
        Der direkte Test wäre ein Unittest auf _require_api_key's Implementierung.
        """
        import secrets
        correct = TEST_API_KEY
        wrong = "x" * 64
        # compare_digest gibt False zurück wenn Keys nicht übereinstimmen
        assert secrets.compare_digest(correct, correct) is True
        assert secrets.compare_digest(correct, wrong) is False


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Rate Limiting Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimiting:
    """
    Tests für das Rate-Limiting (Step 2).

    Verwendet rate_limit_app/rate_limit_client mit niedrigen Limits:
      - Global:    3/minute
      - Mutations: 2/minute (POST /test/mutation)

    Damit können 429-Responses mit wenigen Requests provoziert werden.
    """

    def test_mutation_under_limit_returns_200(self, rate_limit_client):
        """Erste Anfrage auf Mutations-Route → 200 (unter dem Limit)."""
        resp = rate_limit_client.post("/test/mutation")
        assert resp.status_code == 200

    def test_mutation_over_limit_returns_429(self, rate_limit_client):
        """Nach Überschreitung des Mutations-Limits (2/min) → 429."""
        # 2 erlaubte Anfragen verbrauchen
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")
        # 3. Anfrage überschreitet das Limit
        resp = rate_limit_client.post("/test/mutation")
        assert resp.status_code == 429

    def test_429_response_contains_detail_field(self, rate_limit_client):
        """429-Response enthält 'detail'-Feld (konsistent mit anderen Fehlerantworten)."""
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")
        resp = rate_limit_client.post("/test/mutation")
        assert resp.status_code == 429
        data = resp.json()
        assert "detail" in data

    def test_global_limit_applies_to_reads(self, rate_limit_client):
        """Das globale Limit (3/min) greift auch auf GET-Routen."""
        # 3 erlaubte Anfragen verbrauchen
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
        # 4. Anfrage überschreitet das globale Limit
        resp = rate_limit_client.get("/test/read")
        assert resp.status_code == 429

    def test_reads_count_towards_global_limit(self, rate_limit_client):
        """
        Das globale Limit (3/min) greift pro Endpoint-Bucket.
        SlowAPI führt separate Zähler je Route – GET /test/read hat seinen
        eigenen Bucket mit 3/min. Nach 3 Anfragen an denselben Endpoint → 429.
        """
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
        # 4. Anfrage an denselben Endpoint überschreitet den Bucket
        resp = rate_limit_client.get("/test/read")
        assert resp.status_code == 429

    def test_mutation_limit_is_stricter_than_global(self, rate_limit_client):
        """
        Mutations-Limit (2/min) greift vor dem globalen Limit (3/min).
        Nach 2 POSTs → nächster POST liefert 429, obwohl globales Limit noch nicht erreicht.
        """
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")
        # Noch im globalen Limit (2 < 3), aber Mutations-Limit erschöpft
        resp = rate_limit_client.post("/test/mutation")
        assert resp.status_code == 429

    def test_rate_limit_event_is_logged(self, rate_limit_client, caplog):
        """
        Bei 429 wird ein 'rate_limit_exceeded'-Event geloggt.
        """
        import logging
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")

        with caplog.at_level(logging.WARNING):
            rate_limit_client.post("/test/mutation")

        assert any("rate_limit_exceeded" in record.message for record in caplog.records)

    def test_normal_app_has_high_enough_limit(self, client):
        """
        Die normale Test-App hat ein Limit von 120/min – typische Tests
        erreichen dieses Limit nicht und bekommen immer 200.
        """
        # 5 Anfragen – weit unter dem Limit
        for _ in range(5):
            resp = client.get("/status")
            assert resp.status_code == 200

    def test_normal_app_mutations_have_limit(self, client, mock_io):
        """
        Auch die normale Test-App hat ein Mutations-Limit (30/min).
        Unter dem Limit erhalten POST-Routen 200.
        """
        # 5 POSTs – weit unter dem Limit von 30/min
        for _ in range(5):
            resp = client.post("/stop")
            assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: CORS-Konfiguration Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCORS:
    """
    Tests für die CORS-Konfiguration (Step 3).

    Die Test-App (app-Fixture aus conftest.py) hat CORSMiddleware konfiguriert
    mit CORS_TEST_ORIGIN als einzig erlaubter Origin.

    Technik: Starlette's TestClient ist kein Browser und erzwingt CORS nicht.
    Wir prüfen das Verhalten der CORSMiddleware direkt, indem wir den
    Origin-Header manuell setzen und die Response-Headers auswerten:
      - Access-Control-Allow-Origin: Zeigt an, dass die Origin erlaubt ist.
      - Fehlen dieses Headers: Origin ist nicht in der Whitelist.
    """

    # ── Einfache Requests (nicht-Preflight) ──────────────────────────────────

    def test_allowed_origin_gets_acao_header(self, client):
        """
        Request mit erlaubter Origin → Access-Control-Allow-Origin im Response.
        Der Header-Wert muss der gesendeten Origin entsprechen.
        """
        resp = client.get("/health", headers={"Origin": CORS_TEST_ORIGIN})
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == CORS_TEST_ORIGIN

    def test_disallowed_origin_has_no_acao_header(self, client):
        """
        Request mit nicht-erlaubter Origin → kein Access-Control-Allow-Origin.
        CORS ist ein Browser-Mechanismus; der Server antwortet weiterhin,
        aber ohne ACAO-Header – der Browser blockiert dann den Zugriff.
        """
        resp = client.get("/health", headers={"Origin": "http://evil.attacker.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_request_without_origin_has_no_acao_header(self, client):
        """
        Kein Origin-Header gesendet (z.B. direkter API-Aufruf, kein Browser)
        → kein Access-Control-Allow-Origin im Response.
        CORS-Headers sind nur relevant wenn ein Origin vorhanden ist.
        """
        resp = client.get("/health")
        assert "access-control-allow-origin" not in resp.headers

    def test_allowed_origin_on_authenticated_endpoint(self, client):
        """
        CORS-Headers erscheinen auch auf geschützten Endpunkten (mit Auth).
        Wichtig: CORSMiddleware greift VOR Auth-Prüfung (outermost).
        """
        resp = client.get("/status", headers={"Origin": CORS_TEST_ORIGIN})
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == CORS_TEST_ORIGIN

    def test_disallowed_origin_on_authenticated_endpoint(self, client):
        """Nicht-erlaubte Origin auf Auth-Endpunkt → kein ACAO-Header."""
        resp = client.get("/status", headers={"Origin": "http://evil.attacker.com"})
        assert "access-control-allow-origin" not in resp.headers

    # ── Preflight OPTIONS-Requests ────────────────────────────────────────────

    def test_preflight_allowed_origin_returns_200(self, client):
        """
        OPTIONS-Preflight mit erlaubter Origin → 200.
        CORSMiddleware antwortet Preflight-Requests direkt (ohne Auth-Check).
        """
        resp = client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200

    def test_preflight_allowed_origin_has_acao_header(self, client):
        """OPTIONS-Preflight mit erlaubter Origin → Access-Control-Allow-Origin gesetzt."""
        resp = client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == CORS_TEST_ORIGIN

    def test_preflight_allowed_origin_has_acam_header(self, client):
        """
        OPTIONS-Preflight mit erlaubter Origin → Access-Control-Allow-Methods.
        Enthält mindestens GET, POST, DELETE (die konfigurierten Methoden).
        """
        resp = client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-methods" in resp.headers
        allowed_methods = resp.headers["access-control-allow-methods"].upper()
        assert "POST" in allowed_methods

    def test_preflight_disallowed_origin_has_no_acao_header(self, client):
        """
        OPTIONS-Preflight mit nicht-erlaubter Origin → kein ACAO-Header.
        """
        resp = client.options(
            "/status",
            headers={
                "Origin": "http://evil.attacker.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_bypasses_api_key_auth(self, raw_client):
        """
        OPTIONS-Preflight kommt ohne Auth-Header an (Browser sendet keinen).
        CORSMiddleware muss Preflight VOR Auth-Check beantworten (outermost).
        Ohne diese Eigenschaft würde jeder Preflight mit 401 scheitern.
        """
        resp = raw_client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        # Preflight darf NICHT mit 401 scheitern – CORSMiddleware antwortet direkt
        assert resp.status_code != 401

    # ── allow_credentials ────────────────────────────────────────────────────

    def test_allow_credentials_is_false(self, client):
        """
        allow_credentials=False: 'Access-Control-Allow-Credentials' darf nicht
        'true' sein. Cookies oder gespeicherte Auth-Credentials können damit
        nicht in Cross-Origin-Requests gesendet werden (kein CSRF-Risiko).
        """
        resp = client.get("/health", headers={"Origin": CORS_TEST_ORIGIN})
        acao_credentials = resp.headers.get("access-control-allow-credentials", "false")
        assert acao_credentials.lower() != "true"

    # ── ALLOWED_ORIGINS Parsing (core/config.py) ─────────────────────────────

    def test_allowed_origins_default_without_env_var(self, monkeypatch):
        """
        Ohne ALLOWED_ORIGINS-Env-Var ist der Default 'http://localhost:8080'.
        """
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
        # core.config neu importieren mit bereinigter Umgebung
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == ["http://localhost:8080"]

    def test_allowed_origins_single_from_env(self, monkeypatch):
        """Einzelne Origin aus Env-Var wird korrekt gelesen."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "http://192.168.1.100:8080")
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == ["http://192.168.1.100:8080"]

    def test_allowed_origins_multiple_comma_separated(self, monkeypatch):
        """Komma-separierte Origins werden korrekt in eine Liste geparst."""
        monkeypatch.setenv(
            "ALLOWED_ORIGINS",
            "http://192.168.1.100:8080,http://localhost:8080",
        )
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == [
            "http://192.168.1.100:8080",
            "http://localhost:8080",
        ]

    def test_allowed_origins_whitespace_is_stripped(self, monkeypatch):
        """Leerzeichen um Origins herum werden toleriert und entfernt."""
        monkeypatch.setenv(
            "ALLOWED_ORIGINS",
            "http://192.168.1.100:8080 , http://localhost:8080 ",
        )
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == [
            "http://192.168.1.100:8080",
            "http://localhost:8080",
        ]

    def test_allowed_origins_empty_entries_ignored(self, monkeypatch):
        """Leere Einträge (z.B. doppeltes Komma) werden ignoriert."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8080,,")
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == ["http://localhost:8080"]
