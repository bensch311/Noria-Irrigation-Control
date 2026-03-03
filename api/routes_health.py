from fastapi import APIRouter
from datetime import datetime

from core.state import state, state_lock
from core.config import TZ

router = APIRouter()

@router.get("/health")
def health():
    from services.valve_driver import get_valve_driver, validate_gpio_pins

    # driver_name VOR dem Lock berechnen – get_valve_driver() kann intern
    # state_lock anfordern (_read_driver_settings_from_state), was innerhalb
    # eines nicht-re-entranten Locks zum Deadlock führen würde.
    driver_name = get_valve_driver().name

    with state_lock:
        running = sorted(list((state.active_runs or {}).keys()))
        qlen = len(state.queue or [])

        # Config snapshot for diagnostics
        max_valves = int(getattr(state, "max_valves", 1))
        mode = str(getattr(state, "valve_driver_mode", "sim")).strip().lower()
        active_low = bool(getattr(state, "relay_active_low", True))
        pins_by_zone = dict(getattr(state, "gpio_pins_by_zone", {}) or {})

        parallel_enabled = bool(getattr(state, "parallel_enabled", False))
        max_concurrent_valves = int(getattr(state, "max_concurrent_valves", 1))

        # Hardware-Fault-Status: bestimmt den ok-Wert
        hw_faulted = bool(getattr(state, "hw_faulted", False))
        hw_fault_reason = str(getattr(state, "hw_fault_reason", ""))
        hw_fault_zone = getattr(state, "hw_fault_zone", None)
        hw_fault_since = str(getattr(state, "hw_fault_since", ""))

    configured_zones = sorted([int(z) for z in pins_by_zone.keys() if int(z) >= 1])
    required_zones = list(range(1, max_valves + 1))
    missing_zones = [z for z in required_zones if z not in set(configured_zones)]

    gpio_validation = validate_gpio_pins(pins_by_zone) if mode == "rpi" else {"ok": True, "invalid_pins": [], "duplicate_pins": []}
    gpio_config_valid = True
    if mode == "rpi":
        gpio_config_valid = (len(missing_zones) == 0) and bool(gpio_validation.get("ok"))

    return {
        # ok=False signalisiert Monitoring-Systemen einen nicht-quittiertem HW-Fault.
        # HTTP 200 bleibt immer erhalten – der ok-Wert im Body ist das
        # eigentliche Gesundheitssignal (kompatibel mit allen gängigen Health-Checks).
        "ok": not hw_faulted,
        "service": "irrigation",
        "version": 1,
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "running_zones": running,
        "queue_length": qlen,
        "parallel_enabled": parallel_enabled,
        "max_concurrent_valves": max_concurrent_valves,
        "hw_faulted": hw_faulted,
        "hw_fault_reason": hw_fault_reason,
        "hw_fault_zone": hw_fault_zone,
        "hw_fault_since": hw_fault_since,
        "valves": {
            "valve_driver": driver_name,
            "configured_driver_mode": mode,
            "relay_active_low": active_low,
            "max_valves": max_valves,
            "configured_zones": configured_zones,
            "missing_zones": missing_zones,
            "gpio_config_valid": gpio_config_valid,
            "invalid_pins": gpio_validation.get("invalid_pins", []),
            "duplicate_pins": gpio_validation.get("duplicate_pins", []),
        },
    }

