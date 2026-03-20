"""
Gemeinsame Fixtures und Hilfsfunktionen für alle Tests.

Autouse-Fixtures sorgen vor/nach jedem Test für:
  - sauberen RunState (clean_state)
  - SimValveDriver als Valve-Driver (sim_driver)
  - SimSensorDriver als Sensor-Driver (sim_sensor_driver)
  - MagicMock als IO-Worker (mock_io)
  - bekannten API-Key in core.security (_patch_api_key)

Opt-in Fixtures:
  - failing_io  : IO-Worker schlägt immer fehl
  - client      : FastAPI-TestClient ohne Lifespan, mit Auth-Header

Konstanten:
  - TEST_API_KEY    : Der für alle Tests verwendete API-Key (64 Hex-Zeichen).
                      Kann in Sicherheitstests über Header-Override überschrieben werden.
  - CORS_TEST_ORIGIN: Die für CORS-Tests konfigurierte erlaubte Origin.
                      Wird im app-Fixture als einzige allow_origin gesetzt.

Rate-Limiting-Hinweis:
  Das app-Fixture erstellt pro Test einen NEUEN Limiter (eigene leere Storage).
  Damit akkumulieren Rate-Limit-Zähler nicht über Tests hinweg.
"""

import time
import uuid
from dataclasses import fields as dc_fields

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from unittest.mock import MagicMock

from api.middleware import SecurityHeadersMiddleware
from core.state import (
    state,
    state_lock,
    RunState,
    ActiveRun,
    QueueItem,
    ScheduleRule,
    HistoryItem,
)
from services.io_worker import IOResult, IOWorker, set_io_worker, reset_io_worker
from services.valve_driver import SimValveDriver, set_valve_driver, reset_valve_driver
from services.sensor_driver import SimSensorDriver, set_sensor_driver, reset_sensor_driver

# ---------------------------------------------------------------------------
# Test-API-Key: 64 gültige Hex-Zeichen (256 bit), fest für alle Tests.
# Dieser Key wird in _patch_api_key in core.security._api_key injiziert
# und vom client-Fixture automatisch als Header gesendet.
# ---------------------------------------------------------------------------
TEST_API_KEY = "deadbeef" * 8  # 64 Hex-Zeichen

# ---------------------------------------------------------------------------
# CORS-Test-Origin: die einzige erlaubte Origin in der Test-App.
# In CORS-Tests wird dieser Wert als Origin-Header gesendet (→ erlaubt)
# oder eine abweichende Origin (→ blockiert).
# ---------------------------------------------------------------------------
CORS_TEST_ORIGIN = "http://test.example.com"


# ─────────────────────────────────────────────────────────────────────────────
# State-Reset
# ─────────────────────────────────────────────────────────────────────────────


def reset_global_state() -> None:
    """
    Setzt den gesamten RunState atomar auf saubere Defaults zurück.

    Implementierung via dataclasses.fields():
    - Erzeugt eine frische RunState()-Instanz (enthält alle korrekten Defaults)
    - Kopiert jeden Feldwert via setattr auf den globalen state
    - Garantiert korrekte Feldnamen und Default-Werte, auch nach Refactoring
    - Collections (None-Default in Dataclass) werden für Tests als leere
      Instanzen gesetzt, damit Tests direkt append() etc. nutzen können

    Warum nicht direkt ``state = RunState()``?
    - Der globale `state` wird von allen Modulen importiert. Eine Neuzuweisung
      würde nur die lokale Referenz in conftest ersetzen, nicht den importierten
      Singleton in den Produktions-Modulen.
    """
    fresh = RunState()
    with state_lock:
        for f in dc_fields(fresh):
            setattr(state, f.name, getattr(fresh, f.name))

        # Collections haben in RunState None als Default (mypy-freundlich).
        # Für Tests setzen wir leere Instanzen, damit Tests direkt
        # state.queue.append() usw. nutzen können ohne None-Guards.
        state.queue = []
        state.schedules = []
        state.active_runs = {}
        state.run_history = []

        # gpio_pins_by_zone: None bedeutet "noch nicht geladen" – im Test-Kontext
        # ist ein leeres Dict das sinnvollere Default (SimDriver benötigt keine Pins).
        if state.gpio_pins_by_zone is None:
            state.gpio_pins_by_zone = {}

        # sensor_gpio_pins_by_zone: analog zu gpio_pins_by_zone.
        if state.sensor_gpio_pins_by_zone is None:
            state.sensor_gpio_pins_by_zone = {}

        # Sensor-Laufzeit-Dicts explizit initialisieren damit Tests
        # ohne None-Guards direkt schreiben können.
        state.sensor_readings = {}
        state.sensor_last_triggered = {}


# ─────────────────────────────────────────────────────────────────────────────
# Autouse-Fixtures (laufen vor/nach JEDEM Test)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_state():
    """Setzt den globalen RunState vor und nach jedem Test zurück."""
    reset_global_state()
    yield
    reset_global_state()


@pytest.fixture(autouse=True)
def sim_driver():
    """Setzt SimValveDriver als aktiven Valve-Driver für alle Tests."""
    driver = SimValveDriver()
    set_valve_driver(driver)
    yield driver
    reset_valve_driver()


@pytest.fixture(autouse=True)
def sim_sensor_driver():
    """Setzt SimSensorDriver als aktiven Sensor-Driver für alle Tests."""
    driver = SimSensorDriver()
    set_sensor_driver(driver)
    yield driver
    reset_sensor_driver()


@pytest.fixture(autouse=True)
def mock_io():
    """
    Ersetzt den globalen IO-Worker durch einen MagicMock für alle Tests.

    Standard-Rückgabe: IOResult(success=True, duration_ms=1.0)
    """
    worker = MagicMock(spec=IOWorker)
    worker.send_command.return_value = IOResult(success=True, duration_ms=1.0)
    worker._started = True
    set_io_worker(worker)
    yield worker
    reset_io_worker()


@pytest.fixture(autouse=True)
def _patch_api_key(monkeypatch):
    """
    Setzt core.security._api_key auf TEST_API_KEY für die Dauer jedes Tests.

    Damit ist die Security-Dependency in allen Tests aktiv, aber mit einem
    bekannten Key – kein Disk-Zugriff, kein echter Startup nötig.

    Tests die Auth-Fehler prüfen wollen, senden einfach einen falschen oder
    leeren X-API-Key-Header (das Default-Header im client-Fixture kann per
    request-Zeit-Header überschrieben werden).
    """
    import core.security as sec
    monkeypatch.setattr(sec, "_api_key", TEST_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def failing_io(mock_io):
    """
    Überschreibt mock_io so, dass jeder send_command-Aufruf fehlschlägt.
    Gibt die konfigurierte Zone aus dem Command zurück.
    """

    def _fail(cmd, timeout_s=5.0):
        return IOResult(success=False, zone=cmd.zone, error="GPIO Fehler", duration_ms=1.0)

    mock_io.send_command.side_effect = _fail
    yield mock_io
    mock_io.send_command.side_effect = None


@pytest.fixture
def app():
    """
    FastAPI-App *ohne* Lifespan – für schnelle Route-Tests.

    Middleware-Stack (spiegelt Produktion exakt):
      Client → SecurityHeadersMiddleware → CORSMiddleware → SlowAPIMiddleware → Route-Handler

    SecurityHeadersMiddleware als äußerste Schicht: setzt Security-Header auf
    alle Responses, inkl. CORS-Preflights und Fehlerantworten.

    CORSMiddleware verwendet CORS_TEST_ORIGIN als einzige erlaubte Origin,
    damit CORS-Tests ohne Umgebungsvariablen-Manipulation funktionieren.

    Rate-Limiting:
    Jeder Test bekommt eine NEUE Limiter-Instanz mit leerer in-memory Storage.
    Damit akkumulieren Rate-Limit-Zähler nicht über Tests hinweg.
    """
    from api.errors import register_error_handlers
    from api.routes_health import router as health_router
    from api.routes_system import router as system_router
    from api.routes_queue import router as queue_router
    from api.routes_schedule import router as schedule_router
    from api.routes_control import router as control_router
    from api.routes_history import router as history_router
    from api.routes_settings import router as settings_router
    from api.routes_sensors import router as sensors_router

    # Frische Limiter-Instanz pro Test – identische Konfiguration wie Produktion.
    test_limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])

    _app = FastAPI()
    _app.state.limiter = test_limiter

    # Middleware-Reihenfolge: zuletzt hinzugefügt = outermost = verarbeitet zuerst.
    # SlowAPIMiddleware zuerst (innermost), CORSMiddleware als zweites,
    # SecurityHeadersMiddleware zuletzt (outermost) – spiegelt Produktion exakt.
    _app.add_middleware(SlowAPIMiddleware)
    _app.add_middleware(
        CORSMiddleware,
        allow_origins=[CORS_TEST_ORIGIN],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key"],
    )
    _app.add_middleware(SecurityHeadersMiddleware)

    register_error_handlers(_app)
    _app.include_router(health_router)
    _app.include_router(system_router)
    _app.include_router(queue_router)
    _app.include_router(schedule_router)
    _app.include_router(control_router)
    _app.include_router(history_router)
    _app.include_router(settings_router)
    _app.include_router(sensors_router)
    return _app


@pytest.fixture
def client(app):
    """
    Starlette-TestClient; Exceptions aus Handlern werden als Responses zurückgegeben.

    Sendet automatisch den TEST_API_KEY als X-API-Key-Header, damit alle
    bestehenden Tests ohne Änderung weiter funktionieren.

    Für Tests die Auth-Fehler prüfen wollen:
        resp = client.get("/status", headers={"X-API-Key": ""})      # kein Key
        resp = client.get("/status", headers={"X-API-Key": "wrong"}) # falscher Key
    """
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-API-Key": TEST_API_KEY},
    ) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Factory-Fixtures  (erzeugen Objekte, injizieren keine State-Änderungen)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def make_active_run():
    """Factory: erzeugt einen ActiveRun mit realistischen Defaults."""

    def _factory(
        zone: int = 1,
        duration_s: int = 60,
        source: str = "manual",
        elapsed: bool = False,
    ) -> ActiveRun:
        now = time.monotonic()
        end_time = (now - 1.0) if elapsed else (now + duration_s)
        return ActiveRun(
            zone=zone,
            end_time=end_time,
            time_unit="Sekunden",
            started_at=now - (duration_s if elapsed else 0),
            started_source=source,
            started_planned_s=duration_s,
        )

    return _factory


@pytest.fixture
def make_schedule():
    """Factory: erzeugt eine ScheduleRule mit sinnvollen Defaults."""

    def _factory(
        zone: int = 1,
        weekdays: list | None = None,
        start_times: list | None = None,
        duration_s: int = 60,
        repeat: bool = True,
        enabled: bool = True,
        rule_id: str | None = None,
    ) -> ScheduleRule:
        days = weekdays if weekdays is not None else list(range(7))
        times = start_times or ["06:00"]
        return ScheduleRule(
            id=rule_id or str(uuid.uuid4())[:8],
            zone=zone,
            weekdays=days,
            start_times=times,
            duration_s=duration_s,
            time_unit="Sekunden",
            repeat=repeat,
            enabled=enabled,
            once_pending=(
                None
                if repeat
                else [f"{d} {t}" for d in days for t in times]
            ),
        )

    return _factory


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion (kein Fixture – direkter Import in Tests)
# ─────────────────────────────────────────────────────────────────────────────


def set_running_zone(zone: int, duration_s: int = 60, source: str = "manual"):
    """
    Setzt state.active_runs auf einen laufenden Zustand.
    Muss OHNE state_lock aufgerufen werden (holt Lock intern).
    """
    now = time.monotonic()
    ar = ActiveRun(
        zone=zone,
        end_time=now + duration_s,
        time_unit="Sekunden",
        started_at=now,
        started_source=source,
        started_planned_s=duration_s,
    )
    with state_lock:
        state.active_runs = {zone: ar}
