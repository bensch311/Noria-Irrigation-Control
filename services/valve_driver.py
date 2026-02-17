# app/services/valve_driver.py
from __future__ import annotations
from typing import Dict, Any
import os
from dataclasses import dataclass
from core.logging import log_event


class ValveDriverError(RuntimeError):
    pass


class BaseValveDriver:
    """
    Hardware-Abstraktion für Ventile.
    """

    name: str = "base"

    def open(self, zone: int) -> None:
        raise NotImplementedError

    def close(self, zone: int) -> None:
        raise NotImplementedError

    def close_all(self) -> None:
        raise NotImplementedError


@dataclass
class SimValveDriver(BaseValveDriver):
    """
    Simulation: macht nichts an GPIO, loggt aber die Calls.
    Ideal für Windows/Dev/Tests.
    """
    name: str = "sim"

    def open(self, zone: int) -> None:
        log_event("valve_hw_open", source="driver", driver=self.name, zone=int(zone))

    def close(self, zone: int) -> None:
        log_event("valve_hw_close", source="driver", driver=self.name, zone=int(zone))

    def close_all(self) -> None:
        log_event("valve_hw_close_all", source="driver", driver=self.name)


class RpiGpioValveDriver(BaseValveDriver):
    """
    Raspberry Pi GPIO Driver via RPi.GPIO (BCM numbering).
    Relay boards are often active-low.
    """
    name: str = "rpi"

    def __init__(self, pins_by_zone: Dict[int, int], active_low: bool):
        self._pins_by_zone = dict(pins_by_zone)
        self._active_low = bool(active_low)

        try:
            import RPi.GPIO as GPIO  # type: ignore
        except Exception as e:
            raise ValveDriverError(f"RPi.GPIO nicht verfügbar: {e}")

        self._GPIO = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        # Setup all pins as outputs, default to "closed"
        for zone, pin in sorted(self._pins_by_zone.items()):
            GPIO.setup(int(pin), GPIO.OUT)
            self._write_closed(int(pin))

        log_event(
            "valve_driver_gpio_setup",
            source="driver",
            driver=self.name,
            active_low=self._active_low,
            zones=sorted(list(self._pins_by_zone.keys())),
        )

    def _write_open(self, pin: int) -> None:
        # open = relay energized
        val = 0 if self._active_low else 1
        self._GPIO.output(pin, val)

    def _write_closed(self, pin: int) -> None:
        # closed = relay de-energized
        val = 1 if self._active_low else 0
        self._GPIO.output(pin, val)

    def open(self, zone: int) -> None:
        if zone not in self._pins_by_zone:
            raise ValveDriverError(f"Kein GPIO Pin für zone={zone} konfiguriert")
        pin = int(self._pins_by_zone[zone])
        self._write_open(pin)
        log_event("valve_hw_open", source="driver", driver=self.name, zone=int(zone), pin=pin)

    def close(self, zone: int) -> None:
        if zone not in self._pins_by_zone:
            raise ValveDriverError(f"Kein GPIO Pin für zone={zone} konfiguriert")
        pin = int(self._pins_by_zone[zone])
        self._write_closed(pin)
        log_event("valve_hw_close", source="driver", driver=self.name, zone=int(zone), pin=pin)

    def close_all(self) -> None:
        for zone, pin in sorted(self._pins_by_zone.items()):
            try:
                self._write_closed(int(pin))
            except Exception:
                # best effort
                pass
        log_event("valve_hw_close_all", source="driver", driver=self.name)


# --- Singleton / Accessor ---
_driver: BaseValveDriver | None = None

def reset_valve_driver() -> None:
    global _driver
    _driver = None
    log_event("valve_driver_reset", source="driver")


def set_valve_driver(driver: BaseValveDriver) -> None:
    """
    Für Tests/Dev: erlaubt gezieltes Setzen eines Drivers.
    """
    global _driver
    _driver = driver
    log_event("valve_driver_set", source="driver", driver=getattr(driver, "name", "unknown"))


def _read_driver_settings_from_state() -> dict[str, Any]:
    """
    Liest Driver-Settings aus state, falls vorhanden.
    Wird von load_device_config_from_disk() gesetzt.
    """
    try:
        from core.state import state, state_lock
        with state_lock:
            return {
                "mode": getattr(state, "valve_driver_mode", None),
                "active_low": getattr(state, "relay_active_low", None),
                "pins": getattr(state, "gpio_pins_by_zone", None),
            }
    except Exception:
        return {"mode": None, "active_low": None, "pins": None}


def get_valve_driver() -> BaseValveDriver:
    """
    Reihenfolge (Best Practice):
      1) ENV override (wenn gesetzt)
      2) device_config.json/state
      3) fallback = sim
    """
    global _driver
    if _driver is not None:
        return _driver

    env_mode = (os.getenv("IRRIGATION_VALVE_DRIVER") or "").strip().lower() or None

    st = _read_driver_settings_from_state()
    mode = (env_mode or (st.get("mode") or "sim")).strip().lower()

    # active_low: ENV override optional (wenn gesetzt), sonst settings, sonst True (typisch)
    env_active_low = os.getenv("IRRIGATION_RELAY_ACTIVE_LOW")
    if env_active_low is not None and env_active_low.strip() != "":
        active_low = env_active_low.strip().lower() in ("1", "true", "yes", "on")
    else:
        active_low = bool(st.get("active_low")) if st.get("active_low") is not None else True

    pins = st.get("pins") if isinstance(st.get("pins"), dict) else {}

    try:
        if mode == "sim":
            _driver = SimValveDriver()
            log_event("valve_driver_init", source="driver", driver=_driver.name, mode=mode, env_override=bool(env_mode))
            return _driver

        if mode == "rpi":
            if not pins:
                raise ValveDriverError("IRRIGATION_GPIO_PINS ist leer/fehlt in settings.json")

            pins_by_zone: Dict[int, int] = {}
            for k, v in pins.items():
                z = int(k)
                p = int(v)
                pins_by_zone[z] = p

            # NEW: require full coverage 1..max_valves if present in state
            try:
                from core.state import state, state_lock
                with state_lock:
                    max_valves = int(getattr(state, "max_valves", 1))
            except Exception:
                max_valves = 1

            missing = [z for z in range(1, max_valves + 1) if z not in pins_by_zone]
            if missing:
                raise ValveDriverError(f"GPIO Pins fehlen für Zonen: {missing}")

            _driver = RpiGpioValveDriver(pins_by_zone=pins_by_zone, active_low=active_low)
            log_event("valve_driver_init", source="driver", driver=_driver.name, mode=mode, env_override=bool(env_mode))
            return _driver

        # unknown mode => safe fallback
        _driver = SimValveDriver()
        log_event(
            "valve_driver_init_fallback",
            level="warning",
            source="driver",
            requested_mode=mode,
            driver=_driver.name,
            message="Unbekannter IRRIGATION_VALVE_DRIVER; fallback auf sim.",
        )
        return _driver

    except Exception as e:
        # any init error => safe fallback to sim
        _driver = SimValveDriver()
        log_event(
            "valve_driver_init_failed_fallback",
            level="error",
            source="driver",
            requested_mode=mode,
            driver=_driver.name,
            error=repr(e),
        )
        return _driver
