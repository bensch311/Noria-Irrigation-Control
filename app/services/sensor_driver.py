# app/services/sensor_driver.py
"""
Sensor-Treiber: Hardware-Abstraktion für Bewässerungs-Sensoren.

Dieses Modul stellt eine einheitliche Schnittstelle für Sensor-Lesevorgänge
bereit, unabhängig davon ob echter Hardware oder eine Simulation verwendet wird.

Treiber-Typen:
  SimSensorDriver            – Simulation (kein GPIO, nur Logging). Für Dev/Tests/Windows.
  RpiGpioSwitchSensorDriver  – Raspberry Pi GPIO, digitaler Trockenkontakt (z.B. MMM TXS
                               Schalttensiometer) via lgpio / rpi-lgpio.

Elektrisches Prinzip (TXS Schalttensiometer):
  Der TXS liefert einen potentialfreien Trockenkontakt (max. 2A/24V).
  Der Kontakt schließt wenn die Bodensaugspannung den eingestellten
  Schwellwert überschreitet (Boden zu trocken → Bewässerung nötig).

  Empfohlene Beschaltung am Raspberry Pi:

    3.3V ──┬── 10kΩ Pull-up ──┬── GPIO Pin (Input, BCM-Nummerierung)
           │                  │
           │              [TXS Trockenkontakt]
           │                  │
          GND ────────────────┘

  Kontakt OFFEN  (Boden feucht, kein Bedarf) → GPIO HIGH (1) → needs_irrigation=False
  Kontakt GESCHLOSSEN (Boden trocken)        → GPIO LOW  (0) → needs_irrigation=True

  WICHTIG: Externer 10kΩ Pull-up zu 3.3V ist für den Produktionseinsatz
  empfohlen. Der interne Pi-Pull-up (~50kΩ) kann als Fallback genutzt werden
  (sensor_internal_pull_up=True in device_config.json), ist aber für kritische
  Anwendungen zu hochohmig und störanfälliger.

Treiber-Auswahl (Reihenfolge, siehe get_sensor_driver()):
  1. ENV-Variable IRRIGATION_SENSOR_DRIVER ("sim" | "rpi_switch")
  2. device_config.json → state.sensor_driver_mode
  3. Fallback: "sim"

Bei RpiGpioSwitchSensorDriver:
  - Pins werden beim Init via gpio_claim_input() als Inputs konfiguriert.
  - internal_pull_up=True: Aktiviert internen Pi-Pull-Up (~50kΩ).
  - internal_pull_up=False: Kein interner Pull-Up – externer Pull-Up erwartet.
  - Sensor-Pins DÜRFEN NICHT mit Ventil-Pins überlappen (manuelle Prüfung
    bei der Konfiguration in device_config.json erforderlich).
  - cleanup() gibt den GPIO-Chip-Handle frei – IMMER nach dem letzten read() aufrufen.

Warum lgpio statt RPi.GPIO:
  Identische Begründung wie in valve_driver.py: Pi 5 / RP1-Chip-Kompatibilität.

Singleton-Pattern:
  get_sensor_driver()   – gibt die globale Instanz zurück (lazy init)
  reset_sensor_driver() – setzt die Instanz zurück (für Tests / Config-Reload)
  set_sensor_driver()   – setzt eine vordefinierte Instanz (für Tests)

Thread-Safety:
  Sensor-Lesevorgänge dürfen NICHT unter state_lock aufgerufen werden.
  In der Sensor-Engine müssen sie über den IO-Worker-Thread serialisiert werden,
  analog zu den Ventil-Operationen in services/engine.py.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Set

from core.logging import log_event


# ─────────────────────────────────────────────────────────────────────────────
# Fehlerklasse
# ─────────────────────────────────────────────────────────────────────────────

class SensorDriverError(RuntimeError):
    """Fehler bei einer Sensor-Hardware-Operation (read/cleanup)."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# SensorReading – Ergebnis eines Lesevorgangs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SensorReading:
    """
    Ergebnis eines einzelnen Sensor-Lesevorgangs.

    Felder:
      zone             – Zonen-Nummer (1-basiert, entspricht Ventil-Zone)
      needs_irrigation – True: Sensor signalisiert Bewässerungsbedarf
      raw_gpio_value   – Roher GPIO-Wert (0=LOW, 1=HIGH) für Diagnose/Logging.
                         0 entspricht geschlossenem Kontakt (trocken),
                         1 entspricht offenem Kontakt (feucht).
      timestamp        – monotonic timestamp des Lesevorgangs (time.monotonic())
      driver_name      – Name des Treibers der diese Lesung erzeugt hat
    """
    zone: int
    needs_irrigation: bool
    raw_gpio_value: int      # 0 oder 1
    timestamp: float         # time.monotonic()
    driver_name: str


# ─────────────────────────────────────────────────────────────────────────────
# Validierung
# ─────────────────────────────────────────────────────────────────────────────

def validate_sensor_pins(pins_by_zone: Dict[int, int]) -> Dict[str, Any]:
    """Validiert BCM-Sensor-Pins analog zu validate_gpio_pins() in valve_driver.py.

    Prüft:
      - Pin-Werte im gültigen BCM-Bereich (2..27)
      - Keine Duplikate (eine Zone ↔ ein Pin, ein Pin ↔ eine Zone)

    Returns a dict with keys:
      - ok: bool
      - invalid_pins: list[{zone, pin, reason}]
      - duplicate_pins: list[{pin, zones}]
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

        # BCM GPIO pins usable in practice are typically 2..27
        if pin < 2 or pin > 27:
            invalid.append({"zone": zone, "pin": pin, "reason": "out_of_range_2_27"})
        by_pin.setdefault(pin, []).append(zone)

    dup = [{"pin": pin, "zones": sorted(zs)} for pin, zs in by_pin.items() if len(zs) > 1]
    ok = (len(invalid) == 0) and (len(dup) == 0)
    return {"ok": ok, "invalid_pins": invalid, "duplicate_pins": dup}


# ─────────────────────────────────────────────────────────────────────────────
# BaseSensorDriver
# ─────────────────────────────────────────────────────────────────────────────

class BaseSensorDriver:
    """
    Hardware-Abstraktion für Sensoren.

    Unterklassen implementieren read() für die jeweilige Hardware.
    cleanup() ist ein No-Op sofern keine Hardware-Ressourcen zu freigeben sind.
    """

    name: str = "base"

    def read(self, zone: int) -> SensorReading:
        """Liest den aktuellen Sensor-Zustand für die angegebene Zone.

        Raises:
            SensorDriverError: Bei Hardware-Fehler oder nicht konfigurierter Zone.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """Gibt Hardware-Ressourcen frei (z.B. gpiochip_close() auf dem RPi).
        Default ist ein No-Op – Unterklassen überschreiben bei Bedarf.
        Muss nach dem letzten read() aufgerufen werden.
        """
        pass


# ─────────────────────────────────────────────────────────────────────────────
# SimSensorDriver
# ─────────────────────────────────────────────────────────────────────────────

class SimSensorDriver(BaseSensorDriver):
    """
    Simulation: kein GPIO-Zugriff, nur Logging.
    Ideal für Windows/Dev/Tests.

    Alle Zonen starten als "feucht" (needs_irrigation=False).
    Für Tests können Zonen gezielt als "trocken" markiert werden über
    set_zone_dry() / set_zone_moist() / set_all_dry().
    """

    name: str = "sim"

    def __init__(self) -> None:
        # Zonen die als "trocken" simuliert werden sollen (needs_irrigation=True)
        self._dry_zones: Set[int] = set()

    def set_zone_dry(self, zone: int) -> None:
        """Markiert eine Zone als 'trocken' → needs_irrigation=True."""
        self._dry_zones.add(int(zone))

    def set_zone_moist(self, zone: int) -> None:
        """Markiert eine Zone als 'feucht' → needs_irrigation=False."""
        self._dry_zones.discard(int(zone))

    def set_all_dry(self, zones: list[int]) -> None:
        """Markiert mehrere Zonen gleichzeitig als 'trocken'."""
        for z in zones:
            self._dry_zones.add(int(z))

    def read(self, zone: int) -> SensorReading:
        zone = int(zone)
        needs = zone in self._dry_zones
        # Kontakt geschlossen (raw=0) = trocken; Kontakt offen (raw=1) = feucht
        raw = 0 if needs else 1
        log_event(
            "sensor_hw_read",
            source="driver",
            driver=self.name,
            zone=zone,
            needs_irrigation=needs,
        )
        return SensorReading(
            zone=zone,
            needs_irrigation=needs,
            raw_gpio_value=raw,
            timestamp=time.monotonic(),
            driver_name=self.name,
        )


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioSwitchSensorDriver
# ─────────────────────────────────────────────────────────────────────────────

class RpiGpioSwitchSensorDriver(BaseSensorDriver):
    """
    Raspberry Pi GPIO Driver für digitale Trockenkontakt-Sensoren (z.B. MMM TXS).

    Kompatibel mit Raspberry Pi 5 (RP1-Chip). Verwendet lgpio statt RPi.GPIO,
    da RPi.GPIO 0.7.x den RP1-I/O-Controller nicht unterstützt.

    Elektrisches Prinzip:
      Externer 10kΩ Pull-up zu 3.3V empfohlen. Kontakt OFFEN → HIGH (kein Bedarf),
      Kontakt GESCHLOSSEN → LOW (Bewässerung nötig).

    Thread-Safety:
      read() ist NICHT thread-safe bezüglich des lgpio-Handles. Alle Aufrufe
      müssen über den IO-Worker-Thread serialisiert werden.
    """

    name: str = "rpi_switch"

    # lgpio lFlags-Konstante für internen Pull-Up (aus lgpio-Source: SET_PULL_UP = 0x20)
    _LGPIO_SET_PULL_UP: int = 0x20

    def __init__(self, pins_by_zone: Dict[int, int], internal_pull_up: bool = False):
        """
        Args:
            pins_by_zone:     Mapping Zone → BCM-Pin (z.B. {1: 17, 2: 18}).
            internal_pull_up: True  = interner Pi-Pull-Up (~50kΩ) verwenden.
                              False = externer Pull-Up erwartet (Produktion empfohlen).
        """
        self._pins_by_zone = dict(pins_by_zone)
        self._internal_pull_up = bool(internal_pull_up)

        try:
            import lgpio  # type: ignore
        except Exception as e:
            raise SensorDriverError(f"lgpio nicht verfügbar: {e}")

        self._lgpio = lgpio

        # gpiochip 0 ist auf allen Pi-Modellen der primäre GPIO-Chip.
        try:
            self._handle = lgpio.gpiochip_open(0)
        except Exception as e:
            raise SensorDriverError(f"GPIO-Chip konnte nicht geöffnet werden: {e}")

        lflags = self._LGPIO_SET_PULL_UP if self._internal_pull_up else 0

        for zone, pin in sorted(self._pins_by_zone.items()):
            try:
                lgpio.gpio_claim_input(self._handle, int(pin), lflags)
            except Exception as e:
                # Chip-Handle freigeben bevor wir die Exception weiterwerfen,
                # damit kein Ressourcen-Leak entsteht.
                try:
                    lgpio.gpiochip_close(self._handle)
                except Exception:
                    pass
                raise SensorDriverError(
                    f"Sensor-Pin BCM {pin} (Zone {zone}) konnte nicht konfiguriert werden: {e}"
                )

        log_event(
            "sensor_driver_gpio_setup",
            source="driver",
            driver=self.name,
            internal_pull_up=self._internal_pull_up,
            zones=sorted(list(self._pins_by_zone.keys())),
        )

    def read(self, zone: int) -> SensorReading:
        """Liest den Kontaktzustand für die angegebene Zone.

        Returns:
            SensorReading mit needs_irrigation=True wenn GPIO LOW (Kontakt geschlossen).

        Raises:
            SensorDriverError: Wenn Zone nicht konfiguriert oder GPIO-Lesefehler.
        """
        zone = int(zone)
        if zone not in self._pins_by_zone:
            raise SensorDriverError(f"Kein GPIO Pin für Sensor zone={zone} konfiguriert")

        pin = int(self._pins_by_zone[zone])
        try:
            raw = self._lgpio.gpio_read(self._handle, pin)
        except Exception as e:
            raise SensorDriverError(
                f"GPIO-Lesefehler Pin BCM {pin} (Zone {zone}): {e}"
            )

        # LOW (0) = Kontakt geschlossen = Boden trocken = Bewässerung nötig
        needs_irrigation = (raw == 0)

        log_event(
            "sensor_hw_read",
            source="driver",
            driver=self.name,
            zone=zone,
            pin=pin,
            raw_gpio_value=int(raw),
            needs_irrigation=needs_irrigation,
        )
        return SensorReading(
            zone=zone,
            needs_irrigation=needs_irrigation,
            raw_gpio_value=int(raw),
            timestamp=time.monotonic(),
            driver_name=self.name,
        )

    def cleanup(self) -> None:
        """Gibt den GPIO-Chip-Handle frei.

        Muss nach dem letzten read() aufgerufen werden – niemals davor, da
        gpiochip_close() die Pin-Kontrolle sofort abgibt.
        """
        try:
            self._lgpio.gpiochip_close(self._handle)
            log_event("sensor_driver_gpio_cleanup", source="driver", driver=self.name)
        except Exception as e:
            log_event(
                "sensor_driver_gpio_cleanup_error",
                level="error",
                source="driver",
                driver=self.name,
                error=repr(e),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Singleton / Accessor
# ─────────────────────────────────────────────────────────────────────────────

_sensor_driver: BaseSensorDriver | None = None


def reset_sensor_driver() -> None:
    """Setzt den globalen Sensor-Driver zurück.

    Wird nach einer Config-Änderung in load_device_config_from_disk() aufgerufen,
    damit get_sensor_driver() beim nächsten Aufruf einen neuen Driver initialisiert.
    Auch für Tests nützlich.
    """
    global _sensor_driver
    _sensor_driver = None
    log_event("sensor_driver_reset", source="driver")


def set_sensor_driver(driver: BaseSensorDriver) -> None:
    """Für Tests/Dev: erlaubt gezieltes Setzen eines Drivers."""
    global _sensor_driver
    _sensor_driver = driver
    log_event(
        "sensor_driver_set",
        source="driver",
        driver=getattr(driver, "name", "unknown"),
    )


def _read_sensor_settings_from_state() -> dict[str, Any]:
    """Liest Sensor-Driver-Settings aus dem globalen State.

    Der State wird von load_device_config_from_disk() (persistence.py) befüllt.
    Diese Funktion ist die "Brücke" zwischen Persistence und Driver-Initialisierung.
    """
    try:
        from core.state import state, state_lock
        with state_lock:
            return {
                "mode": getattr(state, "sensor_driver_mode", None),
                "pins": getattr(state, "sensor_gpio_pins_by_zone", None),
                "internal_pull_up": getattr(state, "sensor_internal_pull_up", False),
            }
    except Exception:
        return {"mode": None, "pins": None, "internal_pull_up": False}


def get_sensor_driver() -> BaseSensorDriver:
    """Gibt die globale Sensor-Driver-Instanz zurück (lazy singleton).

    Reihenfolge (Best Practice):
      1) ENV override IRRIGATION_SENSOR_DRIVER (wenn gesetzt)
      2) device_config.json / state
      3) fallback = sim

    Bei Fehlern in der Initialisierung (z.B. lgpio nicht verfügbar,
    fehlende GPIO-Pins) → sicherer Fallback auf SimSensorDriver mit Error-Log.
    """
    global _sensor_driver
    if _sensor_driver is not None:
        return _sensor_driver

    env_mode = (os.getenv("IRRIGATION_SENSOR_DRIVER") or "").strip().lower() or None

    st = _read_sensor_settings_from_state()
    mode = (env_mode or (st.get("mode") or "sim")).strip().lower()
    internal_pull_up = bool(st.get("internal_pull_up"))

    pins_raw = st.get("pins")
    pins: Dict[int, int] = {}
    if isinstance(pins_raw, dict):
        for k, v in pins_raw.items():
            try:
                pins[int(k)] = int(v)
            except Exception:
                continue

    try:
        if mode == "sim":
            _sensor_driver = SimSensorDriver()
            log_event(
                "sensor_driver_init",
                source="driver",
                driver=_sensor_driver.name,
                mode=mode,
                env_override=bool(env_mode),
            )
            return _sensor_driver

        if mode == "rpi_switch":
            if not pins:
                raise SensorDriverError(
                    "IRRIGATION_SENSOR_PINS ist leer/fehlt in device_config.json"
                )

            vres = validate_sensor_pins(pins)
            if not vres.get("ok"):
                raise SensorDriverError(
                    f"Ungültige Sensor-Pin-Konfiguration: {vres}"
                )

            _sensor_driver = RpiGpioSwitchSensorDriver(
                pins_by_zone=pins,
                internal_pull_up=internal_pull_up,
            )
            log_event(
                "sensor_driver_init",
                source="driver",
                driver=_sensor_driver.name,
                mode=mode,
                env_override=bool(env_mode),
            )
            return _sensor_driver

        # Unbekannter Modus → sicherer Fallback auf sim
        _sensor_driver = SimSensorDriver()
        log_event(
            "sensor_driver_init_fallback",
            level="warning",
            source="driver",
            requested_mode=mode,
            driver=_sensor_driver.name,
            message="Unbekannter IRRIGATION_SENSOR_DRIVER; fallback auf sim.",
        )
        return _sensor_driver

    except Exception as e:
        # Jeder Init-Fehler → sicherer Fallback auf sim
        _sensor_driver = SimSensorDriver()
        log_event(
            "sensor_driver_init_failed_fallback",
            level="error",
            source="driver",
            requested_mode=mode,
            driver=_sensor_driver.name,
            error=repr(e),
        )
        return _sensor_driver
