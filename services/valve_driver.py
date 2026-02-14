# app/services/valve_driver.py
from __future__ import annotations

import os
from dataclasses import dataclass

from core.logging import log_event


class ValveDriverError(RuntimeError):
    pass


class BaseValveDriver:
    """
    Hardware-Abstraktion für Ventile.

    WICHTIG: In diesem Patch wird der Driver noch NICHT von der Engine genutzt.
    Das kommt im nächsten Patch, damit wir sauber in kleinen Schritten bleiben.
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


# --- Singleton / Accessor ---
_driver: BaseValveDriver | None = None


def set_valve_driver(driver: BaseValveDriver) -> None:
    """
    Für Tests/Dev: erlaubt gezieltes Setzen eines Drivers.
    """
    global _driver
    _driver = driver
    log_event("valve_driver_set", source="driver", driver=getattr(driver, "name", "unknown"))


def get_valve_driver() -> BaseValveDriver:
    """
    Default-Auswahl per ENV:
      IRRIGATION_VALVE_DRIVER=sim   (default)
      IRRIGATION_VALVE_DRIVER=rpi   (kommt später)
    """
    global _driver
    if _driver is not None:
        return _driver

    mode = (os.getenv("IRRIGATION_VALVE_DRIVER") or "sim").strip().lower()

    if mode == "sim":
        _driver = SimValveDriver()
        log_event("valve_driver_init", source="driver", driver=_driver.name, mode=mode)
        return _driver

    # Platzhalter für später (Raspberry Pi GPIO)
    # Wir fallen sicher auf sim zurück, statt hart zu crashen.
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
