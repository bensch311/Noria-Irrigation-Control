import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.errors import register_error_handlers
from api.routes_health import router as health_router
from api.routes_queue import router as queue_router
from api.routes_schedule import router as schedule_router
from api.routes_control import router as control_router
from api.routes_history import router as history_router
import api.routes_control as routes_control

from core.state import state, state_lock
from services.valve_driver import SimValveDriver, set_valve_driver, reset_valve_driver


def build_test_app() -> FastAPI:
    """
    Test-App ohne lifespan (keine Background-Threads, keine failsafe close_all auf Startup).
    Router/Errors entsprechen main.py.
    """
    app = FastAPI()
    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(queue_router)
    app.include_router(schedule_router)
    app.include_router(control_router)
    app.include_router(history_router)
    return app


@pytest.fixture()
def app() -> FastAPI:
    return build_test_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state_between_tests():
    """
    Harte Rücksetzung des globalen In-Memory-State vor JEDEM Test.
    Wichtig, weil dein Projekt globalen state nutzt.
    """
    with state_lock:
        # runtime
        state.active_runs = {}
        state.running_zone = None
        state.end_time = 0.0
        state.paused = False
        state.remaining_s = 0
        state.started_at = 0.0
        state.started_source = "manual"
        state.started_planned_s = 0
        state.paused_at = 0.0
        state.paused_total_s = 0.0

        # queue
        state.queue = []
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.queue_dirty = False

        # schedules/history
        state.schedules = []
        state.schedules_dirty = False
        state.run_history = []
        state.history_dirty = False

        # parallel defaults für deterministische Tests
        state.parallel_enabled = False
        state.max_concurrent_valves = 1
        state.parallel_drain_logged = False

        # limits/device defaults (falls Tests damit arbeiten)
        state.max_valves = int(getattr(state, "max_valves", 6))

        # automation
        state.automation_enabled = True
        state.automation_block_run_key = None

    # Driver immer auf Sim festnageln (keine GPIO Zugriffe)
    reset_valve_driver()
    set_valve_driver(SimValveDriver())

    yield

    # Cleanup (falls ein Test etwas “hängen lässt”)
    reset_valve_driver()

@pytest.fixture(autouse=True)
def disable_disk_persistence_in_tests(monkeypatch):
    """
    In Integrationstests keine Disk-I/O durch Runtime-Persistenz (parallel toggles etc.).
    """
    monkeypatch.setattr(routes_control, "save_runtime_state_to_disk", lambda: None)
    yield