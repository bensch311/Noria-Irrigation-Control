# app/api/routes_sensors.py
"""
Sensor-Endpunkte.

GET  /sensors/readings      – Aktueller Feuchtezustand pro Sensor
GET  /sensors/config        – Sensor-Hardware-Konfiguration
GET  /sensors/assignments   – Aktuelle Sensor-Zonen-Zuordnung
POST /sensors/assignments   – Sensor-Zonen-Zuordnung setzen (persistiert)
POST /sensors/sim/set       – Sim-only: Sensoren manuell auf trocken/feucht setzen

Schlüsselkonzept:
  sensor_gpio_pins         – {sensor_id: BCM-Pin} aus device_config.json (Hardware-Admin)
  sensor_zone_assignments  – {sensor_id: [zone, ...]} aus sensor_assignments.json (Betrieb, UI-editierbar)
"""

from fastapi import APIRouter, Depends, HTTPException, Request
import time

from core.state import state, state_lock
from core.security import require_api_key
from core.logging import log_event
from core.limiter import limiter, MUTATION_LIMIT
from services.sensor_driver import get_sensor_driver, validate_sensor_pins, SimSensorDriver
from models.requests import SimSensorSetRequest, SensorAssignmentRequest

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/sensors/readings")
def get_sensor_readings():
    """Aktueller Feuchtezustand aller konfigurierten Sensoren.

    Response-Felder:
      sensor_driver      – Aktiver Treiber-Name ("sim" | "rpi_switch")
      sensors_configured – Sortierte Liste konfigurierter Sensor-IDs
      readings           – {sensor_id: needs_irrigation} – True = trocken
      cooldown_s         – Konfigurierter Cooldown in Sekunden
      last_triggered     – {sensor_id: elapsed_s} – Sekunden seit letztem Trigger
      polling_interval_s – Konfiguriertes Polling-Intervall
    """
    # Driver-Name vor Lock (get_sensor_driver() kann intern state_lock anfordern)
    driver_name = get_sensor_driver().name

    now_m = time.monotonic()

    with state_lock:
        pins               = dict(state.sensor_gpio_pins or {})
        readings_raw       = dict(state.sensor_readings or {})
        last_triggered_raw = dict(state.sensor_last_triggered or {})
        cooldown_s         = int(getattr(state, "sensor_cooldown_s", 600))
        polling_interval_s = int(getattr(state, "sensor_polling_interval_s", 30))

    sensors_configured = sorted(int(s) for s in pins.keys())

    readings: dict[str, bool] = {
        str(s): bool(v) for s, v in readings_raw.items()
    }

    last_triggered: dict[str, float] = {}
    for sid in sensors_configured:
        ts = last_triggered_raw.get(sid)
        if ts is not None:
            last_triggered[str(sid)] = round(now_m - ts, 1)

    return {
        "sensor_driver":       driver_name,
        "sensors_configured":  sensors_configured,
        "readings":            readings,
        "cooldown_s":          cooldown_s,
        "last_triggered":      last_triggered,
        "polling_interval_s":  polling_interval_s,
    }


@router.get("/sensors/config")
def get_sensor_config():
    """Sensor-Hardware-Konfiguration (aus device_config.json).

    Response-Felder:
      sensor_driver          – Aktiver Treiber-Name
      configured_driver_mode – Konfigurierter Modus
      sensor_internal_pull_up – True = interner Pi-Pull-Up aktiv
      sensors_configured     – Sortierte Liste konfigurierter Sensor-IDs
      polling_interval_s     – Polling-Intervall in Sekunden
      cooldown_s             – Sensor-Cooldown in Sekunden
      default_duration_s     – Standard-Bewässerungsdauer in Sekunden
      gpio_config_valid      – False wenn ungültige/doppelte Pins
      invalid_pins           – Liste ungültiger Pins (nur bei rpi_switch)
      duplicate_pins         – Liste doppelter Pins (nur bei rpi_switch)
    """
    driver_name = get_sensor_driver().name

    with state_lock:
        mode               = str(getattr(state, "sensor_driver_mode", "sim")).strip().lower()
        pins               = dict(state.sensor_gpio_pins or {})
        internal_pull_up   = bool(getattr(state, "sensor_internal_pull_up", False))
        polling_interval_s = int(getattr(state, "sensor_polling_interval_s", 30))
        cooldown_s         = int(getattr(state, "sensor_cooldown_s", 600))
        default_duration_s = int(getattr(state, "sensor_default_duration_s", 300))

    sensors_configured = sorted(int(s) for s in pins.keys())

    if mode == "rpi_switch":
        pins_int: dict[int, int] = {}
        for k, v in pins.items():
            try:
                pins_int[int(k)] = int(v)
            except Exception:
                pass
        gpio_validation  = validate_sensor_pins(pins_int)
        gpio_config_valid = bool(gpio_validation.get("ok"))
    else:
        gpio_validation   = {"ok": True, "invalid_pins": [], "duplicate_pins": []}
        gpio_config_valid = True

    return {
        "sensor_driver":           driver_name,
        "configured_driver_mode":  mode,
        "sensor_internal_pull_up": internal_pull_up,
        "sensors_configured":      sensors_configured,
        "polling_interval_s":      polling_interval_s,
        "cooldown_s":              cooldown_s,
        "default_duration_s":      default_duration_s,
        "gpio_config_valid":       gpio_config_valid,
        "invalid_pins":            gpio_validation.get("invalid_pins", []),
        "duplicate_pins":          gpio_validation.get("duplicate_pins", []),
    }


@router.get("/sensors/assignments")
def get_sensor_assignments():
    """Aktuelle Sensor-Zonen-Zuordnung.

    Response-Felder:
      assignments – {sensor_id: [zone, ...]}
    """
    with state_lock:
        raw = dict(state.sensor_zone_assignments or {})

    return {
        "assignments": {str(sid): zones for sid, zones in raw.items()},
    }


@router.post("/sensors/assignments")
@limiter.limit(MUTATION_LIMIT)
def set_sensor_assignments(request: Request, req: SensorAssignmentRequest):
    """Setzt Sensor-Zonen-Zuordnung und persistiert sie sofort.

    PUT-Semantik: die gesamte bisherige Zuordnung wird ersetzt.
    Übergebene assignments müssen nur die sensor_ids enthalten die
    konfiguriert sind (aus device_config.json) – andere werden akzeptiert
    aber ignoriert wenn kein GPIO-Pin konfiguriert ist.

    Persistierung: sofort via save_sensor_assignments_to_disk(),
    nicht über den 2s-dirty-Flag-Mechanismus (zu wichtig um zu warten).
    """
    # Validierung: Zonen dürfen max_valves nicht überschreiten
    with state_lock:
        max_valves = int(getattr(state, "max_valves", 6))

    normalized: dict[int, list[int]] = {}
    for sid_str, zones in req.assignments.items():
        sid = int(sid_str)
        valid_zones = [int(z) for z in zones if 1 <= int(z) <= max_valves]
        normalized[sid] = valid_zones

    with state_lock:
        state.sensor_zone_assignments = normalized
        state.sensor_assignments_dirty = True

    # Sofort persistieren
    from services.persistence import save_sensor_assignments_to_disk
    save_sensor_assignments_to_disk()

    log_event(
        "sensor_assignments_updated",
        source="manual",
        sensor_count=len(normalized),
        assignments={str(k): v for k, v in normalized.items()},
    )

    return {
        "ok":          True,
        "assignments": {str(sid): zones for sid, zones in normalized.items()},
    }


@router.post("/sensors/sim/set")
@limiter.limit(MUTATION_LIMIT)
def sim_set_sensor_state(request: Request, req: SimSensorSetRequest):
    """Setzt Sensor-Zustände im Sim-Modus manuell.

    NUR im Sim-Modus verfügbar (sensor_driver_mode == "sim") → 404 sonst.

    Request-Felder:
      dry_sensors   – Sensor-IDs auf "trocken" setzen (needs_irrigation=True)
      moist_sensors – Sensor-IDs auf "feucht" setzen  (needs_irrigation=False)
    """
    with state_lock:
        mode = str(getattr(state, "sensor_driver_mode", "sim")).strip().lower()

    if mode != "sim":
        raise HTTPException(
            status_code=404,
            detail=f"POST /sensors/sim/set ist nur im Sim-Modus verfügbar (aktuell: '{mode}').",
        )

    driver = get_sensor_driver()
    if not isinstance(driver, SimSensorDriver):
        raise HTTPException(
            status_code=500,
            detail="Sensor-Driver ist kein SimSensorDriver trotz sim-Modus.",
        )

    for sid in req.dry_sensors:
        driver.set_zone_dry(sid)
    for sid in req.moist_sensors:
        driver.set_zone_moist(sid)

    dry_now = sorted(driver._dry_zones)

    log_event(
        "sensor_sim_set",
        source="manual",
        dry_sensors=req.dry_sensors,
        moist_sensors=req.moist_sensors,
        driver_dry_sensors_after=dry_now,
    )

    return {
        "ok":          True,
        "dry_sensors": dry_now,
    }
