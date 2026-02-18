from services.valve_driver import BaseValveDriver, ValveDriverError, set_valve_driver
from core.state import state_lock


class FailingOpenDriver(BaseValveDriver):
    name = "fail_open"

    def open(self, zone: int) -> None:
        raise ValveDriverError("simulated open failure")

    def close(self, zone: int) -> None:
        return None

    def close_all(self) -> None:
        return None


class FailingCloseDriver(BaseValveDriver):
    name = "fail_close"

    def open(self, zone: int) -> None:
        return None

    def close(self, zone: int) -> None:
        raise ValveDriverError("simulated close failure")

    def close_all(self) -> None:
        return None


def test_start_returns_503_on_open_failure(client):
    set_valve_driver(FailingOpenDriver())

    r = client.post("/start", json={"zone": 1, "duration": 10, "time_unit": "Sekunden"})
    assert r.status_code == 503
    # detail ist string in engine.py open error -> HTTPException 503
    assert "Hardware Fehler" in str(r.json()["detail"]) or "Hardware" in str(r.json()["detail"])


def test_pause_returns_503_on_close_failure(client):
    # Erst mit einem Driver starten, der open kann (Sim ist im conftest schon gesetzt)
    r = client.post("/start", json={"zone": 1, "duration": 10, "time_unit": "Sekunden"})
    assert r.status_code == 200

    # Jetzt close kaputt machen -> pause muss 503 liefern
    set_valve_driver(FailingCloseDriver())

    r = client.post("/pause")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "Hardware Fehler" in str(detail) or "failed" in str(detail)

    # Cleanup: stop kann ebenfalls close brauchen; dafür wieder Sim setzen (conftest macht das pro Test ohnehin)
