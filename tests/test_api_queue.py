def test_queue_add_and_start(client):
    # add
    r = client.post("/queue/add", json={"zone": 1, "duration": 5, "time_unit": "Sekunden"})
    assert r.status_code == 200

    r = client.get("/queue")
    q = r.json()
    assert q["queue_length"] == 1
    assert q["items"][0]["zone"] == 1

    # start queue -> startet direkt Ventil (weil parallel_enabled=False und nichts läuft)
    r = client.post("/queue/start")
    assert r.status_code == 200

    # queue sollte nun leer sein (Item wurde entnommen)
    r = client.get("/queue")
    q = r.json()
    assert q["queue_length"] == 0

    # status: Ventil läuft
    r = client.get("/status")
    s = r.json()
    assert s["running_zone"] == 1
    assert 1 in s["running_zones"]

    # cleanup
    r = client.post("/stop")
    assert r.status_code == 200


def test_queue_clear(client):
    client.post("/queue/add", json={"zone": 1, "duration": 5, "time_unit": "Sekunden"})
    r = client.post("/queue/clear")
    assert r.status_code == 200
    out = r.json()
    assert out["queue_length"] == 0
