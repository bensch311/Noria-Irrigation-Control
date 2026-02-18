def test_schedule_add_and_get(client):
    payload = {
        "zone": 1,
        "weekdays": [0],          # Montag
        "start_times": ["06:30"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    r = client.post("/schedule/add", json=payload)
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert "id" in out

    r = client.get("/schedule")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["items"][0]["zone"] == 1


def test_schedule_add_rejects_bad_time_format(client):
    payload = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["6:3"],   # invalid
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    r = client.post("/schedule/add", json=payload)
    assert r.status_code == 400
