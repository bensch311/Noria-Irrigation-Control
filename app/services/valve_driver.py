# app/services/valve_driver.py
"""
Ventil-Treiber: Hardware-Abstraktion für Bewässerungsventile.

Dieses Modul stellt eine einheitliche Schnittstelle für Ventiloperationen bereit,
unabhängig davon ob ein echter Raspberry Pi oder eine Simulation verwendet wird.

Treiber-Typen:
  SimValveDriver      – Simulation (kein GPIO/I2C, nur Logging). Für Dev/Tests/Windows.
  RpiGpioValveDriver  – Raspberry Pi GPIO via lgpio/rpi-lgpio (BCM-Nummerierung).
  I2cRelayValveDriver – Sequent Microsystems Relay HAT via I2C/SMBus.

Treiber-Auswahl (Reihenfolge, see get_valve_driver()):
  1. ENV-Variable IRRIGATION_VALVE_DRIVER ("sim" | "rpi" | "i2c")
  2. device_config.json → state.valve_driver_mode
  3. Fallback: "sim"

Bei RpiGpioValveDriver:
  - active_low=True: Active-Low-Logik (LOW = Relais zieht an = Ventil öffnet).
  - Pins werden beim Init via gpio_claim_output() atomar auf "geschlossen" gesetzt.
  - close_all() ist best-effort: alle Zonen werden versucht, auch bei Teilfehlern.
  - cleanup() gibt den GPIO-Chip-Handle frei – IMMER nach close_all() aufrufen.

Bei I2cRelayValveDriver (Sequent Microsystems 8-Relay / 16-Relay HAT):
  - hat_type="8relay":  PCA9554-kompatibler 8-Bit I/O-Expander.
                        Primäradresse 0x38–0x3F, Alternativadresse 0x20–0x27.
  - hat_type="16relay": PCA9555-kompatibler 16-Bit I/O-Expander.
                        Primäradresse 0x20–0x27.
  - Register-Quellen: relay.h (16relind) und relay8.h (8relind), Sequent GitHub.
  - Init-Reihenfolge: 1. Output-Latch auf 0x00 (Relais AUS, Pins noch Input),
                      2. Config-Register auf 0x00 (Pins werden Outputs, starten bei 0).
    Diese Reihenfolge verhindert ein kurzes Einschalten der Relais beim Start
    (Default-Config nach Power-on: 0xFF = alle Inputs, Output-Latch: 0xFF).
  - Relay-Zustand wird als Bitmask in-memory gehalten (kein I2C-Readback).
  - 8relay:  Hardware-Remap erforderlich – PCA9554-Pins sind NICHT linear verdrahtet
             (_REL8_RELAY_MASK, Quelle: relay8.h, Sequent GitHub). Ohne Remap werden
             falsche physikalische Relais aktiviert.
  - 16relay: Lineare Verdrahtung. Bit 0 = Zone 1, Bit N-1 = Zone N.
  - Bit=1: Relais zieht an (Ventil öffnet). Bit=0: Relais fällt ab.
  - close_all() ist best-effort: schreibt Register einzeln, loggt Fehler, wirft nicht.
  - cleanup() schließt den SMBus-Handle – IMMER nach close_all() aufrufen.

Warum lgpio statt RPi.GPIO:
  Der Raspberry Pi 5 verwendet den neuen RP1-I/O-Controller. RPi.GPIO 0.7.x
  unterstützt diesen Chip nicht. rpi-lgpio stellt die lgpio-API bereit.

Singleton-Pattern:
  get_valve_driver()   – gibt die globale Instanz zurück (lazy init)
  reset_valve_driver() – setzt die Instanz zurück (für Tests / Reload nach Config-Änderung)
  set_valve_driver()   – setzt eine vordefinierte Instanz (für Tests)

ALLE Hardware-Operationen müssen über den IO-Worker-Thread laufen (services/io_worker.py).
Den Treiber NIEMALS direkt aus dem Main-Thread oder unter state_lock aufrufen.
"""

from __future__ import annotations
from typing import Dict, Any, List
import os
from dataclasses import dataclass
from core.logging import log_event


class ValveDriverError(RuntimeError):
    """Fehler bei einer Hardware-Operation (open/close/close_all/init)."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# I2C-Register-Konstanten (Sequent Microsystems Relay HATs)
# Quelle: relay.h (16relind) und relay8.h (8relind) auf GitHub
# ─────────────────────────────────────────────────────────────────────────────

# 16-Relay HAT – PCA9555-kompatibler 16-Bit I/O-Expander
_REL16_OUTPORT_REG_LO = 0x02   # RELAY16_OUTPORT_REG_ADD → Port 0 (Relais 1–8)
_REL16_OUTPORT_REG_HI = 0x03   # RELAY16_OUTPORT_REG_ADD+1 → Port 1 (Relais 9–16)
_REL16_CFG_REG_LO     = 0x06   # RELAY16_CFG_REG_ADD → Port 0 Config (0x00 = Ausgang)
_REL16_CFG_REG_HI     = 0x07   # RELAY16_CFG_REG_ADD+1 → Port 1 Config (0x00 = Ausgang)

# 8-Relay HAT – PCA9554-kompatibler 8-Bit I/O-Expander
_REL8_OUTPORT_REG     = 0x01   # RELAY8_OUTPORT_REG_ADD → Port (Relais 1–8)
_REL8_CFG_REG         = 0x03   # RELAY8_CFG_REG_ADD → Config (0x00 = Ausgang)

# 8-Relay HAT – Hardware-Remap: PCA9554-Pins sind auf der Platine NICHT linear
# mit den physikalischen Relais verdrahtet. Index 0 = Zone 1, Index 7 = Zone 8.
# Quelle: relayMaskRemap[] in relay8.h (8relind), Sequent Microsystems GitHub.
_REL8_RELAY_MASK: tuple[int, ...] = (
    0x01,  # Zone 1 → Bit 0
    0x04,  # Zone 2 → Bit 2
    0x10,  # Zone 3 → Bit 4
    0x40,  # Zone 4 → Bit 6
    0x80,  # Zone 5 → Bit 7
    0x20,  # Zone 6 → Bit 5
    0x08,  # Zone 7 → Bit 3
    0x02,  # Zone 8 → Bit 1
)


# ─────────────────────────────────────────────────────────────────────────────
# Validierungsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

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


def validate_i2c_config(
    hat_type: str,
    i2c_bus: int,
    i2c_address: int,
    max_valves: int,
) -> Dict[str, Any]:
    """Validiert I2C-Konfiguration für I2cRelayValveDriver.

    Returns a dict with keys:
      - ok: bool
      - errors: list[str]

    Gültige Adressbereiche (Sequent Microsystems):
      16relay: 0x20–0x27 (Primäradresse, entspricht Stack 0–7)
       8relay: 0x38–0x3F (Primäradresse)  ODER  0x20–0x27 (Alternativadresse)
    """
    errors: List[str] = []

    if hat_type not in ("8relay", "16relay"):
        errors.append(f"Unbekannter hat_type: '{hat_type}' (erlaubt: '8relay', '16relay')")

    if i2c_bus not in (0, 1):
        errors.append(f"i2c_bus={i2c_bus} ungültig (erlaubt: 0 oder 1)")

    # Adressvalidierung nur wenn hat_type gültig
    if hat_type == "16relay":
        if not (0x20 <= i2c_address <= 0x27):
            errors.append(
                f"I2C-Adresse 0x{i2c_address:02X} ungültig für 16-Relay HAT "
                f"(erlaubt: 0x20–0x27)"
            )
        if max_valves > 16:
            errors.append(
                f"max_valves={max_valves} überschreitet Kapazität des 16-Relay HAT (max. 16)"
            )
    elif hat_type == "8relay":
        primary   = (0x38 <= i2c_address <= 0x3F)
        alternate = (0x20 <= i2c_address <= 0x27)
        if not (primary or alternate):
            errors.append(
                f"I2C-Adresse 0x{i2c_address:02X} ungültig für 8-Relay HAT "
                f"(erlaubt: 0x38–0x3F oder 0x20–0x27)"
            )
        if max_valves > 8:
            errors.append(
                f"max_valves={max_valves} überschreitet Kapazität des 8-Relay HAT (max. 8)"
            )

    return {"ok": len(errors) == 0, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Basis-Klasse
# ─────────────────────────────────────────────────────────────────────────────

class BaseValveDriver:
    """Hardware-Abstraktion für Ventile."""

    name: str = "base"

    def open(self, zone: int) -> None:
        raise NotImplementedError

    def close(self, zone: int) -> None:
        raise NotImplementedError

    def close_all(self) -> None:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Gibt Hardware-Ressourcen frei.
        Default ist ein No-Op – Unterklassen überschreiben bei Bedarf.
        Muss NACH close_all() aufgerufen werden.
        """
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Simulations-Treiber
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SimValveDriver(BaseValveDriver):
    """
    Simulation: macht nichts an GPIO/I2C, loggt aber die Calls.
    Ideal für Windows/Dev/Tests.
    """
    name: str = "sim"

    def open(self, zone: int) -> None:
        log_event("valve_hw_open", source="driver", driver=self.name, zone=int(zone))

    def close(self, zone: int) -> None:
        log_event("valve_hw_close", source="driver", driver=self.name, zone=int(zone))

    def close_all(self) -> None:
        log_event("valve_hw_close_all", source="driver", driver=self.name)


# ─────────────────────────────────────────────────────────────────────────────
# GPIO-Treiber (Raspberry Pi direkt)
# ─────────────────────────────────────────────────────────────────────────────

class RpiGpioValveDriver(BaseValveDriver):
    """
    Raspberry Pi GPIO Driver via lgpio / rpi-lgpio (BCM numbering).

    Kompatibel mit Raspberry Pi 5 (RP1-Chip). Verwendet lgpio statt RPi.GPIO,
    da RPi.GPIO 0.7.x den RP1-I/O-Controller nicht unterstützt.

    Relay boards sind oft active-low (LOW = Relais zieht an).
    """
    name: str = "rpi"

    def __init__(self, pins_by_zone: Dict[int, int], active_low: bool):
        self._pins_by_zone = dict(pins_by_zone)
        self._active_low = bool(active_low)

        try:
            import lgpio  # type: ignore
        except Exception as e:
            raise ValveDriverError(f"lgpio nicht verfügbar: {e}")

        self._lgpio = lgpio

        # gpiochip 0 ist auf allen Pi-Modellen der primäre GPIO-Chip.
        try:
            self._handle = lgpio.gpiochip_open(0)
        except Exception as e:
            raise ValveDriverError(f"GPIO-Chip konnte nicht geöffnet werden: {e}")

        # gpio_claim_output() setzt Richtung UND Initialwert atomar – kein Race
        # zwischen claim und erstem write(). Bei Active-Low-Boards bedeutet
        # "geschlossen" (de-energized) = HIGH (1). Bei Active-High = LOW (0).
        initial_closed = self._closed_val()
        for zone, pin in sorted(self._pins_by_zone.items()):
            try:
                lgpio.gpio_claim_output(self._handle, int(pin), initial_closed)
            except Exception as e:
                # Chip-Handle freigeben bevor wir die Exception weiterwerfen,
                # damit kein Ressourcen-Leak entsteht.
                try:
                    lgpio.gpiochip_close(self._handle)
                except Exception:
                    pass
                raise ValveDriverError(
                    f"Pin BCM {pin} (Zone {zone}) konnte nicht konfiguriert werden: {e}"
                )

        log_event(
            "valve_driver_gpio_setup",
            source="driver",
            driver=self.name,
            active_low=self._active_low,
            zones=sorted(list(self._pins_by_zone.keys())),
        )

    def _open_val(self) -> int:
        return 0 if self._active_low else 1

    def _closed_val(self) -> int:
        return 1 if self._active_low else 0

    def _write_open(self, pin: int) -> None:
        self._lgpio.gpio_write(self._handle, pin, self._open_val())

    def _write_closed(self, pin: int) -> None:
        self._lgpio.gpio_write(self._handle, pin, self._closed_val())

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
        """Gibt den GPIO-Chip-Handle frei.

        Muss nach close_all() aufgerufen werden – niemals davor, da
        gpiochip_close() die Pin-Kontrolle sofort abgibt.
        """
        try:
            self._lgpio.gpiochip_close(self._handle)
            log_event("valve_driver_gpio_cleanup", source="driver", driver=self.name)
        except Exception as e:
            log_event(
                "valve_driver_gpio_cleanup_error",
                level="error",
                source="driver",
                driver=self.name,
                error=repr(e),
            )


# ─────────────────────────────────────────────────────────────────────────────
# I2C-Treiber (Sequent Microsystems Relay HAT)
# ─────────────────────────────────────────────────────────────────────────────

class I2cRelayValveDriver(BaseValveDriver):
    """
    Sequent Microsystems Relay HAT via I2C/SMBus (smbus2).

    Unterstützte Modelle:
      hat_type="8relay"  – Sequent 8-Relay HAT  (PCA9554, Primäradresse 0x38–0x3F)
      hat_type="16relay" – Sequent 16-Relay HAT (PCA9555, Primäradresse 0x20–0x27)

    Relay-Bitmask (in-memory):
      Bit 0 = Zone 1, ..., Bit N-1 = Zone N.
      Bit=1: Relais zieht an (Ventil öffnet). Bit=0: Relais fällt ab.

    Initialisierungsreihenfolge (sicherheitskritisch!):
      1. Output-Latch auf 0x00 setzen (Relais AUS, Pins noch im Input-Modus).
      2. Config-Register auf 0x00 (Pins werden zu Ausgängen, starten mit Latch=0).
      Diese Reihenfolge verhindert ein kurzes Einschalten aller Relais beim Start,
      da PCA9555/9554 nach Power-on mit Config=0xFF (alle Inputs) und
      Output-Latch=0xFF (alle HIGH) starten.

    Der SMBus-Handle wird beim Init geöffnet und in cleanup() geschlossen.
    ALLE Operationen müssen über den IO-Worker-Thread laufen (io_worker.py).
    """
    name: str = "i2c"

    def __init__(
        self,
        hat_type: str,
        i2c_bus: int,
        i2c_address: int,
        num_zones: int,
    ):
        """
        Args:
            hat_type:    "8relay" oder "16relay"
            i2c_bus:     I2C-Busnummer (0 oder 1, typisch 1 = /dev/i2c-1)
            i2c_address: I2C-Adresse als Dezimalzahl (z.B. 32 = 0x20, 56 = 0x38)
            num_zones:   Anzahl der konfigurierten Ventil-Zonen (1–8 oder 1–16)
        """
        try:
            import smbus2  # type: ignore
        except Exception as e:
            raise ValveDriverError(f"smbus2 nicht verfügbar: {e}")

        self._hat_type  = hat_type
        self._addr      = int(i2c_address)
        self._num_zones = int(num_zones)
        self._state     = 0   # Bitmask: Bit 0 = Zone 1, Bit N-1 = Zone N
        self._bus       = None  # Explizit None für sauberes Cleanup bei Part-Init

        try:
            self._bus = smbus2.SMBus(int(i2c_bus))
        except Exception as e:
            raise ValveDriverError(
                f"I2C-Bus {i2c_bus} konnte nicht geöffnet werden: {e}"
            )

        try:
            self._init_hardware()
        except Exception as e:
            self._safe_close_bus()
            raise ValveDriverError(
                f"I2C-Hardware-Initialisierung fehlgeschlagen (HAT: {hat_type}, "
                f"Adresse: 0x{i2c_address:02X}): {e}"
            )

        log_event(
            "valve_driver_i2c_setup",
            source="driver",
            driver=self.name,
            hat_type=hat_type,
            i2c_bus=i2c_bus,
            i2c_address=hex(self._addr),
            num_zones=num_zones,
        )

    def _init_hardware(self) -> None:
        """Initialisiert I/O-Register: Output-Latch auf 0x00, dann Config auf 0x00.

        Reihenfolge ist sicherheitskritisch: Output-Latch MUSS vor dem
        Config-Register geschrieben werden, damit die Pins beim Wechsel
        auf Output-Modus sofort den Wert 0 (Relais AUS) annehmen.

        PCA9555/9554 Default nach Power-on: Config=0xFF (alle Inputs),
        Output-Latch=0xFF → ohne korrekte Init würden alle Relais kurz einschalten.
        """
        if self._hat_type == "8relay":
            # 1. Output-Latch = 0 (Relais AUS, Pins noch Input → kein HW-Effekt)
            self._bus.write_byte_data(self._addr, _REL8_OUTPORT_REG, 0x00)
            # 2. Config = 0 (Pins werden Ausgänge, starten mit Latch=0)
            self._bus.write_byte_data(self._addr, _REL8_CFG_REG, 0x00)
        else:  # 16relay
            # 1. Beide Output-Latches = 0
            self._bus.write_byte_data(self._addr, _REL16_OUTPORT_REG_LO, 0x00)
            self._bus.write_byte_data(self._addr, _REL16_OUTPORT_REG_HI, 0x00)
            # 2. Beide Config-Register = 0
            self._bus.write_byte_data(self._addr, _REL16_CFG_REG_LO, 0x00)
            self._bus.write_byte_data(self._addr, _REL16_CFG_REG_HI, 0x00)

    def _write_state(self) -> None:
        """Schreibt den aktuellen _state-Bitmask in die I2C-Ausgangsregister."""
        if self._hat_type == "8relay":
            self._bus.write_byte_data(
                self._addr, _REL8_OUTPORT_REG, self._state & 0xFF
            )
        else:  # 16relay
            self._bus.write_byte_data(
                self._addr, _REL16_OUTPORT_REG_LO, self._state & 0xFF
            )
            self._bus.write_byte_data(
                self._addr, _REL16_OUTPORT_REG_HI, (self._state >> 8) & 0xFF
            )

    def _zone_bitmask(self, zone: int) -> int:
        """Gibt die Hardware-Bitmask für eine Zone zurück.

        8relay:  Die PCA9554-Pins sind auf der Platine nicht linear verdrahtet.
                 _REL8_RELAY_MASK bildet Zone 1–8 auf die korrekten Hardware-Bits ab.
                 Ohne diesen Remap würden falsche physikalische Relais aktiviert.
        16relay: Lineare Verdrahtung – Bit 0 = Zone 1, Bit N-1 = Zone N.
        """
        if self._hat_type == "8relay":
            return _REL8_RELAY_MASK[zone - 1]
        return 1 << (zone - 1)

    def _safe_close_bus(self) -> None:
        """Schließt den SMBus-Handle ohne Exception zu werfen (für Cleanup bei Fehler)."""
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def open(self, zone: int) -> None:
        if zone < 1 or zone > self._num_zones:
            raise ValveDriverError(
                f"Zone {zone} nicht konfiguriert "
                f"(konfigurierte Zonen: 1–{self._num_zones})"
            )
        self._state |= self._zone_bitmask(zone)
        self._write_state()
        log_event("valve_hw_open", source="driver", driver=self.name, zone=int(zone))

    def close(self, zone: int) -> None:
        if zone < 1 or zone > self._num_zones:
            raise ValveDriverError(
                f"Zone {zone} nicht konfiguriert "
                f"(konfigurierte Zonen: 1–{self._num_zones})"
            )
        self._state &= ~self._zone_bitmask(zone)
        self._write_state()
        log_event("valve_hw_close", source="driver", driver=self.name, zone=int(zone))

    def close_all(self) -> None:
        """Setzt alle Relais auf AUS (Ventile schließen).

        Best-effort: In-memory-State wird immer auf 0 gesetzt (auch bei HW-Fehler).
        Jeder Register-Schreibvorgang wird einzeln versucht und separat geloggt.
        Wirft keine Exceptions – close_all() ist immer ein Sicherheits-Versuch.
        """
        # In-memory State ZUERST zurücksetzen – auch wenn HW-Writes fehlschlagen,
        # ist der interne Zustand korrekt (nächster Aufruf schreibt dann 0).
        self._state = 0

        # Register-Schreibvorgänge je nach HAT-Typ
        if self._hat_type == "8relay":
            writes = [(_REL8_OUTPORT_REG, 0x00)]
        else:  # 16relay
            writes = [
                (_REL16_OUTPORT_REG_LO, 0x00),
                (_REL16_OUTPORT_REG_HI, 0x00),
            ]

        errors: list[str] = []
        for reg, val in writes:
            try:
                self._bus.write_byte_data(self._addr, reg, val)
            except Exception as e:
                err_str = repr(e)
                errors.append(err_str)
                log_event(
                    "valve_hw_close_all_reg_error",
                    level="error",
                    source="driver",
                    driver=self.name,
                    register=hex(reg),
                    error=err_str,
                )

        log_event(
            "valve_hw_close_all",
            source="driver",
            driver=self.name,
            failed_count=len(errors),
            errors=errors,
        )

    def cleanup(self) -> None:
        """Schließt den SMBus-Handle.

        Muss nach close_all() aufgerufen werden. Wirft keine Exceptions.
        """
        try:
            if self._bus is not None:
                self._bus.close()
            log_event("valve_driver_i2c_cleanup", source="driver", driver=self.name)
        except Exception as e:
            log_event(
                "valve_driver_i2c_cleanup_error",
                level="error",
                source="driver",
                driver=self.name,
                error=repr(e),
            )
        finally:
            self._bus = None


# ─────────────────────────────────────────────────────────────────────────────
# Singleton / Accessor
# ─────────────────────────────────────────────────────────────────────────────

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
    """Für Tests/Dev: erlaubt gezieltes Setzen eines Drivers."""
    global _driver
    _driver = driver
    log_event("valve_driver_set", source="driver", driver=getattr(driver, "name", "unknown"))


def _read_driver_settings_from_state() -> dict[str, Any]:
    """Liest Driver-Settings aus dem globalen State.

    Der State wird von load_device_config_from_disk() (persistence.py) befüllt.
    Diese Funktion ist die Brücke zwischen Persistence und Driver-Initialisierung.
    """
    try:
        from core.state import state, state_lock
        with state_lock:
            return {
                "mode":        getattr(state, "valve_driver_mode", None),
                "active_low":  getattr(state, "relay_active_low", None),
                "pins":        getattr(state, "gpio_pins_by_zone", None),
                "hat_type":    getattr(state, "relay_hat_type", "16relay"),
                "i2c_bus":     getattr(state, "i2c_bus", 1),
                "i2c_address": getattr(state, "i2c_address", 0x20),
            }
    except Exception:
        return {
            "mode": None, "active_low": None, "pins": None,
            "hat_type": "16relay", "i2c_bus": 1, "i2c_address": 0x20,
        }


def get_valve_driver() -> BaseValveDriver:
    """Gibt die globale Valve-Driver-Instanz zurück (lazy singleton).

    Reihenfolge:
      1) ENV override (wenn gesetzt)
      2) device_config.json/state
      3) fallback = sim

    Bei Fehlern in der Initialisierung (z.B. I2C/lgpio nicht verfügbar,
    Konfigurationsfehler) → sicherer Fallback auf SimValveDriver mit Error-Log.
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
            log_event(
                "valve_driver_init", source="driver",
                driver=_driver.name, mode=mode, env_override=bool(env_mode),
            )
            return _driver

        if mode == "rpi":
            if not pins:
                raise ValveDriverError("IRRIGATION_GPIO_PINS ist leer/fehlt in device_config.json")

            pins_by_zone: Dict[int, int] = {}
            for k, v in pins.items():
                z = int(k)
                p = int(v)
                pins_by_zone[z] = p

            vres = validate_gpio_pins(pins_by_zone)
            if not vres.get("ok"):
                raise ValveDriverError(f"Ungültige GPIO Pin-Konfiguration: {vres}")

            # Vollständige Pin-Abdeckung 1..max_valves prüfen
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
            log_event(
                "valve_driver_init", source="driver",
                driver=_driver.name, mode=mode, env_override=bool(env_mode),
            )
            return _driver

        if mode == "i2c":
            hat_type = (st.get("hat_type") or "16relay").strip().lower()
            if hat_type not in ("8relay", "16relay"):
                hat_type = "16relay"

            try:
                i2c_bus = int(st.get("i2c_bus", 1))
            except (TypeError, ValueError):
                i2c_bus = 1

            try:
                i2c_address = int(st.get("i2c_address", 0x20))
            except (TypeError, ValueError):
                i2c_address = 0x20

            try:
                from core.state import state, state_lock
                with state_lock:
                    max_valves = int(getattr(state, "max_valves", 1))
            except Exception:
                max_valves = 1

            vres = validate_i2c_config(hat_type, i2c_bus, i2c_address, max_valves)
            if not vres.get("ok"):
                raise ValveDriverError(
                    f"Ungültige I2C-Konfiguration: {vres['errors']}"
                )

            _driver = I2cRelayValveDriver(
                hat_type=hat_type,
                i2c_bus=i2c_bus,
                i2c_address=i2c_address,
                num_zones=max_valves,
            )
            log_event(
                "valve_driver_init", source="driver",
                driver=_driver.name, mode=mode, env_override=bool(env_mode),
            )
            return _driver

        # Unbekannter Modus → sicherer Fallback
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
        # Jeder Init-Fehler → sicherer Fallback auf sim
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
