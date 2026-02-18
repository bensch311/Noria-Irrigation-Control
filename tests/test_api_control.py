def test_status_initial(client):
    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running_zone"] is None
    assert data["paused"] is False


def test_start_pause_resume_stop_flow(client):
    # start
    r = client.post("/start", json={"zone": 1, "duration": 10, "time_unit": "Sekunden"})
    assert r.status_code == 200

    # status running
    r = client.get("/status")
    assert r.status_code == 200
    s = r.json()
    assert s["running_zone"] == 1
    assert 1 in s["running_zones"]

    # pause
    r = client.post("/pause")
    assert r.status_code == 200

    r = client.get("/status")
    s = r.json()
    assert s["paused"] is True

    # resume
    r = client.post("/resume")
    assert r.status_code == 200

    r = client.get("/status")
    s = r.json()
    assert s["paused"] is False
    assert s["running_zone"] == 1

    # stop
    r = client.post("/stop")
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True

    r = client.get("/status")
    s = r.json()
    assert s["running_zone"] is None
    assert s["running_zones"] == []
