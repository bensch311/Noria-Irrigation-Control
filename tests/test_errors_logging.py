import api.errors as api_errors


def test_http_409_is_logged_as_request_rejected(client, monkeypatch):
    calls = []

    def fake_log_event(event: str, **fields):
        calls.append((event, fields))

    monkeypatch.setattr(api_errors, "log_event", fake_log_event)

    # /pause ohne laufendes Ventil -> 409 (in routes_control.py)
    r = client.post("/pause")
    assert r.status_code == 409

    # error handler soll request_rejected loggen für 409 (REJECT_LOG_STATUS_CODES enthält 409)
    assert any(ev == "request_rejected" for ev, _ in calls), calls


def test_422_validation_error_is_logged(client, monkeypatch):
    calls = []

    def fake_log_event(event: str, **fields):
        calls.append((event, fields))

    monkeypatch.setattr(api_errors, "log_event", fake_log_event)

    # /start verlangt duration>=1. Wir senden duration=0 -> RequestValidationError -> 422
    r = client.post("/start", json={"zone": 1, "duration": 0, "time_unit": "Sekunden"})
    assert r.status_code == 422

    assert any(ev == "request_validation_error" for ev, _ in calls), calls
