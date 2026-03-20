# app/api/routes_sensors.py
"""
Sensor-Endpunkte: Lesezustand, Konfiguration und Simulations-Steuerung.

GET  /sensors/readings    – Aktueller Feuchtezustand aller konfigurierten Sensor-Zonen
GET  /sensors/config      – Aktive Sensor-Konfiguration inkl. GPIO-Validierung
POST /sensors/sim/set     – Sim-only: Zonen manuell auf trocken/feucht setzen

GET-Endpunkte sind read-only und liefern ausschließlich State-Snapshots.
Die Bewertungslogik (wann eine Zone bewässert wird) liegt vollständig in
services/sensor_engine.py.

Besonderheit von /sensors/readings:
  sensor_readings im State wird von sensor_engine_loop() befüllt – erst nach
  dem ersten Polling-Zyklus (frühestens sensor_polling_interval_s Sekunden
  nach dem Systemstart). Vorher ist sensor_readings None bzw. ein leeres Dict.
  Der Endpunkt gibt in diesem Fall eine leere readings-Map zurück und setzt
  polled_at=null. Das Frontend muss diesen Zustand explizit behandeln.

  last_triggered enthält die letzte bekannte Sensor-Auslösung pro Zone als
  Unix-Timestamp (time.monotonic()-basiert, daher für Anzeigezwecke die
  elapsed_s-Angabe nutzen – kein absoluter Zeitstempel möglich ohne Wanduhr).

Besonderheit von /sensors/config:
  gpio_validation wird – analog zu GET /health für Ventile – nur im
  Modus "rpi_switch" durchgeführt. Im Sim-Modus ist sie per Definition gültig.

Besonderheit von POST /sensors/sim/set:
  Nur verfügbar wenn sensor_driver_mode == "sim" (404 sonst).
  Schreibt direkt in den SimSensorDriver-Singleton – der nächste Polling-
  Zyklus des sensor_engine_loop liest den gesetzten Zustand und stellt
  ggf. Queue-Items ein. Dient ausschließlich zum manuellen Testen des
  End-to-End-Pfades ohne echte Hardware.
"""

from fastapi import APIRouter, Depends, HTTPException
import time

from core.state import state, state_lock
from core.security import require_api_key
from core.logging import log_event
from core.limiter import limiter, MUTATION_LIMIT
from services.sensor_driver import get_sensor_driver, validate_sensor_pins, SimSensorDriver
from models.requests import SimSensorSetRequest

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/sensors/readings")
def get_sensor_readings():
    """Gibt den aktuellen Feuchtezustand aller konfigurierten Sensor-Zonen zurück.

    Response-Felder:
      sensor_driver     – Name des aktiven Sensor-Treibers ("sim" | "rpi_switch")
      zones_configured  – sortierte Liste der konfigurierten Sensor-Zonen
      readings          – {zone: needs_irrigation} – True = trocken, False = feucht.
                          Leer wenn noch kein Polling-Zyklus gelaufen ist.
      cooldown_s        – konfigurierter Cooldown in Sekunden
      last_triggered    – {zone: elapsed_s} – Sekunden seit letztem Sensor-Trigger.
                          Nur für Zonen mit bekanntem Trigger-Zeitpunkt vorhanden.
                          null wenn noch nie getriggert.
      polling_interval_s – konfiguriertes Polling-Intervall in Sekunden
    """
    # Sensor-Driver-Name vor dem Lock lesen – get_sensor_driver() kann intern
    # state_lock anfordern (_read_sensor_settings_from_state), was innerhalb
    # eines nicht-re-entranten Locks zum Deadlock führen würde.
    driver_name = get_sensor_driver().name

    now_m = time.monotonic()

    with state_lock:
        pins = dict(state.sensor_gpio_pins_by_zone or {})
        readings_raw = dict(state.sensor_readings or {})
        last_triggered_raw = dict(state.sensor_last_triggered or {})
        cooldown_s = int(getattr(state, "sensor_cooldown_s", 600))
        polling_interval_s = int(getattr(state, "sensor_polling_interval_s", 30))

    zones_configured = sorted(int(z) for z in pins.keys())

    # needs_irrigation-Werte auf bool normieren
    readings: dict[str, bool] = {
        str(z): bool(v) for z, v in readings_raw.items()
    }

    # Elapsed-Sekunden seit letztem Trigger pro Zone berechnen.
    # Nur Zonen mit bekanntem Timestamp werden ausgegeben (kein null-Eintrag für ungetriggerte).
    last_triggered: dict[str, float | None] = {}
    for zone in zones_configured:
        ts = last_triggered_raw.get(zone)
        if ts is not None:
            last_triggered[str(zone)] = round(now_m - ts, 1)

    return {
        "sensor_driver":       driver_name,
        "zones_configured":    zones_configured,
        "readings":            readings,
        "cooldown_s":          cooldown_s,
        "last_triggered":      last_triggered,
        "polling_interval_s":  polling_interval_s,
    }


@router.get("/sensors/config")
def get_sensor_config():
    """Gibt die aktive Sensor-Konfiguration zurück.

    Response-Felder:
      sensor_driver          – Name des aktiven Treibers ("sim" | "rpi_switch")
      configured_driver_mode – konfigurierter Modus aus device_config.json
      sensor_internal_pull_up – True = interner Pi-Pull-Up aktiv (~50kΩ)
      zones_configured       – sortierte Liste der Zonen mit Sensor
      polling_interval_s     – Polling-Intervall in Sekunden
      cooldown_s             – Mindestabstand zwischen zwei Sensor-Triggern (Sekunden)
      default_duration_s     – Standard-Bewässerungsdauer bei Sensor-Trigger (Sekunden)
      gpio_validation        – Validierungsergebnis der Sensor-Pins (nur bei rpi_switch)
      gpio_config_valid      – True wenn keine ungültigen/doppelten Sensor-Pins
    """
    # Driver-Name vor dem Lock – identische Begründung wie in get_sensor_readings()
    driver_name = get_sensor_driver().name

    with state_lock:
        mode = str(getattr(state, "sensor_driver_mode", "sim")).strip().lower()
        pins = dict(state.sensor_gpio_pins_by_zone or {})
        internal_pull_up = bool(getattr(state, "sensor_internal_pull_up", False))
        polling_interval_s = int(getattr(state, "sensor_polling_interval_s", 30))
        cooldown_s = int(getattr(state, "sensor_cooldown_s", 600))
        default_duration_s = int(getattr(state, "sensor_default_duration_s", 300))

    zones_configured = sorted(int(z) for z in pins.keys())

    # GPIO-Validierung nur im rpi_switch-Modus sinnvoll.
    # Im Sim-Modus gibt es keine echten Pins → Validierung immer OK.
    if mode == "rpi_switch":
        pins_int: dict[int, int] = {}
        for k, v in pins.items():
            try:
                pins_int[int(k)] = int(v)
            except Exception:
                pass
        gpio_validation = validate_sensor_pins(pins_int)
        gpio_config_valid = bool(gpio_validation.get("ok"))
    else:
        gpio_validation = {"ok": True, "invalid_pins": [], "duplicate_pins": []}
        gpio_config_valid = True

    return {
        "sensor_driver":          driver_name,
        "configured_driver_mode": mode,
        "sensor_internal_pull_up": internal_pull_up,
        "zones_configured":       zones_configured,
        "polling_interval_s":     polling_interval_s,
        "cooldown_s":             cooldown_s,
        "default_duration_s":     default_duration_s,
        "gpio_config_valid":      gpio_config_valid,
        "invalid_pins":           gpio_validation.get("invalid_pins", []),
        "duplicate_pins":         gpio_validation.get("duplicate_pins", []),
    }


@router.post("/sensors/sim/set")
@limiter.limit(MUTATION_LIMIT)
def sim_set_sensor_state(request, req: SimSensorSetRequest):
    """Setzt Sensor-Zonen im Sim-Modus manuell auf trocken oder feucht.

    NUR im Sim-Modus verfügbar (sensor_driver_mode == "sim").
    Im Produktionsmodus (rpi_switch) antwortet der Endpunkt mit 404 –
    es gibt keine Möglichkeit, Hardware-Readings zu überschreiben.

    Funktionsweise:
      - Schreibt direkt in den SimSensorDriver-Singleton via set_zone_dry() /
        set_zone_moist().
      - Der nächste sensor_engine_loop-Zyklus liest den gesetzten Zustand
        und stellt ggf. ein QueueItem ein (wenn Cooldown abgelaufen).
      - Ideal zum End-to-End-Test des vollständigen Sensor→Queue→Ventil-Pfads
        ohne echte Hardware.

    Request-Felder:
      dry_zones   – Zonen auf "trocken" setzen (needs_irrigation=True)
      moist_zones – Zonen auf "feucht" setzen  (needs_irrigation=False)

    Response-Felder:
      ok          – immer True bei Erfolg
      dry_zones   – welche Zonen jetzt als trocken gelten (aus dem Driver)
      moist_zones – welche Zonen jetzt als feucht gelten
    """
    # Modus-Guard: 404 wenn kein Sim-Driver aktiv.
    # Geprüft wird sensor_driver_mode im State (Konfiguration), nicht der
    # tatsächliche Treiber-Name – so schlägt der Guard auch dann an, wenn
    # der Treiber-Init fehlgeschlagen und auf sim gefallen ist (rpi_switch
    # konfiguriert, aber sim aktiv). Letzteres wäre ein Hardware-Problem,
    # kein Grund den Debug-Endpunkt zu öffnen.
    with state_lock:
        mode = str(getattr(state, "sensor_driver_mode", "sim")).strip().lower()

    if mode != "sim":
        raise HTTPException(
            status_code=404,
            detail=(
                f"POST /sensors/sim/set ist nur im Sim-Modus verfügbar "
                f"(aktueller Modus: '{mode}')."
            ),
        )

    # Driver-Instanz holen und auf SimSensorDriver prüfen.
    # get_sensor_driver() gibt den globalen Singleton zurück – denselben den
    # sensor_engine_loop beim nächsten Poll verwendet.
    driver = get_sensor_driver()
    if not isinstance(driver, SimSensorDriver):
        # Sollte bei mode=="sim" nicht passieren, aber defensive Guard.
        raise HTTPException(
            status_code=500,
            detail="Sensor-Driver ist kein SimSensorDriver trotz sim-Modus.",
        )

    # Zustand setzen
    for zone in req.dry_zones:
        driver.set_zone_dry(zone)
    for zone in req.moist_zones:
        driver.set_zone_moist(zone)

    # Aktuellen Driver-Zustand für den Response lesen
    dry_now   = sorted(driver._dry_zones)
    moist_now = sorted(
        z for z in (req.dry_zones + req.moist_zones)
        if z not in driver._dry_zones
    )

    log_event(
        "sensor_sim_set",
        source="manual",
        dry_zones=req.dry_zones,
        moist_zones=req.moist_zones,
        driver_dry_zones_after=dry_now,
    )

    return {
        "ok":         True,
        "dry_zones":  dry_now,
        "moist_zones": moist_now,
    }
