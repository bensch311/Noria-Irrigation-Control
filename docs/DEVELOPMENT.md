# DEVELOPMENT.md – Entwickler-Handbuch

Anleitung für lokale Entwicklung, Tests und Erweiterungen.

---

## 1. Lokale Entwicklungsumgebung

### 1.1 Voraussetzungen

- Python ≥ 3.11
- Kein Raspberry Pi erforderlich – Simulationstreiber (`"sim"`) übernimmt GPIO

### 1.2 Setup

```bash
git clone <REPO-URL> bewaesserung
cd bewaesserung

python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# RPi.GPIO-Zeile auskommentieren (nicht auf Nicht-Pi-Systemen installierbar)
# In requirements.txt: # RPi.GPIO
pip install -r requirements.txt
```

### 1.3 Backend starten (Simulationsmodus)

`data/device_config.json` mit `"IRRIGATION_VALVE_DRIVER": "sim"` anlegen (oder fehlen lassen – Vorlage wird automatisch erstellt):

```bash
ENABLE_DOCS=true uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

`ENABLE_DOCS=true` aktiviert Swagger UI unter `http://127.0.0.1:8000/docs` – nur für lokale Entwicklung. Im Produktionsbetrieb wird `ENABLE_DOCS` nicht gesetzt, Swagger UI ist dann vollständig deaktiviert (404).

`--reload` aktiviert Hot-Reload bei Code-Änderungen (nur für Entwicklung).

### 1.4 Frontend starten

```bash
shiny run app.py --host 127.0.0.1 --port 8080
```

### 1.5 API-Key auslesen

```bash
cat data/api_key.txt
```

### 1.6 SimValveDriver vs. RpiGpioValveDriver

Treiberwahl (Priorität):
1. ENV-Variable `IRRIGATION_VALVE_DRIVER=sim|rpi`
2. `device_config.json` → `IRRIGATION_VALVE_DRIVER`
3. Fallback: `"sim"`

Für lokale Entwicklung immer `"sim"` verwenden. In Tests wird der Treiber via `set_valve_driver()` in `conftest.py` auf `SimValveDriver` gesetzt – unabhängig von der Konfiguration.

---

## 2. Tests

### 2.1 Tests ausführen

```bash
# Alle Tests
pytest

# Mit Coverage-Report
pytest --cov=. --cov-report=term-missing

# Nur ein Modul
pytest tests/test_engine.py

# Nur eine Klasse oder Funktion
pytest tests/test_engine.py::TestCanStartNewValve
pytest tests/test_routes_control.py::test_start_success

# Parallel (schneller, bei unabhängigen Tests)
pytest -n auto

# Verbose
pytest -v
```

### 2.2 Teststruktur und Fixtures

Alle Tests nutzen globale Fixtures aus `tests/conftest.py`:

| Fixture | Art | Zweck |
|---|---|---|
| `clean_state` | autouse | Setzt `RunState` vor/nach jedem Test auf saubere Defaults zurück |
| `sim_driver` | autouse | Setzt `SimValveDriver` als aktiven Treiber (kein GPIO) |
| `mock_io` | autouse | Ersetzt IO-Worker durch `MagicMock` (Hardware-Ops werden nicht ausgeführt) |
| `_patch_api_key` | autouse | Setzt `TEST_API_KEY` in `core.security._api_key` |
| `client` | opt-in | FastAPI `TestClient` mit Auth-Header (verwendet `TEST_API_KEY`) |
| `raw_client` | opt-in | FastAPI `TestClient` ohne Auth-Header (für Auth-Tests) |
| `failing_io` | opt-in | IO-Worker schlägt immer fehl (zum Testen von Fehlerbehandlung) |

**Warum kein Lifespan in Tests?**  
Der `client` verwendet `TestClient(app, raise_server_exceptions=True)` ohne Lifespan. Startup/Shutdown-Seiteneffekte (GPIO, Threads) sind in Unit-Tests unerwünscht. State wird durch die Autouse-Fixtures manuell in den gewünschten Ausgangszustand gesetzt.

### 2.3 Rate-Limiting in Tests

Das `app`-Fixture in `conftest.py` erstellt pro Test eine **neue** Limiter-Instanz mit leerer Storage. Damit akkumulieren Rate-Limit-Zähler nicht über Tests hinweg (kein Test-Ordering-Problem).

```python
# conftest.py – App-Fixture mit frischem Limiter
@pytest.fixture
def app():
    fresh_limiter = Limiter(key_func=get_remote_address, default_limits=[GLOBAL_LIMIT])
    test_app = FastAPI()
    test_app.state.limiter = fresh_limiter
    # ...
```

### 2.4 IO-Worker in Tests mocken

Der `mock_io`-Fixture setzt einen `MagicMock` als globalen IO-Worker. Standard-Rückgabe: `IOResult(success=True)`.

```python
# Test mit erfolgreichem IO (Standard)
def test_start_success(client, mock_io):
    resp = client.post("/start", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 200
    # Prüfen was an den IO-Worker gesendet wurde:
    cmd = mock_io.send_command.call_args[0][0]
    assert cmd.action == "open"
    assert cmd.zone == 1

# Test mit fehlschlagendem IO
def test_start_hw_failure(client, failing_io):
    resp = client.post("/start", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 503
```

---

## 3. Neue API-Route hinzufügen – Checkliste

```
□ 1. Pydantic-Request-Modell in models/requests.py definieren
□ 2. Route-Funktion in passendem api/routes_*.py anlegen
□ 3. @limiter.limit(MUTATION_LIMIT) für POST/DELETE hinzufügen
□ 4. Depends(require_api_key) sicherstellen (bereits im Router als Default)
□ 5. log_event() aufrufen mit sinnvollem Event-Namen und relevanten Feldern
□ 6. HTTP-Status-Codes gemäß Fehlerformat dokumentieren (→ API_REFERENCE.md)
□ 7. Tests schreiben: Erfolgsfall, Fehlerfall, Auth-Fehler, Rate-Limit
□ 8. Event-Namen in OPERATIONS.md Log-Events-Referenz ergänzen
□ 9. Endpoint in API_REFERENCE.md dokumentieren
```

### Minimales Beispiel: neue GET-Route

```python
# api/routes_control.py (oder neue Datei)
@router.get("/my-endpoint")
def my_endpoint():
    with state_lock:
        value = state.some_field
    log_event("my_endpoint_read", source="api", value=value)
    return {"value": value}
```

### Minimales Beispiel: neue POST-Route mit Mutation

```python
# models/requests.py
class MyRequest(BaseModel):
    zone: int = Field(..., ge=1)
    value: str = Field(..., min_length=1, max_length=100)

# api/routes_control.py
@router.post("/my-mutation")
@limiter.limit(MUTATION_LIMIT)
def my_mutation(request: Request, req: MyRequest):
    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))
    
    if req.zone > max_v:
        raise HTTPException(status_code=400, detail=f"zone muss 1..{max_v} sein.")
    
    # Prepare-Execute-Commit falls Hardware-Op nötig...
    
    with state_lock:
        state.some_field = req.value
        state.queue_dirty = True  # falls persistent
    
    log_event("my_mutation", source="api", zone=req.zone, value=req.value)
    return {"ok": True}
```

---

## 4. Neues Zeitplan-Regelfeld hinzufügen

Wenn `ScheduleRule` um ein neues Feld erweitert wird, müssen **zwingend** beide Stellen synchron geändert werden:

```
□ core/state.py          → Feld zu ScheduleRule-Dataclass hinzufügen (mit Default)
□ services/persistence.py → _serialize_schedule() und _deserialize_schedule() anpassen
□ models/requests.py      → ScheduleAddRequest anpassen (falls User-eingabe)
□ api/routes_schedule.py  → ScheduleRule-Konstruktor in schedule_add() anpassen
□ tests/test_persistence.py → Serialisierungs-Tests aktualisieren
```

**Warum immer einen Default?**  
Bestehende `schedules.json`-Dateien auf dem Produktions-Pi kennen das neue Feld nicht. `_deserialize_schedule()` verwendet `d.get("feld", default)`, daher muss der Default sinnvoll und sicher sein.

---

## 5. Folder-Struktur-Konventionen

```
api/         – HTTP-Schicht: Router, Middleware, Error-Handler. Keine Business-Logik.
core/        – Kernmodule: Config, State, Security, Logging, Limiter, Lifecycle.
             – Keine Hardware-Abhängigkeiten (kein RPi.GPIO-Import).
models/      – Pydantic-Modelle für Request-Validierung.
services/    – Business-Logik und Hardware-Services.
             – Einzige Schicht die Hardware-Operationen ausführt (via io_worker).
tests/       – Pytest-Tests. Spiegel der Hauptstruktur.
data/        – Laufzeit-Daten. Nicht in Git. Werden beim Start erstellt.
logs/        – Log-Dateien. Nicht in Git.
www/         – Statische Frontend-Assets (CSS, Logo).
```

---

## 6. Test-Konventionen

- Jede neue Funktion erhält mindestens: Erfolgsfall, Fehlerfall (400/409/503), Auth-Fehler (401)
- Schreibende Operationen: IO-Worker-Call mit `mock_io.send_command.call_args` prüfen
- State-Assertions immer unter `state_lock`
- Kein `time.sleep()` in Tests (verwendet `time.monotonic()` mit Offset stattdessen)
- Hardware-Fault-Tests: `failing_io`-Fixture verwenden
- Rate-Limit-Tests: eigene App-Fixture mit niedrigem Limit (nicht die Standard-App nutzen)

---

## 7. `.gitignore` – Was nicht in Git gehört

```gitignore
# Laufzeit-Daten
data/
logs/

# Python
__pycache__/
*.pyc
.venv/
*.egg-info/

# Test-Artefakte
.pytest_cache/
.coverage
htmlcov/

# IDE
.vscode/
.idea/
```
