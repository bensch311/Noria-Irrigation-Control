# app/services/valve_driver.py
"""
Ventil-Treiber: Hardware-Abstraktion für GPIO-gesteuerte Bewässerungsventile.

Dieses Modul stellt eine einheitliche Schnittstelle für Ventiloperationen bereit,
unabhängig davon ob ein echter Raspberry Pi oder eine Simulation verwendet wird.

Treiber-Typen:
  SimValveDriver    – Simulation (kein GPIO, nur Logging). Für Dev/Tests/Windows.
  RpiGpioValveDriver – Echter Raspberry Pi GPIO via RPi.GPIO (BCM-Nummerierung).

Treiber-Auswahl (Reihenfolge, see get_valve_driver()):
  1. ENV-Variable IRRIGATION_VALVE_DRIVER ("sim" | "rpi")
  2. device_config.json → state.valve_driver_mode
  3. Fallback: "sim"

Bei RpiGpioValveDriver:
  - active_low=True: Relais-Board mit Active-Low-Logik (typisch für chinesische
    8-Kanal-Relay-Boards). LOW = Relais anzieht = Ventil öffnet.
  - active_low=False: Standard-Logik. HIGH = Relais anzieht = Ventil öffnet.
  - Pins werden beim Init als Outputs konfiguriert und auf "geschlossen" gesetzt.
  - close_all() ist best-effort: alle Zonen werden versucht, auch bei Teilfehlern.
  - cleanup() gibt GPIO-Ressourcen frei – IMMER nach close_all() aufrufen.

Singleton-Pattern:
  get_valve_driver()   – gibt die globale Instanz zurück (lazy init)
  reset_valve_driver() – setzt die Instanz zurück (für Tests / Reload nach Config-Änderung)
  set_valve_driver()   – setzt eine vordefinierte Instanz (für Tests)

ALLE Hardware-Operationen müssen über den IO-Worker-Thread laufen (services/io_worker.py).
Den Treiber NIEMALS direkt aus dem Main-Thread oder unter state_lock aufrufen.
"""

from __future__ import annotations
from typing import Dict, Any
import os
from dataclasses import dataclass
from core.logging import log_event


class ValveDriverError(RuntimeError):
    """Fehler bei einer Hardware-Operation (open/close/close_all)."""
    pass


def validate_gpio_pins(pins_by_zone: Dict[int, int]) -> Dict[str, Any]:
    """Validate BCM pins for RPi driver.

    Returns a dict with keys:
      - ok: bool
      - invalid_pins: list[{zone,pin,reason}]
      - duplicate_pins: list[{pin,zones}]
    """
    invalid = []
    by_pin: Dict[int, list[int]] = {}
    for z, p in (pins_by_zone or {}).items():
        try:
            zone = int(z)
            pin = int(p)
        except Exception:
            invalid.append({"zone": z, "pin": p, "reason": "not_int"})
            continue

        # BCM GPIO pins usable in practice are typically 2..27 (0/1 are ID / reserved on many boards)
        if pin < 2 or pin > 27:
            invalid.append({"zone": zone, "pin": pin, "reason": "out_of_range_2_27"})
        by_pin.setdefault(pin, []).append(zone)

    dup = [{"pin": pin, "zones": sorted(zs)} for pin, zs in by_pin.items() if len(zs) > 1]
    ok = (len(invalid) == 0) and (len(dup) == 0)
    return {"ok": ok, "invalid_pins": invalid, "duplicate_pins": dup}


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

    def cleanup(self) -> None:
        """Gibt Hardware-Ressourcen frei (z.B. GPIO.cleanup() auf RPi).
        Default ist ein No-Op – Unterklassen überschreiben bei Bedarf.
        Muss NACH close_all() aufgerufen werden.
        """
        pass


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

        # Setup all pins as outputs, initial value = "closed" (sicher/inaktiv).
        # initial= setzt Richtung UND Wert atomar – kein Race zwischen setup()
        # und dem ersten output()-Call. Ohne initial= würde lgpio/RPi.GPIO den
        # Pin kurz auf LOW setzen, was bei Active-Low-Boards alle Relais kurz
        # (oder dauerhaft) aktiviert bevor write_closed() greift.
        initial_closed = GPIO.LOW if self._active_low else GPIO.HIGH
        for zone, pin in sorted(self._pins_by_zone.items()):
            GPIO.setup(int(pin), GPIO.OUT, initial=initial_closed)

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
        # Best-effort: jede Zone wird einzeln versucht, auch wenn vorherige
        # fehlschlagen. Fehler werden geloggt, aber nicht nach außen geworfen –
        # close_all() ist immer ein Sicherheits-Versuch, kein atomarer Op.
        failed_zones: list[dict] = []
        for zone, pin in sorted(self._pins_by_zone.items()):
            try:
                self._write_closed(int(pin))
            except Exception as e:
                failed_zones.append({"zone": zone, "pin": pin, "error": repr(e)})
                log_event(
                    "valve_hw_close_all_zone_error",
                    level="error",
                    source="driver",
                    driver=self.name,
                    zone=zone,
                    pin=pin,
                    error=repr(e),
                )
        log_event(
            "valve_hw_close_all",
            source="driver",
            driver=self.name,
            failed_zones=failed_zones,
            failed_count=len(failed_zones),
        )

    def cleanup(self) -> None:
        """Gibt GPIO-Ressourcen frei und setzt alle Pins auf Input zurück.

        Muss nach close_all() aufgerufen werden – niemals davor, da
        GPIO.cleanup() die Pin-Kontrolle sofort abgibt.
        """
        try:
            self._GPIO.cleanup()
            log_event("valve_driver_gpio_cleanup", source="driver", driver=self.name)
        except Exception as e:
            log_event(
                "valve_driver_gpio_cleanup_error",
                level="error",
                source="driver",
                driver=self.name,
                error=repr(e),
            )


# --- Singleton / Accessor ---
_driver: BaseValveDriver | None = None


def reset_valve_driver() -> None:
    """Setzt den globalen Valve-Driver zurück.

    Wird nach einer Config-Änderung in load_device_config_from_disk() aufgerufen,
    damit get_valve_driver() beim nächsten Aufruf einen neuen Driver initialisiert.
    Auch für Tests nützlich.
    """
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
    """Liest Driver-Settings (mode, active_low, pins) aus dem globalen State.

    Der State wird von load_device_config_from_disk() (persistence.py) befüllt.
    Diese Funktion ist der "Brücke" zwischen Persistence und Driver-Initialisierung.
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
    """Gibt die globale Valve-Driver-Instanz zurück (lazy singleton).

    Reihenfolge (Best Practice):
      1) ENV override (wenn gesetzt)
      2) device_config.json/state
      3) fallback = sim

    Bei Fehlern in der Initialisierung (z.B. RPi.GPIO nicht verfügbar,
    fehlende GPIO-Pins) → sicherer Fallback auf SimValveDriver mit Error-Log.
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

            vres = validate_gpio_pins(pins_by_zone)
            if not vres.get("ok"):
                raise ValveDriverError(f"Ungültige GPIO Pin-Konfiguration: {vres}")

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
