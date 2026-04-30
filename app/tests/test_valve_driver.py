"""
Tests für services/valve_driver.py

Getestet werden:
  - validate_gpio_pins         (valide Konfiguration, Fehler, Duplikate)
  - validate_i2c_config        (hat_type, Adressbereiche, max_valves-Limits)
  - SimValveDriver             (open/close/close_all – kein Fehler erwartet)
  - RpiGpioValveDriver         (Logik: active_low, Pin-Mapping, Fehlerbehandlung)
  - I2cRelayValveDriver        (Init, open/close, Bitmask-Logik, close_all, cleanup)
  - get_valve_driver           (sim, rpi, i2c – inkl. Fallback bei Fehler)

Mock-Strategie für RpiGpioValveDriver:
  lgpio wird via sys.modules["lgpio"] eingehängt bevor der Import erfolgt,
  und danach wieder entfernt damit andere Tests nicht beeinflusst werden.

Mock-Strategie für I2cRelayValveDriver:
  Identisches Muster: sys.modules["smbus2"] wird vor dem __init__ gesetzt
  und danach wieder entfernt. bus_mock (der Rückgabewert von smbus2.SMBus())
  wird für Assertions auf write_byte_data verwendet.
"""

import sys
import pytest
from unittest.mock import MagicMock, call, patch

from services.valve_driver import (
    validate_gpio_pins,
    validate_i2c_config,
    SimValveDriver,
    RpiGpioValveDriver,
    I2cRelayValveDriver,
    get_valve_driver,
    reset_valve_driver,
    set_valve_driver,
    ValveDriverError,
    # Register-Konstanten (direkt importiert für Assertions)
    _REL16_OUTPORT_REG_LO,
    _REL16_OUTPORT_REG_HI,
    _REL16_CFG_REG_LO,
    _REL16_CFG_REG_HI,
    _REL8_OUTPORT_REG,
    _REL8_CFG_REG,
    _REL8_RELAY_MASK,
    _REL16_RELAY_MASK,
)
from core.state import state, state_lock

# Fester Test-Handle, den gpiochip_open() zurückgeben soll
_TEST_HANDLE = 42


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: Erstellt einen gemockten RpiGpioValveDriver
# ─────────────────────────────────────────────────────────────────────────────

def _make_rpi_driver(
    pins_by_zone: dict[int, int],
    active_low: bool = True,
) -> tuple[RpiGpioValveDriver, MagicMock]:
    """
    Instanziiert RpiGpioValveDriver mit einem vollständig gemockten lgpio-Modul.

    Gibt (driver, lgpio_mock) zurück, damit Tests auf
    lgpio_mock.gpio_write, lgpio_mock.gpio_claim_output etc. prüfen können.

    gpiochip_open() gibt _TEST_HANDLE zurück, damit der Handle-Wert in
    allen Write-/Cleanup-Assertions konsistent ist.
    """
    lgpio_mock = MagicMock()
    lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE

    sys.modules["lgpio"] = lgpio_mock
    try:
        driver = RpiGpioValveDriver(pins_by_zone=pins_by_zone, active_low=active_low)
    finally:
        sys.modules.pop("lgpio", None)

    # Nach der Instanziierung bleibt lgpio_mock im driver._lgpio – alle
    # späteren Aufrufe (open/close/cleanup) gehen durch diesen Mock.
    return driver, lgpio_mock


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: Erstellt einen gemockten I2cRelayValveDriver
# ─────────────────────────────────────────────────────────────────────────────

def _make_i2c_driver(
    hat_type: str = "16relay",
    i2c_bus: int = 1,
    i2c_address: int = 0x20,
    num_zones: int = 4,
) -> tuple[I2cRelayValveDriver, MagicMock]:
    """
    Instanziiert I2cRelayValveDriver mit einem vollständig gemockten smbus2-Modul.

    Gibt (driver, bus_mock) zurück. bus_mock ist der Rückgabewert von
    smbus2.SMBus() – alle write_byte_data/close-Aufrufe gehen darüber.

    smbus2 wird nach dem Init aus sys.modules entfernt damit andere Tests
    nicht beeinflusst werden. Der driver._bus referenziert weiterhin bus_mock.
    """
    smbus2_mock = MagicMock()
    bus_mock = MagicMock()
    smbus2_mock.SMBus.return_value = bus_mock

    sys.modules["smbus2"] = smbus2_mock
    try:
        driver = I2cRelayValveDriver(hat_type, i2c_bus, i2c_address, num_zones)
    finally:
        sys.modules.pop("smbus2", None)

    return driver, bus_mock


# ─────────────────────────────────────────────────────────────────────────────
# validate_gpio_pins
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateGpioPins:
    def test_valid_pins(self):
        pins = {1: 17, 2: 18, 3: 27, 4: 22}
        result = validate_gpio_pins(pins)
        assert result["ok"] is True
        assert result["invalid_pins"] == []
        assert result["duplicate_pins"] == []

    def test_empty_pins_is_valid(self):
        result = validate_gpio_pins({})
        assert result["ok"] is True

    def test_pin_out_of_range_low(self):
        result = validate_gpio_pins({1: 1})  # Pin 1 < 2 → ungültig
        assert result["ok"] is False
        assert len(result["invalid_pins"]) == 1
        assert result["invalid_pins"][0]["reason"] == "out_of_range_2_27"

    def test_pin_out_of_range_high(self):
        result = validate_gpio_pins({1: 28})  # Pin 28 > 27 → ungültig
        assert result["ok"] is False
        assert len(result["invalid_pins"]) == 1

    def test_duplicate_pins(self):
        pins = {1: 17, 2: 17}  # Beide Zonen nutzen Pin 17
        result = validate_gpio_pins(pins)
        assert result["ok"] is False
        assert len(result["duplicate_pins"]) == 1
        assert result["duplicate_pins"][0]["pin"] == 17
        assert sorted(result["duplicate_pins"][0]["zones"]) == [1, 2]

    def test_multiple_errors(self):
        pins = {1: 0, 2: 17, 3: 17}  # Pin 0 out of range + Duplikat
        result = validate_gpio_pins(pins)
        assert result["ok"] is False
        assert len(result["invalid_pins"]) >= 1
        assert len(result["duplicate_pins"]) >= 1

    def test_boundary_pin_2_is_valid(self):
        result = validate_gpio_pins({1: 2})
        assert result["ok"] is True

    def test_boundary_pin_27_is_valid(self):
        result = validate_gpio_pins({1: 27})
        assert result["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# validate_i2c_config
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateI2cConfig:
    # ── 16relay ──────────────────────────────────────────────────────────────

    def test_16relay_valid_primary_address(self):
        """Gültige Primäradresse 0x20 für 16-Relay HAT."""
        result = validate_i2c_config("16relay", 1, 0x20, 8)
        assert result["ok"] is True
        assert result["errors"] == []

    def test_16relay_valid_max_address(self):
        """Gültige Maximaladresse 0x27 für 16-Relay HAT."""
        result = validate_i2c_config("16relay", 1, 0x27, 16)
        assert result["ok"] is True

    def test_16relay_valid_max_valves(self):
        """16 Zonen sind erlaubt für 16-Relay HAT."""
        result = validate_i2c_config("16relay", 1, 0x20, 16)
        assert result["ok"] is True

    def test_16relay_address_too_low(self):
        """Adresse unterhalb 0x20 ist für 16-Relay HAT ungültig."""
        result = validate_i2c_config("16relay", 1, 0x1F, 8)
        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert "0x1F" in result["errors"][0]

    def test_16relay_address_too_high(self):
        """Adresse 0x28 ist für 16-Relay HAT ungültig."""
        result = validate_i2c_config("16relay", 1, 0x28, 8)
        assert result["ok"] is False

    def test_16relay_max_valves_exceeded(self):
        """17 Zonen überschreiten die 16-Relay-Kapazität."""
        result = validate_i2c_config("16relay", 1, 0x20, 17)
        assert result["ok"] is False
        assert any("max_valves" in e or "16" in e for e in result["errors"])

    # ── 8relay ───────────────────────────────────────────────────────────────

    def test_8relay_valid_primary_address(self):
        """Gültige Primäradresse 0x38 für 8-Relay HAT."""
        result = validate_i2c_config("8relay", 1, 0x38, 4)
        assert result["ok"] is True
        assert result["errors"] == []

    def test_8relay_valid_max_primary_address(self):
        """Gültige Primäradresse 0x3F für 8-Relay HAT."""
        result = validate_i2c_config("8relay", 1, 0x3F, 8)
        assert result["ok"] is True

    def test_8relay_valid_alternate_address(self):
        """Alternativadresse 0x20 ist gültig für 8-Relay HAT."""
        result = validate_i2c_config("8relay", 1, 0x20, 4)
        assert result["ok"] is True

    def test_8relay_valid_max_alternate_address(self):
        """Alternativadresse 0x27 ist gültig für 8-Relay HAT."""
        result = validate_i2c_config("8relay", 1, 0x27, 8)
        assert result["ok"] is True

    def test_8relay_invalid_address(self):
        """Adresse 0x30 liegt weder im Primär- noch im Alternativbereich."""
        result = validate_i2c_config("8relay", 1, 0x30, 4)
        assert result["ok"] is False
        assert "0x30" in result["errors"][0]

    def test_8relay_max_valves_exceeded(self):
        """9 Zonen überschreiten die 8-Relay-Kapazität."""
        result = validate_i2c_config("8relay", 1, 0x38, 9)
        assert result["ok"] is False

    def test_8relay_valid_max_valves(self):
        """8 Zonen sind erlaubt für 8-Relay HAT."""
        result = validate_i2c_config("8relay", 1, 0x38, 8)
        assert result["ok"] is True

    # ── Allgemein ─────────────────────────────────────────────────────────────

    def test_invalid_hat_type(self):
        """Unbekannter hat_type muss als Fehler gemeldet werden."""
        result = validate_i2c_config("32relay", 1, 0x20, 4)
        assert result["ok"] is False
        assert any("hat_type" in e or "32relay" in e for e in result["errors"])

    def test_invalid_i2c_bus(self):
        """Nur I2C-Bus 0 und 1 sind gültig."""
        result = validate_i2c_config("16relay", 2, 0x20, 4)
        assert result["ok"] is False
        assert any("i2c_bus" in e or "bus" in e.lower() for e in result["errors"])

    def test_i2c_bus_0_is_valid(self):
        """I2C-Bus 0 ist explizit erlaubt."""
        result = validate_i2c_config("16relay", 0, 0x20, 4)
        assert result["ok"] is True

    def test_multiple_errors_combined(self):
        """Mehrere Fehler werden gemeinsam gemeldet."""
        result = validate_i2c_config("bad_type", 5, 0x10, 99)
        assert result["ok"] is False
        assert len(result["errors"]) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# SimValveDriver
# ─────────────────────────────────────────────────────────────────────────────


class TestSimValveDriver:
    def test_open_does_not_raise(self):
        drv = SimValveDriver()
        drv.open(1)

    def test_close_does_not_raise(self):
        drv = SimValveDriver()
        drv.close(1)

    def test_close_all_does_not_raise(self):
        drv = SimValveDriver()
        drv.close_all()

    def test_driver_name_is_sim(self):
        drv = SimValveDriver()
        assert drv.name == "sim"


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – Initialisierung
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverInit:
    def test_gpiochip_open_called_with_chip_0(self):
        """gpiochip_open(0) muss beim Init aufgerufen werden."""
        driver, lgpio = _make_rpi_driver({1: 17})
        lgpio.gpiochip_open.assert_called_once_with(0)

    def test_all_pins_claimed_as_output(self):
        """gpio_claim_output() muss für jeden konfigurierten Pin aufgerufen werden."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 18, 3: 27})
        claimed_pins = [c.args[1] for c in lgpio.gpio_claim_output.call_args_list]
        assert sorted(claimed_pins) == [17, 18, 27]

    def test_all_pins_use_correct_handle(self):
        """gpio_claim_output() muss mit dem von gpiochip_open() zurückgegebenen Handle aufgerufen werden."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 18})
        for c in lgpio.gpio_claim_output.call_args_list:
            assert c.args[0] == _TEST_HANDLE

    def test_all_pins_initialized_to_closed_active_low(self):
        """
        Mit active_low=True: 'geschlossen' = HIGH (1) = Relais de-energized.
        gpio_claim_output() muss atomar mit initial=1 aufgerufen werden,
        damit der Pin nie unbeabsichtigt LOW geht.
        """
        driver, lgpio = _make_rpi_driver({1: 17, 2: 18}, active_low=True)
        for c in lgpio.gpio_claim_output.call_args_list:
            initial = c.args[2]
            assert initial == 1, (
                f"Pin {c.args[1]}: erwartet initial=1 (HIGH), bekommen: {initial}"
            )
        # gpio_write darf beim Init NICHT aufgerufen werden (claim ist atomar)
        lgpio.gpio_write.assert_not_called()

    def test_all_pins_initialized_to_closed_active_high(self):
        """
        Mit active_low=False: 'geschlossen' = LOW (0) = Relais de-energized.
        """
        driver, lgpio = _make_rpi_driver({1: 17, 2: 18}, active_low=False)
        for c in lgpio.gpio_claim_output.call_args_list:
            initial = c.args[2]
            assert initial == 0, (
                f"Pin {c.args[1]}: erwartet initial=0 (LOW), bekommen: {initial}"
            )
        lgpio.gpio_write.assert_not_called()

    def test_driver_name_is_rpi(self):
        driver, _ = _make_rpi_driver({1: 17})
        assert driver.name == "rpi"

    def test_lgpio_not_available_raises_valve_driver_error(self):
        """Wenn lgpio nicht importiert werden kann, muss ValveDriverError geworfen werden."""
        sys.modules.pop("lgpio", None)
        with patch.dict("sys.modules", {"lgpio": None}):
            with pytest.raises(ValveDriverError, match="lgpio"):
                RpiGpioValveDriver(pins_by_zone={1: 17}, active_low=True)

    def test_gpiochip_open_failure_raises_valve_driver_error(self):
        """Wenn gpiochip_open() fehlschlägt, muss ValveDriverError geworfen werden."""
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.side_effect = RuntimeError("Chip nicht gefunden")
        sys.modules["lgpio"] = lgpio_mock
        try:
            with pytest.raises(ValveDriverError, match="GPIO-Chip"):
                RpiGpioValveDriver(pins_by_zone={1: 17}, active_low=True)
        finally:
            sys.modules.pop("lgpio", None)

    def test_pin_claim_failure_closes_chip_handle(self):
        """
        Wenn gpio_claim_output() für einen Pin fehlschlägt, muss gpiochip_close()
        aufgerufen werden, damit kein Chip-Handle-Leak entsteht.
        """
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE
        lgpio_mock.gpio_claim_output.side_effect = RuntimeError("Pin belegt")
        sys.modules["lgpio"] = lgpio_mock
        try:
            with pytest.raises(ValveDriverError):
                RpiGpioValveDriver(pins_by_zone={1: 17}, active_low=True)
            lgpio_mock.gpiochip_close.assert_called_once_with(_TEST_HANDLE)
        finally:
            sys.modules.pop("lgpio", None)


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – open()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverOpen:
    def test_open_active_low_sends_gpio_low(self):
        """
        active_low=True: Relais zieht an wenn Pin LOW (0).
        open() muss gpio_write(handle, pin, 0) senden.
        """
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.open(1)

        lgpio.gpio_write.assert_called_once_with(_TEST_HANDLE, 17, 0)

    def test_open_active_high_sends_gpio_high(self):
        """
        active_low=False: Relais zieht an wenn Pin HIGH (1).
        open() muss gpio_write(handle, pin, 1) senden.
        """
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=False)
        lgpio.gpio_write.reset_mock()

        driver.open(1)

        lgpio.gpio_write.assert_called_once_with(_TEST_HANDLE, 17, 1)

    def test_open_uses_correct_pin_for_zone(self):
        """Zone → Pin Mapping muss korrekt aufgelöst werden."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.open(2)

        called_pins = [c.args[1] for c in lgpio.gpio_write.call_args_list]
        assert called_pins == [22]

    def test_open_unknown_zone_raises_valve_driver_error(self):
        """Eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)

        with pytest.raises(ValveDriverError, match="zone=99"):
            driver.open(99)

    def test_open_unknown_zone_does_not_touch_gpio(self):
        """Bei unbekannter Zone darf kein GPIO-Output erfolgen."""
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)
        lgpio.gpio_write.reset_mock()

        with pytest.raises(ValveDriverError):
            driver.open(99)

        lgpio.gpio_write.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – close()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverClose:
    def test_close_active_low_sends_gpio_high(self):
        """
        active_low=True: Relais fällt ab wenn Pin HIGH (1).
        close() muss gpio_write(handle, pin, 1) senden.
        """
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.close(1)

        lgpio.gpio_write.assert_called_once_with(_TEST_HANDLE, 17, 1)

    def test_close_active_high_sends_gpio_low(self):
        """
        active_low=False: Relais fällt ab wenn Pin LOW (0).
        close() muss gpio_write(handle, pin, 0) senden.
        """
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=False)
        lgpio.gpio_write.reset_mock()

        driver.close(1)

        lgpio.gpio_write.assert_called_once_with(_TEST_HANDLE, 17, 0)

    def test_close_uses_correct_pin_for_zone(self):
        """Zone → Pin Mapping muss korrekt aufgelöst werden."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.close(3)

        called_pins = [c.args[1] for c in lgpio.gpio_write.call_args_list]
        assert called_pins == [27]

    def test_close_unknown_zone_raises_valve_driver_error(self):
        """Eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)

        with pytest.raises(ValveDriverError, match="zone=5"):
            driver.close(5)

    def test_close_unknown_zone_does_not_touch_gpio(self):
        """Bei unbekannter Zone darf kein GPIO-Output erfolgen."""
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)
        lgpio.gpio_write.reset_mock()

        with pytest.raises(ValveDriverError):
            driver.close(5)

        lgpio.gpio_write.assert_not_called()

    def test_open_then_close_inverts_signal_active_low(self):
        """
        Kritischer Integrationstest: open() und close() müssen entgegengesetzte
        Signale senden. Verwechslung würde Ventile dauerhaft offen lassen.
        """
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.open(1)
        open_val = lgpio.gpio_write.call_args_list[0].args[2]

        lgpio.gpio_write.reset_mock()
        driver.close(1)
        close_val = lgpio.gpio_write.call_args_list[0].args[2]

        assert open_val != close_val, (
            f"open() und close() senden dasselbe Signal ({open_val})! "
            "Das würde das Ventil dauerhaft offen lassen."
        )

    def test_open_then_close_inverts_signal_active_high(self):
        """Dasselbe für active_low=False."""
        driver, lgpio = _make_rpi_driver({1: 17}, active_low=False)
        lgpio.gpio_write.reset_mock()

        driver.open(1)
        open_val = lgpio.gpio_write.call_args_list[0].args[2]

        lgpio.gpio_write.reset_mock()
        driver.close(1)
        close_val = lgpio.gpio_write.call_args_list[0].args[2]

        assert open_val != close_val


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – close_all()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverCloseAll:
    def test_close_all_touches_all_pins(self):
        """close_all() muss jeden konfigurierten Pin ansprechen."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.close_all()

        called_pins = sorted([c.args[1] for c in lgpio.gpio_write.call_args_list])
        assert called_pins == [17, 22, 27]

    def test_close_all_sends_closed_signal_active_low(self):
        """close_all() muss mit active_low=True alle Pins auf HIGH (1) setzen."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.close_all()

        for c in lgpio.gpio_write.call_args_list:
            assert c.args[2] == 1, f"Pin {c.args[1]} wurde nicht auf HIGH gesetzt"

    def test_close_all_sends_closed_signal_active_high(self):
        """close_all() muss mit active_low=False alle Pins auf LOW (0) setzen."""
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22}, active_low=False)
        lgpio.gpio_write.reset_mock()

        driver.close_all()

        for c in lgpio.gpio_write.call_args_list:
            assert c.args[2] == 0, f"Pin {c.args[1]} wurde nicht auf LOW gesetzt"

    def test_close_all_best_effort_continues_after_partial_failure(self):
        """
        close_all() ist best-effort: Schlägt ein Pin fehl, müssen die
        verbleibenden Pins trotzdem angesprochen werden.
        Dies ist sicherheitskritisch – im Fehlerfall sollen so viele
        Ventile wie möglich geschlossen werden.
        """
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        lgpio.gpio_write.reset_mock()

        def _write_with_failure(handle, pin, val):
            if pin == 22:
                raise RuntimeError("GPIO Schreibfehler")

        lgpio.gpio_write.side_effect = _write_with_failure

        # Darf keinen Fehler nach außen werfen
        driver.close_all()

        called_pins = sorted([c.args[1] for c in lgpio.gpio_write.call_args_list])
        assert 17 in called_pins
        assert 27 in called_pins

    def test_close_all_empty_mapping_does_not_raise(self):
        """Leeres Pin-Mapping darf nicht zu einem Fehler führen."""
        driver, lgpio = _make_rpi_driver({}, active_low=True)
        lgpio.gpio_write.reset_mock()

        driver.close_all()  # Darf nicht werfen

        lgpio.gpio_write.assert_not_called()

    def test_close_all_zone_failure_is_logged(self):
        """
        Wenn _write_closed für eine Zone fehlschlägt, muss ein
        valve_hw_close_all_zone_error-Event mit zone und pin geloggt werden.
        """
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        lgpio.gpio_write.reset_mock()

        def _write_with_failure(handle, pin, val):
            if pin == 22:
                raise RuntimeError("GPIO Schreibfehler")

        lgpio.gpio_write.side_effect = _write_with_failure

        with patch("services.valve_driver.log_event") as mock_log:
            driver.close_all()

        error_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "valve_hw_close_all_zone_error"
        ]
        assert len(error_events) == 1
        kw = error_events[0].kwargs
        assert kw["zone"] == 2
        assert kw["pin"] == 22
        assert "error" in kw
        assert kw["level"] == "error"

    def test_close_all_summary_log_contains_failed_count(self):
        """
        Das abschließende valve_hw_close_all-Event muss failed_count und
        failed_zones enthalten, damit Logs auswertbar sind.
        """
        driver, lgpio = _make_rpi_driver({1: 17, 2: 22}, active_low=True)
        lgpio.gpio_write.reset_mock()

        def _write_with_failure(handle, pin, val):
            if pin == 22:
                raise RuntimeError("GPIO Schreibfehler")

        lgpio.gpio_write.side_effect = _write_with_failure

        with patch("services.valve_driver.log_event") as mock_log:
            driver.close_all()

        summary_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "valve_hw_close_all"
        ]
        assert len(summary_events) == 1
        kw = summary_events[0].kwargs
        assert kw["failed_count"] == 1
        assert len(kw["failed_zones"]) == 1
        assert kw["failed_zones"][0]["zone"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – Initialisierung
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriverInit:
    def test_smbus2_not_available_raises_valve_driver_error(self):
        """Wenn smbus2 nicht importiert werden kann, muss ValveDriverError geworfen werden."""
        sys.modules.pop("smbus2", None)
        with patch.dict("sys.modules", {"smbus2": None}):
            with pytest.raises(ValveDriverError, match="smbus2"):
                I2cRelayValveDriver("16relay", 1, 0x20, 4)

    def test_smbus_open_failure_raises_valve_driver_error(self):
        """Wenn SMBus() fehlschlägt, muss ValveDriverError geworfen werden."""
        smbus2_mock = MagicMock()
        smbus2_mock.SMBus.side_effect = OSError("I2C-Bus nicht gefunden")
        sys.modules["smbus2"] = smbus2_mock
        try:
            with pytest.raises(ValveDriverError, match="I2C-Bus"):
                I2cRelayValveDriver("16relay", 1, 0x20, 4)
        finally:
            sys.modules.pop("smbus2", None)

    def test_driver_name_is_i2c(self):
        driver, _ = _make_i2c_driver()
        assert driver.name == "i2c"

    def test_16relay_init_writes_output_latch_before_config(self):
        """
        Sicherheitskritisch: Output-Latch muss VOR dem Config-Register auf 0x00
        geschrieben werden. Falsche Reihenfolge würde alle Relais kurz einschalten.
        Beide Register in der richtigen Sequenz prüfen.
        """
        driver, bus_mock = _make_i2c_driver(hat_type="16relay")
        write_calls = bus_mock.write_byte_data.call_args_list

        # Reihenfolge der Register-Schreibvorgänge extrahieren
        reg_sequence = [c.args[1] for c in write_calls]

        # Output-Latch LO (0x02) muss vor Config LO (0x06) kommen
        assert reg_sequence.index(_REL16_OUTPORT_REG_LO) < reg_sequence.index(_REL16_CFG_REG_LO), (
            "Output-Latch LO muss vor Config LO geschrieben werden!"
        )
        # Output-Latch HI (0x03) muss vor Config HI (0x07) kommen
        assert reg_sequence.index(_REL16_OUTPORT_REG_HI) < reg_sequence.index(_REL16_CFG_REG_HI), (
            "Output-Latch HI muss vor Config HI geschrieben werden!"
        )

    def test_16relay_init_writes_all_registers_to_zero(self):
        """Alle 4 Register des 16-Relay HAT müssen beim Init auf 0x00 gesetzt werden."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay")
        write_calls = bus_mock.write_byte_data.call_args_list

        written_regs = {c.args[1]: c.args[2] for c in write_calls}
        assert written_regs.get(_REL16_OUTPORT_REG_LO) == 0x00
        assert written_regs.get(_REL16_OUTPORT_REG_HI) == 0x00
        assert written_regs.get(_REL16_CFG_REG_LO) == 0x00
        assert written_regs.get(_REL16_CFG_REG_HI) == 0x00

    def test_8relay_init_writes_output_latch_before_config(self):
        """
        8-Relay HAT: Output-Latch muss vor Config-Register auf 0x00 gesetzt werden.
        """
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x38)
        write_calls = bus_mock.write_byte_data.call_args_list
        reg_sequence = [c.args[1] for c in write_calls]

        assert reg_sequence.index(_REL8_OUTPORT_REG) < reg_sequence.index(_REL8_CFG_REG), (
            "Output-Latch (0x01) muss vor Config-Register (0x03) geschrieben werden!"
        )

    def test_8relay_init_writes_both_registers_to_zero(self):
        """Beide Register des 8-Relay HAT müssen beim Init auf 0x00 gesetzt werden."""
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x38)
        write_calls = bus_mock.write_byte_data.call_args_list
        written_regs = {c.args[1]: c.args[2] for c in write_calls}

        assert written_regs.get(_REL8_OUTPORT_REG) == 0x00
        assert written_regs.get(_REL8_CFG_REG) == 0x00

    def test_init_hardware_failure_closes_bus(self):
        """Wenn _init_hardware() fehlschlägt, muss bus.close() aufgerufen werden (kein Handle-Leak)."""
        smbus2_mock = MagicMock()
        bus_mock = MagicMock()
        smbus2_mock.SMBus.return_value = bus_mock
        bus_mock.write_byte_data.side_effect = OSError("I2C write error")

        sys.modules["smbus2"] = smbus2_mock
        try:
            with pytest.raises(ValveDriverError):
                I2cRelayValveDriver("16relay", 1, 0x20, 4)
            bus_mock.close.assert_called_once()
        finally:
            sys.modules.pop("smbus2", None)

    def test_initial_state_is_zero(self):
        """Nach dem Init muss der interne Bitmask-State 0 sein (alle Relais aus)."""
        driver, _ = _make_i2c_driver()
        assert driver._state == 0


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – open()
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriverOpen:
    def test_open_zone1_sets_msb(self):
        """open(1) muss Bit 15 setzen (0x8000): HI-Byte=0x80, LO-Byte=0x00."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=4)
        bus_mock.reset_mock()

        driver.open(1)

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x00
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x80

    def test_open_zone2_sets_next_msb(self):
        """open(2) muss Bit 14 setzen (0x4000): HI-Byte=0x40, LO-Byte=0x00."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=4)
        bus_mock.reset_mock()

        driver.open(2)

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x00
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x40

    def test_open_multiple_zones_accumulates_bitmask(self):
        """Mehrere open()-Aufrufe müssen die remappten Bitmasks kumulieren.

        Zone 1 (0x8000) + Zone 3 (0x2000) = 0xA000.
        """
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=4)
        bus_mock.reset_mock()

        driver.open(1)
        driver.open(3)

        assert driver._state == 0xA000, (
            f"Zone 1+3 erwartet 0xA000, erhalten 0x{driver._state:04X}"
        )

    def test_open_zone9_sets_lo_byte(self):
        """
        Zone 9 → Bit 7 (0x0080) liegt im Low-Byte → LO-Byte=0x80, HI-Byte=0x00.
        """
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=16)
        bus_mock.reset_mock()

        driver.open(9)

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}

        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x80, "Low-Byte muss 0x80 sein"
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x00, "High-Byte muss 0x00 bleiben"

    def test_open_8relay_writes_single_register(self):
        """8-Relay HAT: open() darf nur REG_RELAY (0x01) schreiben.
        Zone 1 → _REL8_RELAY_MASK[0] = 0x01 (zufällig identisch mit linearem Bit 0,
        aber Korrektheit kommt aus der Remap-Tabelle, nicht aus 1<<0)."""
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x38, num_zones=4)
        bus_mock.reset_mock()

        driver.open(1)

        calls = bus_mock.write_byte_data.call_args_list
        written_regs = [c.args[1] for c in calls]
        assert written_regs == [_REL8_OUTPORT_REG]
        assert calls[0].args[2] == _REL8_RELAY_MASK[0]  # Zone 1 → 0x01

    def test_open_zone_out_of_range_raises(self):
        """open() für eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, _ = _make_i2c_driver(num_zones=4)
        with pytest.raises(ValveDriverError, match="Zone 5"):
            driver.open(5)

    def test_open_zone0_raises(self):
        """Zone 0 ist ungültig (Zonen starten bei 1)."""
        driver, _ = _make_i2c_driver(num_zones=4)
        with pytest.raises(ValveDriverError):
            driver.open(0)

    def test_open_does_not_modify_state_on_error(self):
        """Bei ungültiger Zone darf der interne State nicht verändert werden."""
        driver, _ = _make_i2c_driver(num_zones=4)
        initial_state = driver._state
        with pytest.raises(ValveDriverError):
            driver.open(99)
        assert driver._state == initial_state


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – close()
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriverClose:
    def test_close_clears_correct_bit(self):
        """close(1) muss den Remap-Bit von Zone 1 (0x8000) löschen ohne andere Bits zu verändern."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=4)
        driver.open(1)
        driver.open(2)
        bus_mock.reset_mock()

        driver.close(1)

        # Zone 2 (0x4000) darf noch gesetzt sein, Zone 1 (0x8000) nicht mehr
        assert driver._state == 0x4000, (
            f"Nach close(1) erwartet 0x4000, erhalten 0x{driver._state:04X}"
        )
        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x40
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x00

    def test_close_zone_not_open_is_idempotent(self):
        """close() einer bereits geschlossenen Zone darf nicht fehlschlagen."""
        driver, bus_mock = _make_i2c_driver(num_zones=4)
        # Zone 1 ist noch nicht geöffnet
        driver.close(1)  # darf nicht werfen
        assert driver._state == 0

    def test_close_out_of_range_raises(self):
        """close() für eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, _ = _make_i2c_driver(num_zones=4)
        with pytest.raises(ValveDriverError):
            driver.close(5)

    def test_open_close_results_in_zero_state(self):
        """open() gefolgt von close() muss denselben Zustand wie initial erzeugen."""
        driver, _ = _make_i2c_driver(num_zones=4)
        driver.open(1)
        driver.open(2)
        driver.close(1)
        driver.close(2)
        assert driver._state == 0


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – 8relay Hardware-Remap
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriver8RelayRemap:
    """
    Stellt sicher dass _zone_bitmask() für den 8-Relay HAT korrekte Hardware-Bits
    liefert. Die PCA9554-Pins sind auf der Platine nicht linear verdrahtet –
    ohne Remap werden falsche physikalische Relais aktiviert.

    Erwartete Remap-Tabelle (_REL8_RELAY_MASK):
      Zone 1 → 0x01, Zone 2 → 0x04, Zone 3 → 0x10, Zone 4 → 0x40
      Zone 5 → 0x80, Zone 6 → 0x20, Zone 7 → 0x08, Zone 8 → 0x02
    """

    @pytest.mark.parametrize("zone,expected_mask", [
        (1, 0x01),
        (2, 0x04),
        (3, 0x40),
        (4, 0x10),
        (5, 0x20),
        (6, 0x80),
        (7, 0x08),
        (8, 0x02),
    ])
    def test_zone_bitmask_all_8_zones(self, zone: int, expected_mask: int):
        """_zone_bitmask() muss für jede Zone den korrekten Hardware-Bit liefern."""
        driver, _ = _make_i2c_driver(hat_type="8relay", i2c_address=0x27, num_zones=8)
        assert driver._zone_bitmask(zone) == expected_mask, (
            f"Zone {zone}: erwartet 0x{expected_mask:02X}, "
            f"erhalten 0x{driver._zone_bitmask(zone):02X}"
        )

    @pytest.mark.parametrize("zone,expected_mask", [
        (1, 0x01),
        (2, 0x04),
        (3, 0x40),
        (4, 0x10),
        (5, 0x20),
        (6, 0x80),
        (7, 0x08),
        (8, 0x02),
    ])
    def test_open_all_zones_writes_correct_bitmask(self, zone: int, expected_mask: int):
        """open(zone) muss exakt _REL8_RELAY_MASK[zone-1] in das Ausgangsregister schreiben."""
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x27, num_zones=8)
        bus_mock.reset_mock()

        driver.open(zone)

        calls = bus_mock.write_byte_data.call_args_list
        assert len(calls) == 1
        assert calls[0].args[1] == _REL8_OUTPORT_REG
        assert calls[0].args[2] == expected_mask, (
            f"Zone {zone}: erwartet 0x{expected_mask:02X}, "
            f"geschrieben 0x{calls[0].args[2]:02X}"
        )

    def test_open_multiple_zones_accumulates_remap_bitmask(self):
        """Mehrere open()-Aufrufe müssen die remappten Bitmasks korrekt akkumulieren.

        Zone 1 (0x01) + Zone 3 (0x40) = 0x41 (nicht 0x05 wie bei linearem Mapping).
        """
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x27, num_zones=8)
        bus_mock.reset_mock()

        driver.open(1)  # 0x01
        driver.open(3)  # 0x40

        assert driver._state == 0x41, (
            f"Zone 1+3 erwartet 0x41, erhalten 0x{driver._state:02X}"
        )

    def test_close_removes_correct_remap_bit(self):
        """close() muss den korrekten Remap-Bit löschen ohne andere Bits zu verändern.

        Zone 1 (0x01) + Zone 2 (0x04) → close(1) → nur 0x04 darf bleiben.
        """
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x27, num_zones=8)
        driver.open(1)  # state = 0x01
        driver.open(2)  # state = 0x01 | 0x04 = 0x05
        bus_mock.reset_mock()

        driver.close(1)

        assert driver._state == 0x04, (
            f"Nach close(1) erwartet 0x04, erhalten 0x{driver._state:02X}"
        )
        calls = bus_mock.write_byte_data.call_args_list
        assert calls[0].args[2] == 0x04

    def test_open_close_all_zones_round_trip(self):
        """open() aller 8 Zonen gefolgt von close() aller Zonen muss State 0 ergeben."""
        driver, _ = _make_i2c_driver(hat_type="8relay", i2c_address=0x27, num_zones=8)
        for z in range(1, 9):
            driver.open(z)
        assert driver._state != 0, "State nach open() aller Zonen darf nicht 0 sein"
        for z in range(1, 9):
            driver.close(z)
        assert driver._state == 0, (
            f"Nach open+close aller Zonen erwartet 0, erhalten 0x{driver._state:02X}"
        )

    def test_16relay_zone_bitmask_uses_remap(self):
        """16-Relay HAT: _zone_bitmask() muss _REL16_RELAY_MASK verwenden (gespiegelt).
        Zone 1 → Bit 15 (0x8000), Zone 16 → Bit 0 (0x0001).
        """
        driver, _ = _make_i2c_driver(hat_type="16relay", i2c_address=0x20, num_zones=16)
        for z in range(1, 17):
            assert driver._zone_bitmask(z) == _REL16_RELAY_MASK[z - 1], (
                f"16relay Zone {z}: Remap-Wert erwartet"
            )


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – 16relay Hardware-Remap
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriver16RelayRemap:
    """
    Stellt sicher dass _zone_bitmask() für den 16-Relay HAT die gespiegelte
    Bit-Reihenfolge korrekt abbildet.

    Beobachtet auf dem Pi: Zone 1 aktivierte Relay 16, Zone 2 Relay 15 usw.
    → Bit 0 (LSB) entspricht physikalisch Relay 16, Bit 15 Relay 1.
    → Zone N muss Bit (16 - N) setzen.
    """

    @pytest.mark.parametrize("zone,expected_mask", [
        (1,  0x8000),
        (2,  0x4000),
        (3,  0x2000),
        (4,  0x1000),
        (5,  0x0800),
        (6,  0x0400),
        (7,  0x0200),
        (8,  0x0100),
        (9,  0x0080),
        (10, 0x0040),
        (11, 0x0020),
        (12, 0x0010),
        (13, 0x0008),
        (14, 0x0004),
        (15, 0x0002),
        (16, 0x0001),
    ])
    def test_zone_bitmask_all_16_zones(self, zone: int, expected_mask: int):
        """_zone_bitmask() muss für alle 16 Zonen den korrekten gespiegelten Bit liefern."""
        driver, _ = _make_i2c_driver(hat_type="16relay", i2c_address=0x20, num_zones=16)
        assert driver._zone_bitmask(zone) == expected_mask, (
            f"Zone {zone}: erwartet 0x{expected_mask:04X}, "
            f"erhalten 0x{driver._zone_bitmask(zone):04X}"
        )

    def test_open_zone1_sets_msb(self):
        """open(1) muss 0x8000 setzen: HI-Byte = 0x80, LO-Byte = 0x00."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=16)
        bus_mock.reset_mock()

        driver.open(1)

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x00, "LO-Byte muss 0x00 sein"
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x80, "HI-Byte muss 0x80 sein"

    def test_open_zone16_sets_lsb(self):
        """open(16) muss 0x0001 setzen: LO-Byte = 0x01, HI-Byte = 0x00."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=16)
        bus_mock.reset_mock()

        driver.open(16)

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x01, "LO-Byte muss 0x01 sein"
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x00, "HI-Byte muss 0x00 sein"

    def test_open_close_all_zones_round_trip(self):
        """open() aller 16 Zonen gefolgt von close() aller Zonen muss State 0 ergeben."""
        driver, _ = _make_i2c_driver(hat_type="16relay", i2c_address=0x20, num_zones=16)
        for z in range(1, 17):
            driver.open(z)
        assert driver._state != 0
        for z in range(1, 17):
            driver.close(z)
        assert driver._state == 0, (
            f"Nach open+close aller 16 Zonen erwartet 0, erhalten 0x{driver._state:04X}"
        )

    def test_relay_mask_constant_covers_all_16_zones(self):
        """_REL16_RELAY_MASK muss genau 16 Einträge haben."""
        assert len(_REL16_RELAY_MASK) == 16

    def test_relay_mask_all_bits_unique(self):
        """Jeder Eintrag in _REL16_RELAY_MASK muss einzigartig sein."""
        assert len(set(_REL16_RELAY_MASK)) == 16

    def test_relay_mask_all_bits_are_single_bit_values(self):
        """Jeder Eintrag in _REL16_RELAY_MASK muss eine Zweierpotenz sein."""
        for i, mask in enumerate(_REL16_RELAY_MASK):
            assert mask > 0 and (mask & (mask - 1)) == 0, (
                f"_REL16_RELAY_MASK[{i}] = 0x{mask:04X} ist keine Zweierpotenz!"
            )

    def test_relay_mask_covers_all_16_bits(self):
        """Die Vereinigung aller _REL16_RELAY_MASK-Bits muss 0xFFFF ergeben."""
        union = 0
        for mask in _REL16_RELAY_MASK:
            union |= mask
        assert union == 0xFFFF, (
            f"Nicht alle 16 Bits abgedeckt: Union = 0x{union:04X}, erwartet 0xFFFF"
        )

    def test_relay_mask_constant_covers_all_8_zones(self):
        """_REL8_RELAY_MASK muss genau 8 Einträge haben – einen pro Relay."""
        assert len(_REL8_RELAY_MASK) == 8

    def test_relay_mask_all_bits_unique(self):
        """Jeder Eintrag in _REL8_RELAY_MASK muss ein anderes Bit sein (kein Duplikat)."""
        assert len(set(_REL8_RELAY_MASK)) == 8, "Doppelter Eintrag in _REL8_RELAY_MASK!"

    def test_relay_mask_all_bits_are_single_bit_values(self):
        """Jeder Eintrag in _REL8_RELAY_MASK muss eine Zweierpotenz sein (ein Bit)."""
        for i, mask in enumerate(_REL8_RELAY_MASK):
            assert mask > 0 and (mask & (mask - 1)) == 0, (
                f"_REL8_RELAY_MASK[{i}] = 0x{mask:02X} ist keine Zweierpotenz!"
            )

    def test_relay_mask_covers_all_8_bits(self):
        """Die Vereinigung aller _REL8_RELAY_MASK-Bits muss 0xFF ergeben."""
        union = 0
        for mask in _REL8_RELAY_MASK:
            union |= mask
        assert union == 0xFF, (
            f"Nicht alle 8 Bits abgedeckt: Union = 0x{union:02X}, erwartet 0xFF"
        )





class TestI2cRelayValveDriverCloseAll:
    def test_close_all_resets_internal_state(self):
        """close_all() muss den internen _state auf 0 setzen."""
        driver, bus_mock = _make_i2c_driver(num_zones=4)
        driver.open(1)
        driver.open(3)
        assert driver._state != 0

        bus_mock.reset_mock()
        driver.close_all()

        assert driver._state == 0

    def test_close_all_16relay_writes_both_registers_to_zero(self):
        """close_all() beim 16-Relay HAT muss beide Ausgangsregister auf 0x00 schreiben."""
        driver, bus_mock = _make_i2c_driver(hat_type="16relay", num_zones=8)
        driver.open(1)
        bus_mock.reset_mock()

        driver.close_all()

        calls = bus_mock.write_byte_data.call_args_list
        reg_to_val = {c.args[1]: c.args[2] for c in calls}
        assert reg_to_val.get(_REL16_OUTPORT_REG_LO) == 0x00
        assert reg_to_val.get(_REL16_OUTPORT_REG_HI) == 0x00

    def test_close_all_8relay_writes_single_register_to_zero(self):
        """close_all() beim 8-Relay HAT muss nur REG_RELAY auf 0x00 schreiben."""
        driver, bus_mock = _make_i2c_driver(hat_type="8relay", i2c_address=0x38, num_zones=4)
        driver.open(1)
        bus_mock.reset_mock()

        driver.close_all()

        calls = bus_mock.write_byte_data.call_args_list
        written_regs = [c.args[1] for c in calls]
        assert written_regs == [_REL8_OUTPORT_REG]
        assert calls[0].args[2] == 0x00

    def test_close_all_best_effort_does_not_raise_on_hw_error(self):
        """
        Sicherheitskritisch: close_all() darf selbst bei I2C-Fehler nicht werfen.
        Der interne State muss trotzdem auf 0 gesetzt werden.
        """
        driver, bus_mock = _make_i2c_driver(num_zones=4)
        driver.open(1)
        bus_mock.write_byte_data.side_effect = OSError("I2C-Busfehler")

        driver.close_all()  # darf nicht werfen

        assert driver._state == 0

    def test_close_all_logs_register_errors(self):
        """Bei I2C-Fehler in close_all() muss valve_hw_close_all_reg_error geloggt werden."""
        driver, bus_mock = _make_i2c_driver(num_zones=4)
        driver.open(1)
        bus_mock.write_byte_data.side_effect = OSError("I2C-Busfehler")

        with patch("services.valve_driver.log_event") as mock_log:
            driver.close_all()

        error_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "valve_hw_close_all_reg_error"
        ]
        assert len(error_events) >= 1
        kw = error_events[0].kwargs
        assert kw["level"] == "error"
        assert "error" in kw

    def test_close_all_summary_event_logged(self):
        """valve_hw_close_all-Event muss nach close_all() geloggt werden."""
        driver, bus_mock = _make_i2c_driver(num_zones=4)
        bus_mock.reset_mock()

        with patch("services.valve_driver.log_event") as mock_log:
            driver.close_all()

        summary_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "valve_hw_close_all"
        ]
        assert len(summary_events) == 1
        assert summary_events[0].kwargs.get("failed_count") == 0


# ─────────────────────────────────────────────────────────────────────────────
# I2cRelayValveDriver – cleanup()
# ─────────────────────────────────────────────────────────────────────────────


class TestI2cRelayValveDriverCleanup:
    def test_cleanup_calls_bus_close(self):
        """cleanup() muss bus.close() aufrufen."""
        driver, bus_mock = _make_i2c_driver()
        driver.cleanup()
        bus_mock.close.assert_called_once()

    def test_cleanup_sets_bus_to_none(self):
        """Nach cleanup() muss driver._bus None sein (kein dangling Handle)."""
        driver, _ = _make_i2c_driver()
        driver.cleanup()
        assert driver._bus is None

    def test_cleanup_does_not_raise_on_bus_error(self):
        """Wenn bus.close() wirft, darf cleanup() nicht werfen."""
        driver, bus_mock = _make_i2c_driver()
        bus_mock.close.side_effect = OSError("Bus bereits geschlossen")
        driver.cleanup()  # kein raise erwartet

    def test_cleanup_logs_error_on_bus_failure(self):
        """Bei bus.close()-Fehler muss valve_driver_i2c_cleanup_error geloggt werden."""
        driver, bus_mock = _make_i2c_driver()
        bus_mock.close.side_effect = OSError("Bus bereits geschlossen")

        with patch("services.valve_driver.log_event") as mock_log:
            driver.cleanup()

        error_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "valve_driver_i2c_cleanup_error"
        ]
        assert len(error_events) == 1
        assert error_events[0].kwargs.get("level") == "error"

    def test_cleanup_idempotent_after_none_bus(self):
        """cleanup() auf einem bereits bereinigten Driver (bus=None) darf nicht werfen."""
        driver, _ = _make_i2c_driver()
        driver._bus = None
        driver.cleanup()  # kein raise erwartet


# ─────────────────────────────────────────────────────────────────────────────
# get_valve_driver – Singleton & Modi
# ─────────────────────────────────────────────────────────────────────────────


class TestGetValveDriver:
    def test_sim_mode_returns_sim_driver(self):
        reset_valve_driver()
        with state_lock:
            state.valve_driver_mode = "sim"

        drv = get_valve_driver()
        assert drv.name == "sim"
        reset_valve_driver()

    def test_unknown_mode_falls_back_to_sim(self):
        reset_valve_driver()
        with state_lock:
            state.valve_driver_mode = "unknown_xyz"

        drv = get_valve_driver()
        assert drv.name == "sim"
        reset_valve_driver()

    def test_singleton_returns_same_instance(self):
        reset_valve_driver()
        with state_lock:
            state.valve_driver_mode = "sim"

        drv1 = get_valve_driver()
        drv2 = get_valve_driver()
        assert drv1 is drv2
        reset_valve_driver()

    def test_set_valve_driver_overrides_singleton(self):
        custom = SimValveDriver()
        set_valve_driver(custom)
        assert get_valve_driver() is custom

    def test_i2c_mode_with_valid_config_returns_i2c_driver(self):
        """
        Wenn valve_driver_mode='i2c' und smbus2 verfügbar ist,
        muss ein I2cRelayValveDriver zurückgegeben werden.
        """
        reset_valve_driver()

        smbus2_mock = MagicMock()
        bus_mock = MagicMock()
        smbus2_mock.SMBus.return_value = bus_mock

        with state_lock:
            state.valve_driver_mode = "i2c"
            state.relay_hat_type = "16relay"
            state.i2c_bus = 1
            state.i2c_address = 0x20
            state.max_valves = 4

        sys.modules["smbus2"] = smbus2_mock
        try:
            drv = get_valve_driver()
            assert drv.name == "i2c"
            assert isinstance(drv, I2cRelayValveDriver)
        finally:
            sys.modules.pop("smbus2", None)
            reset_valve_driver()

    def test_i2c_mode_without_smbus2_falls_back_to_sim(self):
        """
        Wenn valve_driver_mode='i2c' aber smbus2 nicht verfügbar ist,
        muss sicher auf SimValveDriver zurückgefallen werden.
        """
        reset_valve_driver()

        with state_lock:
            state.valve_driver_mode = "i2c"
            state.relay_hat_type = "16relay"
            state.i2c_bus = 1
            state.i2c_address = 0x20
            state.max_valves = 4

        sys.modules.pop("smbus2", None)
        with patch.dict("sys.modules", {"smbus2": None}):
            drv = get_valve_driver()
            assert drv.name == "sim"

        reset_valve_driver()

    def test_i2c_mode_with_invalid_config_falls_back_to_sim(self):
        """
        Wenn valve_driver_mode='i2c' aber die Konfiguration ungültig ist
        (z.B. max_valves > 8 für 8relay), muss auf sim zurückgefallen werden.
        """
        reset_valve_driver()

        with state_lock:
            state.valve_driver_mode = "i2c"
            state.relay_hat_type = "8relay"
            state.i2c_bus = 1
            state.i2c_address = 0x38
            state.max_valves = 16  # zu viele für 8relay!

        drv = get_valve_driver()
        assert drv.name == "sim"
        reset_valve_driver()


# ─────────────────────────────────────────────────────────────────────────────
# cleanup() – Sim und Rpi
# ─────────────────────────────────────────────────────────────────────────────


class TestSimValveDriverCleanup:
    def test_cleanup_does_not_raise(self):
        """SimValveDriver.cleanup() ist ein No-Op und darf nicht werfen."""
        drv = SimValveDriver()
        drv.cleanup()


class TestRpiGpioValveDriverCleanup:
    def test_cleanup_calls_gpiochip_close(self):
        """cleanup() muss gpiochip_close() mit dem korrekten Handle aufrufen."""
        driver, lgpio = _make_rpi_driver({1: 17})
        driver.cleanup()
        lgpio.gpiochip_close.assert_called_once_with(_TEST_HANDLE)

    def test_cleanup_does_not_raise_on_lgpio_error(self):
        """Wenn gpiochip_close() eine Exception wirft, darf cleanup() nicht werfen."""
        driver, lgpio = _make_rpi_driver({1: 17})
        lgpio.gpiochip_close.side_effect = RuntimeError("Chip bereits geschlossen")
        driver.cleanup()  # kein raise erwartet

    def test_cleanup_logs_error_on_lgpio_failure(self, caplog):
        """Bei lgpio-Fehler muss ein valve_driver_gpio_cleanup_error-Event geloggt werden."""
        import logging
        driver, lgpio = _make_rpi_driver({1: 17})
        lgpio.gpiochip_close.side_effect = RuntimeError("Chip bereits geschlossen")
        with caplog.at_level(logging.ERROR):
            driver.cleanup()
        assert any("valve_driver_gpio_cleanup_error" in r.message for r in caplog.records)
