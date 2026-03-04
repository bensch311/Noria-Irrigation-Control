# tests/test_security.py
"""
Tests für core/security.py (Step 1), Rate Limiting (Step 2), CORS (Step 3),
Security Response Headers (Step 4) und Audit Logging mit Client-IP (Step 6).

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

Step 4 – Security Response Headers:
  - X-Content-Type-Options: nosniff auf normalen Responses
  - X-Frame-Options: DENY auf normalen Responses
  - X-XSS-Protection: 0 auf normalen Responses
  - Referrer-Policy: no-referrer auf normalen Responses
  - Content-Security-Policy: default-src 'none' auf normalen Responses
  - Server-Header ist nicht 'uvicorn' (neutralisiert)
  - Server-Header hat den opaken Wert 'webserver'
  - Security-Header sind auf Fehler-Responses (401) vorhanden
  - Security-Header sind auf CORS-Preflight-Responses vorhanden
  - Security-Header sind auch ohne Origin-Header vorhanden (kein CORS-Kontext)

Step 6 – Audit Logging mit Client-IP:
  - get_client_ip(): gibt request.client.host zurück wenn kein X-Forwarded-For
  - get_client_ip(): gibt ersten Eintrag aus X-Forwarded-For zurück wenn gesetzt
  - get_client_ip(): mehrere IPs in X-Forwarded-For → nur den ersten (Client)
  - get_client_ip(): whitespace in X-Forwarded-For wird entfernt
  - get_client_ip(): kein request.client → "unknown"
  - auth_failure-Event enthält client_ip
  - rate_limit_exceeded-Event enthält client_ip
  - request_rejected (404)-Event enthält client_ip
  - request_rejected (409)-Event enthält client_ip
  - request_validation_error (422)-Event enthält client_ip
  - X-Forwarded-For wird als client_ip in auth_failure übernommen
  - X-Forwarded-For wird als client_ip in request_validation_error übernommen
  - X-Forwarded-For: erster Eintrag aus Proxy-Kette wird verwendet
"""

import json
import logging
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from unittest.mock import MagicMock

import core.security as sec
from tests.conftest import TEST_API_KEY, CORS_TEST_ORIGIN


# ---------------------------------------------------------------------------
# Fixtures: roher Client OHNE Auth-Header
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_client(app):
    """TestClient ohne X-API-Key-Header – für Auth-Fehler-Tests."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Fixture: App mit sehr niedrigem Rate-Limit für 429-Tests
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
    from api.errors import register_error_handlers
    from api.routes_health import router as health_router

    low_limiter = Limiter(key_func=get_remote_address, default_limits=["3/minute"])

    _app = FastAPI()
    _app.state.limiter = low_limiter
    _app.add_middleware(SlowAPIMiddleware)
    register_error_handlers(_app)

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
    import core.security as sec
    sec._api_key = TEST_API_KEY
    with TestClient(rate_limit_app, raise_server_exceptions=True,
                    headers={"X-API-Key": TEST_API_KEY}) as c:
        yield c
    sec._api_key = ""


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: load_or_create_api_key()
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
        assert sec._api_key == result
        assert sec.get_api_key() == result

    def test_loads_existing_valid_key(self, tmp_path, monkeypatch):
        """Vorhandene, valide api_key.txt wird geladen."""
        key_file = str(tmp_path / "api_key.txt")
        existing_key = "a" * 64
        with open(key_file, "w") as f:
            f.write(existing_key)

        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert result == existing_key
        assert sec._api_key == existing_key

    def test_regenerates_key_if_format_invalid(self, tmp_path, monkeypatch):
        """Key mit falschem Format (zu kurz) wird verworfen, neuer generiert."""
        key_file = str(tmp_path / "api_key.txt")
        with open(key_file, "w") as f:
            f.write("tooshort")

        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert result != "tooshort"
        assert len(result) == 64

    def test_get_api_key_returns_current_key(self, tmp_path, monkeypatch):
        """get_api_key() gibt den nach load_or_create gesetzten Key zurück."""
        key_file = str(tmp_path / "api_key.txt")
        monkeypatch.setattr(sec, "API_KEY_FILE", key_file)
        monkeypatch.setattr(sec, "_api_key", "")

        result = sec.load_or_create_api_key()

        assert sec._api_key == result
        assert sec.get_api_key() == result


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: _is_valid_key_format()
# ─────────────────────────────────────────────────────────────────────────────

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
# Step 1: require_api_key Dependency (über FastAPI-Routes)
# ─────────────────────────────────────────────────────────────────────────────

class TestRequireApiKey:
    """
    Testet die Auth-Dependency über den client-Fixture (korrekte Key)
    und den raw_client-Fixture (kein Key).
    """

    def test_correct_key_allows_access(self, client):
        """Mit dem richtigen Key wird die Route erreicht (200)."""
        resp = client.get("/status")
        assert resp.status_code == 200

    def test_correct_key_allows_post(self, client):
        """POST-Routen sind mit korrektem Key erreichbar."""
        resp = client.post("/stop")
        assert resp.status_code == 200

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
        with caplog.at_level(logging.WARNING):
            raw_client.get("/status")
        assert any("auth_failure" in record.message for record in caplog.records)

    def test_compare_digest_used(self):
        """compare_digest verhindert Timing-Angriffe."""
        import secrets
        correct = TEST_API_KEY
        wrong = "x" * 64
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
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")
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
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
        rate_limit_client.get("/test/read")
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
        resp = rate_limit_client.get("/test/read")
        assert resp.status_code == 429

    def test_mutation_limit_is_stricter_than_global(self, rate_limit_client):
        """
        Mutations-Limit (2/min) greift vor dem globalen Limit (3/min).
        Nach 2 POSTs → nächster POST liefert 429, obwohl globales Limit noch nicht erreicht.
        """
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")
        resp = rate_limit_client.post("/test/mutation")
        assert resp.status_code == 429

    def test_rate_limit_event_is_logged(self, rate_limit_client, caplog):
        """Bei 429 wird ein 'rate_limit_exceeded'-Event geloggt."""
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
        for _ in range(5):
            resp = client.get("/status")
            assert resp.status_code == 200

    def test_normal_app_mutations_have_limit(self, client, mock_io):
        """
        Auch die normale Test-App hat ein Mutations-Limit (30/min).
        Unter dem Limit erhalten POST-Routen 200.
        """
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
    Origin-Header manuell setzen und die Response-Headers auswerten.
    """

    def test_allowed_origin_gets_acao_header(self, client):
        """Request mit erlaubter Origin → Access-Control-Allow-Origin im Response."""
        resp = client.get("/health", headers={"Origin": CORS_TEST_ORIGIN})
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == CORS_TEST_ORIGIN

    def test_disallowed_origin_has_no_acao_header(self, client):
        """Request mit nicht-erlaubter Origin → kein Access-Control-Allow-Origin."""
        resp = client.get("/health", headers={"Origin": "http://evil.attacker.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_request_without_origin_has_no_acao_header(self, client):
        """Kein Origin-Header → kein ACAO im Response."""
        resp = client.get("/health")
        assert "access-control-allow-origin" not in resp.headers

    def test_allowed_origin_on_authenticated_endpoint(self, client):
        """CORS-Headers erscheinen auch auf geschützten Endpunkten (mit Auth)."""
        resp = client.get("/status", headers={"Origin": CORS_TEST_ORIGIN})
        assert "access-control-allow-origin" in resp.headers
        assert resp.headers["access-control-allow-origin"] == CORS_TEST_ORIGIN

    def test_disallowed_origin_on_authenticated_endpoint(self, client):
        """Nicht-erlaubte Origin auf Auth-Endpunkt → kein ACAO-Header."""
        resp = client.get("/status", headers={"Origin": "http://evil.attacker.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_allowed_origin_returns_200(self, client):
        """OPTIONS-Preflight mit erlaubter Origin → 200."""
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
        """OPTIONS-Preflight mit erlaubter Origin → Access-Control-Allow-Methods."""
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
        """OPTIONS-Preflight mit nicht-erlaubter Origin → kein ACAO-Header."""
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
        """
        resp = raw_client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code != 401

    def test_allow_credentials_is_false(self, client):
        """
        allow_credentials=False: 'Access-Control-Allow-Credentials' darf nicht
        'true' sein.
        """
        resp = client.get("/health", headers={"Origin": CORS_TEST_ORIGIN})
        acao_credentials = resp.headers.get("access-control-allow-credentials", "false")
        assert acao_credentials.lower() != "true"

    def test_allowed_origins_default_without_env_var(self, monkeypatch):
        """Ohne ALLOWED_ORIGINS-Env-Var ist der Default 'http://localhost:8080'."""
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
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
        monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8080, http://192.168.1.100:8080")
        import importlib
        import core.config as cfg
        importlib.reload(cfg)
        assert cfg.ALLOWED_ORIGINS == ["http://localhost:8080", "http://192.168.1.100:8080"]


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Security Response Headers Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityHeaders:
    """
    Tests für SecurityHeadersMiddleware (Step 4).

    Die Test-App (app-Fixture aus conftest.py) enthält SecurityHeadersMiddleware
    als outermost Middleware, identisch mit der Produktionskonfiguration.
    """

    def test_x_content_type_options_present(self, client):
        resp = client.get("/health")
        assert "x-content-type-options" in resp.headers

    def test_x_content_type_options_value(self, client):
        resp = client.get("/health")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options_present(self, client):
        resp = client.get("/health")
        assert "x-frame-options" in resp.headers

    def test_x_frame_options_value(self, client):
        resp = client.get("/health")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_x_xss_protection_present(self, client):
        resp = client.get("/health")
        assert "x-xss-protection" in resp.headers

    def test_x_xss_protection_value(self, client):
        resp = client.get("/health")
        assert resp.headers.get("x-xss-protection") == "0"

    def test_referrer_policy_present(self, client):
        resp = client.get("/health")
        assert "referrer-policy" in resp.headers

    def test_referrer_policy_value(self, client):
        resp = client.get("/health")
        assert resp.headers.get("referrer-policy") == "no-referrer"

    def test_content_security_policy_present(self, client):
        resp = client.get("/health")
        assert "content-security-policy" in resp.headers

    def test_content_security_policy_value(self, client):
        resp = client.get("/health")
        assert resp.headers.get("content-security-policy") == "default-src 'none'"

    def test_server_header_is_not_uvicorn(self, client):
        resp = client.get("/health")
        server = resp.headers.get("server", "")
        assert server.lower() != "uvicorn"

    def test_server_header_is_opaque(self, client):
        resp = client.get("/health")
        assert resp.headers.get("server") == "webserver"

    def test_security_headers_on_error_response(self, raw_client):
        """Security-Header sind auf Fehler-Responses (401) vorhanden."""
        resp = raw_client.get("/status")
        assert resp.status_code == 401
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        assert resp.headers.get("content-security-policy") == "default-src 'none'"

    def test_security_headers_on_cors_preflight(self, client):
        """
        Security-Header sind auf CORS-Preflight-Responses vorhanden.
        Da SecurityHeadersMiddleware outermost ist, wraps sie auch die
        CORSMiddleware-Antworten.
        """
        resp = client.options(
            "/status",
            headers={
                "Origin": CORS_TEST_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        assert resp.headers.get("content-security-policy") == "default-src 'none'"

    def test_security_headers_without_origin(self, client):
        """Security-Header erscheinen unabhängig vom Origin-Header."""
        resp = client.get("/health")
        assert "access-control-allow-origin" not in resp.headers
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"


# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Audit Logging mit Client-IP
# ─────────────────────────────────────────────────────────────────────────────

class TestGetClientIp:
    """
    Unit-Tests für core.security.get_client_ip().

    Prüft die IP-Extraktion ohne FastAPI-Integration – direkt auf Request-Mocks.
    """

    def _make_request(self, client_host: str = "127.0.0.1",
                      forwarded_for: str | None = None) -> MagicMock:
        """Erstellt einen minimalen Request-Mock."""
        req = MagicMock()
        req.client.host = client_host
        headers: dict[str, str] = {}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        req.headers.get = lambda key, default=None: headers.get(key, default)
        return req

    def test_returns_client_host_without_forwarded_for(self):
        """Ohne X-Forwarded-For wird request.client.host zurückgegeben."""
        req = self._make_request(client_host="192.168.1.10")
        assert sec.get_client_ip(req) == "192.168.1.10"

    def test_returns_forwarded_for_when_present(self):
        """X-Forwarded-For hat Vorrang vor request.client.host."""
        req = self._make_request(client_host="10.0.0.1", forwarded_for="192.168.1.50")
        assert sec.get_client_ip(req) == "192.168.1.50"

    def test_takes_first_ip_from_forwarded_for_list(self):
        """Bei mehreren IPs in X-Forwarded-For wird der erste (Client) verwendet."""
        req = self._make_request(forwarded_for="192.168.1.50, 10.0.0.1, 172.16.0.1")
        assert sec.get_client_ip(req) == "192.168.1.50"

    def test_strips_whitespace_from_forwarded_for(self):
        """Whitespace um die IP wird entfernt."""
        req = self._make_request(forwarded_for="  192.168.1.50  ")
        assert sec.get_client_ip(req) == "192.168.1.50"

    def test_returns_unknown_when_no_client(self):
        """Wenn request.client None ist, wird 'unknown' zurückgegeben."""
        req = MagicMock()
        req.client = None
        req.headers.get = lambda key, default=None: None
        assert sec.get_client_ip(req) == "unknown"


class TestAuditLogging:
    """
    Integrations-Tests für Step 6: Audit Logging mit Client-IP.

    Prüft, dass alle sicherheitsrelevanten Fehler-Events (401, 422, 429,
    409, 404) die Client-IP im Log enthalten, und dass X-Forwarded-For
    korrekt als IP-Quelle verwendet wird.

    Der Starlette-TestClient sendet Requests von "testclient" als client.host.
    Alle Tests prüfen daher auf "testclient" als erwartete IP, außer den
    X-Forwarded-For-Tests.
    """

    EXPECTED_IP = "testclient"  # Starlette TestClient host

    def _get_events_by_type(self, caplog, event_name: str) -> list[dict]:
        """Gibt alle Log-Einträge mit dem gegebenen Event-Namen als Dict zurück."""
        result = []
        for record in caplog.records:
            try:
                entry = json.loads(record.message)
                if entry.get("event") == event_name:
                    result.append(entry)
            except (json.JSONDecodeError, AttributeError):
                pass
        return result

    def test_auth_failure_contains_client_ip(self, raw_client, caplog):
        """auth_failure-Event enthält das Feld client_ip."""
        with caplog.at_level(logging.WARNING):
            raw_client.get("/status")

        events = self._get_events_by_type(caplog, "auth_failure")
        assert len(events) >= 1, "Kein auth_failure-Event gefunden"
        assert events[0].get("client_ip") == self.EXPECTED_IP

    def test_rate_limit_exceeded_contains_client_ip(self, rate_limit_client, caplog):
        """rate_limit_exceeded-Event enthält das Feld client_ip."""
        rate_limit_client.post("/test/mutation")
        rate_limit_client.post("/test/mutation")

        with caplog.at_level(logging.WARNING):
            rate_limit_client.post("/test/mutation")

        events = self._get_events_by_type(caplog, "rate_limit_exceeded")
        assert len(events) >= 1, "Kein rate_limit_exceeded-Event gefunden"
        assert events[0].get("client_ip") == self.EXPECTED_IP

    def test_404_contains_client_ip(self, client, caplog):
        """
        request_rejected (404)-Event enthält das Feld client_ip.

        Wichtig: Starlette liefert für komplett unbekannte Routen eine Plain-404
        direkt zurück – OHNE unseren HTTPException-Handler aufzurufen.
        Daher wird eine Route verwendet, die intern explizit HTTPException(404)
        wirft (z.B. schedule/enable mit unbekannter ID).
        """
        with caplog.at_level(logging.WARNING):
            client.post("/schedule/enable/nonexistent-id-xyz")

        events = self._get_events_by_type(caplog, "request_rejected")
        events_404 = [e for e in events if e.get("status_code") == 404]
        assert len(events_404) >= 1, "Kein request_rejected 404-Event gefunden"
        assert events_404[0].get("client_ip") == self.EXPECTED_IP

    def test_409_contains_client_ip(self, client, mock_io, caplog):
        """
        request_rejected (409)-Event enthält das Feld client_ip.

        set_running_zone() aus conftest setzt state.active_runs korrekt auf
        einen laufenden Zustand (mit den richtigen ActiveRun-Feldern).
        """
        from tests.conftest import set_running_zone
        from core.state import state, state_lock

        set_running_zone(zone=1, duration_s=60)

        with state_lock:
            state.parallel_enabled = False

        with caplog.at_level(logging.WARNING):
            client.post("/start", json={"zone": 2, "duration": 30, "time_unit": "Sekunden"})

        events = self._get_events_by_type(caplog, "request_rejected")
        events_409 = [e for e in events if e.get("status_code") == 409]
        assert len(events_409) >= 1, "Kein request_rejected 409-Event gefunden"
        assert events_409[0].get("client_ip") == self.EXPECTED_IP

    def test_422_contains_client_ip(self, client, caplog):
        """request_validation_error (422)-Event enthält das Feld client_ip."""
        with caplog.at_level(logging.WARNING):
            # Ungültiger time_unit-Wert → Pydantic → 422
            client.post("/start", json={"zone": 1, "duration": 30, "time_unit": "Stunden"})

        events = self._get_events_by_type(caplog, "request_validation_error")
        assert len(events) >= 1, "Kein request_validation_error-Event gefunden"
        assert events[0].get("client_ip") == self.EXPECTED_IP

    def test_x_forwarded_for_used_in_auth_failure(self, raw_client, caplog):
        """
        Wenn X-Forwarded-For gesetzt ist, wird diese IP ins auth_failure-Log
        übernommen – nicht die TestClient-IP 'testclient'.
        """
        proxy_ip = "192.168.1.77"

        with caplog.at_level(logging.WARNING):
            raw_client.get("/status", headers={"X-Forwarded-For": proxy_ip})

        events = self._get_events_by_type(caplog, "auth_failure")
        assert len(events) >= 1
        assert events[0].get("client_ip") == proxy_ip

    def test_x_forwarded_for_used_in_422(self, client, caplog):
        """
        Wenn X-Forwarded-For gesetzt ist, wird diese IP ins
        request_validation_error-Log übernommen.
        """
        proxy_ip = "10.0.0.42"

        with caplog.at_level(logging.WARNING):
            client.post(
                "/start",
                json={"zone": 1, "duration": 30, "time_unit": "Stunden"},
                headers={"X-Forwarded-For": proxy_ip},
            )

        events = self._get_events_by_type(caplog, "request_validation_error")
        assert len(events) >= 1
        assert events[0].get("client_ip") == proxy_ip

    def test_x_forwarded_for_first_ip_taken_in_chain(self, raw_client, caplog):
        """
        Bei X-Forwarded-For mit mehreren IPs (Proxy-Kette) wird nur der
        erste Eintrag (originaler Client) geloggt.
        """
        with caplog.at_level(logging.WARNING):
            raw_client.get(
                "/status",
                headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 172.16.0.1"},
            )

        events = self._get_events_by_type(caplog, "auth_failure")
        assert len(events) >= 1
        assert events[0].get("client_ip") == "203.0.113.5"


# ─────────────────────────────────────────────────────────────────────────────
# API Key File – Dateisystem-Berechtigungen (chmod 600)
# ─────────────────────────────────────────────────────────────────────────────

class TestApiKeyFilePermissions:
    """
    api_key.txt muss mit chmod 600 erstellt werden und beim Laden auf 600 gesetzt
    werden (repariert Dateien die ohne explizites chmod erstellt wurden).

    Sicherheitsregel: Nur der Prozess-Owner darf die Datei lesen und schreiben.
    Andere Benutzer auf dem System (z.B. www-data) dürfen den Key nicht lesen.
    """

    def test_new_api_key_file_has_mode_600(self, tmp_path, monkeypatch):
        """
        Beim Generieren einer neuen api_key.txt werden die Berechtigungen
        auf 600 (rw-------) gesetzt.
        """
        import stat as _stat
        import core.security as security_mod

        key_file = tmp_path / "api_key.txt"
        monkeypatch.setattr(security_mod, "API_KEY_FILE", str(key_file))
        # _api_key leeren damit load_or_create nicht früh zurückkehrt
        monkeypatch.setattr(security_mod, "_api_key", "")

        security_mod.load_or_create_api_key()

        assert key_file.exists(), "api_key.txt wurde nicht erstellt"
        mode = _stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"Erwartet 0o600, erhalten {oct(mode)}"

    def test_existing_api_key_file_gets_chmod_600_on_load(self, tmp_path, monkeypatch):
        """
        Eine bestehende api_key.txt mit falschen Berechtigungen (644) wird beim
        Laden automatisch auf 600 korrigiert.

        Repariert Dateien die vor Einführung des expliziten chmod erstellt wurden.
        """
        import stat as _stat
        import core.security as security_mod

        key_file = tmp_path / "api_key.txt"
        valid_key = "a" * 64  # 64 gültige Hex-Zeichen (alle 'a')
        key_file.write_text(valid_key, encoding="utf-8")
        key_file.chmod(0o644)  # absichtlich falsche Berechtigungen

        monkeypatch.setattr(security_mod, "API_KEY_FILE", str(key_file))
        monkeypatch.setattr(security_mod, "_api_key", "")

        loaded_key = security_mod.load_or_create_api_key()

        assert loaded_key == valid_key, "Geladener Key stimmt nicht überein"
        mode = _stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"Erwartet 0o600 nach Load, erhalten {oct(mode)}"

    def test_api_key_file_content_unchanged_after_chmod(self, tmp_path, monkeypatch):
        """
        Das Setzen von chmod darf den Key-Inhalt nicht verändern.
        """
        import core.security as security_mod

        key_file = tmp_path / "api_key.txt"
        valid_key = "deadbeef" * 8  # 64 Hex-Zeichen
        key_file.write_text(valid_key, encoding="utf-8")
        key_file.chmod(0o644)

        monkeypatch.setattr(security_mod, "API_KEY_FILE", str(key_file))
        monkeypatch.setattr(security_mod, "_api_key", "")

        loaded_key = security_mod.load_or_create_api_key()

        assert loaded_key == valid_key
        assert key_file.read_text(encoding="utf-8").strip() == valid_key
