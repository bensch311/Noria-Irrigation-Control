from core.state import state, state_lock


def test_parallel_disable_blocks_new_queue_starts_until_empty(client):
    # Parallel an + max 2
    r = client.post("/parallel", json={"enabled": True})
    assert r.status_code == 200

    # Start 2 Ventile manuell (soll gehen, max_concurrent_valves ist im State reset fixture 1,
    # aber parallel_enabled=True; du setzt max_concurrent_valves über runtime_state,
    # in Tests lassen wir es minimal: erst direkt im state setzen)
    with state_lock:
        state.max_concurrent_valves = 2

    r = client.post("/start", json={"zone": 1, "duration": 30, "time_unit": "Sekunden"})
    assert r.status_code == 200
    r = client.post("/start", json={"zone": 2, "duration": 30, "time_unit": "Sekunden"})
    assert r.status_code == 200

    # Parallel aus (während 2 laufen)
    r = client.post("/parallel", json={"enabled": False})
    assert r.status_code == 200

    # Queue füllen (3. Ventil)
    r = client.post("/queue/add", json={"zone": 3, "duration": 10, "time_unit": "Sekunden"})
    assert r.status_code == 200

    # queue/start darf NICHT starten, weil parallel aus und active_runs != 0
    r = client.post("/queue/start")
    assert r.status_code == 200

    r = client.get("/status")
    s = r.json()
    assert sorted(s["running_zones"]) == [1, 2]
    assert 3 not in s["running_zones"]

    # Stop alle -> dann erst darf queue starten
    r = client.post("/stop")
    assert r.status_code == 200

    # queue ist noch da? (queue/start poppt nur, wenn gestartet wird)
    r = client.get("/queue")
    q = r.json()
    assert q["queue_length"] == 1
    assert q["items"][0]["zone"] == 3

    # queue/start jetzt -> startet zone 3
    r = client.post("/queue/start")
    assert r.status_code == 200

    r = client.get("/status")
    s = r.json()
    assert s["running_zone"] == 3
    assert 3 in s["running_zones"]

    client.post("/stop")
