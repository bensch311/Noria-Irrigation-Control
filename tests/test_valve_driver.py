"""
Tests für services/valve_driver.py

Getestet werden:
  - validate_gpio_pins       (valide Konfiguration, Fehler, Duplikate)
  - SimValveDriver           (open/close/close_all – kein Fehler erwartet)
  - RpiGpioValveDriver       (Logik: active_low, Pin-Mapping, Fehlerbehandlung)
  - get_valve_driver         (sim-Modus, unbekannter Modus → Fallback)
"""

import sys
import pytest
from unittest.mock import MagicMock, call, patch

from services.valve_driver import (
    validate_gpio_pins,
    SimValveDriver,
    RpiGpioValveDriver,
    get_valve_driver,
    reset_valve_driver,
    set_valve_driver,
    ValveDriverError,
)
from core.state import state, state_lock


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: Erstellt einen gemockten RpiGpioValveDriver
# ─────────────────────────────────────────────────────────────────────────────

def _make_rpi_driver(
    pins_by_zone: dict[int, int],
    active_low: bool = True,
) -> tuple[RpiGpioValveDriver, MagicMock]:
    """
    Instanziiert RpiGpioValveDriver mit einem vollständig gemockten RPi.GPIO.

    Gibt (driver, gpio_mock) zurück, damit Tests auf gpio_mock.output etc. prüfen
    können.

    Strategie: RPi.GPIO wird in sys.modules eingehängt, bevor __init__ den
    'import RPi.GPIO as GPIO' ausführt. Nach der Instanziierung wird der Eintrag
    wieder entfernt, damit andere Tests nicht beeinflusst werden.
    """
    gpio_mock = MagicMock()
    gpio_mock.BCM = 11   # typischer Wert – muss nur konsistent sein
    gpio_mock.OUT = 0

    # WICHTIG: Python löst 'import RPi.GPIO as GPIO' intern über
    # sys.modules["RPi"].GPIO auf, NICHT direkt über sys.modules["RPi.GPIO"].
    # rpi_mock.GPIO muss deshalb explizit auf gpio_mock gesetzt werden, damit
    # driver._GPIO und unser gpio_mock dasselbe Objekt sind.
    rpi_mock = MagicMock()
    rpi_mock.GPIO = gpio_mock
    sys.modules["RPi"] = rpi_mock
    sys.modules["RPi.GPIO"] = gpio_mock

    try:
        driver = RpiGpioValveDriver(pins_by_zone=pins_by_zone, active_low=active_low)
    finally:
        sys.modules.pop("RPi.GPIO", None)
        sys.modules.pop("RPi", None)

    return driver, gpio_mock


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
    def test_gpio_setmode_bcm_called(self):
        """BCM-Modus muss beim Init gesetzt werden."""
        driver, gpio = _make_rpi_driver({1: 17})
        gpio.setmode.assert_called_once_with(gpio.BCM)

    def test_gpio_setwarnings_false(self):
        """Warnungen sollen deaktiviert werden."""
        driver, gpio = _make_rpi_driver({1: 17})
        gpio.setwarnings.assert_called_once_with(False)

    def test_all_pins_set_as_output(self):
        """Jeder konfigurierte Pin muss als OUTPUT eingerichtet werden."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 18, 3: 27})
        setup_pins = [c.args[0] for c in gpio.setup.call_args_list]
        assert sorted(setup_pins) == [17, 18, 27]
        for c in gpio.setup.call_args_list:
            assert c.args[1] == gpio.OUT

    def test_all_pins_initialized_to_closed_active_low(self):
        """
        Mit active_low=True: 'closed' = GPIO HIGH (1).
        Beim Init muss jeder Pin auf HIGH gesetzt werden.
        """
        driver, gpio = _make_rpi_driver({1: 17, 2: 18}, active_low=True)
        output_calls = gpio.output.call_args_list
        # Nur Init-Calls prüfen (zwei Pins → zwei output-Calls beim Setup)
        init_pins = {c.args[0]: c.args[1] for c in output_calls}
        assert init_pins[17] == 1  # HIGH = closed bei active_low
        assert init_pins[18] == 1

    def test_all_pins_initialized_to_closed_active_high(self):
        """
        Mit active_low=False: 'closed' = GPIO LOW (0).
        Beim Init muss jeder Pin auf LOW gesetzt werden.
        """
        driver, gpio = _make_rpi_driver({1: 17, 2: 18}, active_low=False)
        output_calls = gpio.output.call_args_list
        init_pins = {c.args[0]: c.args[1] for c in output_calls}
        assert init_pins[17] == 0
        assert init_pins[18] == 0

    def test_driver_name_is_rpi(self):
        driver, _ = _make_rpi_driver({1: 17})
        assert driver.name == "rpi"

    def test_rpi_gpio_not_available_raises_valve_driver_error(self):
        """Wenn RPi.GPIO nicht importiert werden kann, muss ValveDriverError geworfen werden."""
        # Sicherstellen, dass RPi.GPIO NICHT in sys.modules ist
        sys.modules.pop("RPi.GPIO", None)
        sys.modules.pop("RPi", None)

        with patch.dict("sys.modules", {"RPi": None, "RPi.GPIO": None}):
            with pytest.raises(ValveDriverError, match="RPi.GPIO"):
                RpiGpioValveDriver(pins_by_zone={1: 17}, active_low=True)


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – open()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverOpen:
    def test_open_active_low_sends_gpio_low(self):
        """
        active_low=True: Relais zieht an wenn Pin LOW (0).
        open() muss GPIO.output(pin, 0) senden.
        """
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)
        gpio.output.reset_mock()  # Init-Calls verwerfen

        driver.open(1)

        gpio.output.assert_called_once_with(17, 0)

    def test_open_active_high_sends_gpio_high(self):
        """
        active_low=False: Relais zieht an wenn Pin HIGH (1).
        open() muss GPIO.output(pin, 1) senden.
        """
        driver, gpio = _make_rpi_driver({1: 17}, active_low=False)
        gpio.output.reset_mock()

        driver.open(1)

        gpio.output.assert_called_once_with(17, 1)

    def test_open_uses_correct_pin_for_zone(self):
        """Zone → Pin Mapping muss korrekt aufgelöst werden."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        gpio.output.reset_mock()

        driver.open(2)

        # Nur Pin 22 darf angesprochen worden sein
        called_pins = [c.args[0] for c in gpio.output.call_args_list]
        assert called_pins == [22]

    def test_open_unknown_zone_raises_valve_driver_error(self):
        """Eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)

        with pytest.raises(ValveDriverError, match="zone=99"):
            driver.open(99)

    def test_open_unknown_zone_does_not_touch_gpio(self):
        """Bei unbekannter Zone darf kein GPIO-Output erfolgen."""
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)
        gpio.output.reset_mock()

        with pytest.raises(ValveDriverError):
            driver.open(99)

        gpio.output.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – close()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverClose:
    def test_close_active_low_sends_gpio_high(self):
        """
        active_low=True: Relais fällt ab wenn Pin HIGH (1).
        close() muss GPIO.output(pin, 1) senden.
        """
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)
        gpio.output.reset_mock()

        driver.close(1)

        gpio.output.assert_called_once_with(17, 1)

    def test_close_active_high_sends_gpio_low(self):
        """
        active_low=False: Relais fällt ab wenn Pin LOW (0).
        close() muss GPIO.output(pin, 0) senden.
        """
        driver, gpio = _make_rpi_driver({1: 17}, active_low=False)
        gpio.output.reset_mock()

        driver.close(1)

        gpio.output.assert_called_once_with(17, 0)

    def test_close_uses_correct_pin_for_zone(self):
        """Zone → Pin Mapping muss korrekt aufgelöst werden."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        gpio.output.reset_mock()

        driver.close(3)

        called_pins = [c.args[0] for c in gpio.output.call_args_list]
        assert called_pins == [27]

    def test_close_unknown_zone_raises_valve_driver_error(self):
        """Eine nicht konfigurierte Zone muss ValveDriverError werfen."""
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)

        with pytest.raises(ValveDriverError, match="zone=5"):
            driver.close(5)

    def test_close_unknown_zone_does_not_touch_gpio(self):
        """Bei unbekannter Zone darf kein GPIO-Output erfolgen."""
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)
        gpio.output.reset_mock()

        with pytest.raises(ValveDriverError):
            driver.close(5)

        gpio.output.assert_not_called()

    def test_open_then_close_inverts_signal_active_low(self):
        """
        Kritischer Integrationstest: open() und close() müssen entgegengesetzte
        Signale senden. Verwechslung würde Ventile dauerhaft offen lassen.
        """
        driver, gpio = _make_rpi_driver({1: 17}, active_low=True)
        gpio.output.reset_mock()

        driver.open(1)
        open_val = gpio.output.call_args_list[0].args[1]

        gpio.output.reset_mock()
        driver.close(1)
        close_val = gpio.output.call_args_list[0].args[1]

        assert open_val != close_val, (
            f"open() und close() senden dasselbe Signal ({open_val})! "
            "Das würde das Ventil dauerhaft offen lassen."
        )

    def test_open_then_close_inverts_signal_active_high(self):
        """Dasselbe für active_low=False."""
        driver, gpio = _make_rpi_driver({1: 17}, active_low=False)
        gpio.output.reset_mock()

        driver.open(1)
        open_val = gpio.output.call_args_list[0].args[1]

        gpio.output.reset_mock()
        driver.close(1)
        close_val = gpio.output.call_args_list[0].args[1]

        assert open_val != close_val


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioValveDriver – close_all()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioValveDriverCloseAll:
    def test_close_all_touches_all_pins(self):
        """close_all() muss jeden konfigurierten Pin ansprechen."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        gpio.output.reset_mock()

        driver.close_all()

        called_pins = sorted([c.args[0] for c in gpio.output.call_args_list])
        assert called_pins == [17, 22, 27]

    def test_close_all_sends_closed_signal_active_low(self):
        """close_all() muss mit active_low=True alle Pins auf HIGH setzen."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 22}, active_low=True)
        gpio.output.reset_mock()

        driver.close_all()

        for c in gpio.output.call_args_list:
            assert c.args[1] == 1, f"Pin {c.args[0]} wurde nicht auf HIGH gesetzt"

    def test_close_all_sends_closed_signal_active_high(self):
        """close_all() muss mit active_low=False alle Pins auf LOW setzen."""
        driver, gpio = _make_rpi_driver({1: 17, 2: 22}, active_low=False)
        gpio.output.reset_mock()

        driver.close_all()

        for c in gpio.output.call_args_list:
            assert c.args[1] == 0, f"Pin {c.args[0]} wurde nicht auf LOW gesetzt"

    def test_close_all_best_effort_continues_after_partial_failure(self):
        """
        close_all() ist best-effort: Schlägt ein Pin fehl, müssen die
        verbleibenden Pins trotzdem angesprochen werden.
        Dies ist sicherheitskritisch – im Fehlerfall sollen so viele
        Ventile wie möglich geschlossen werden.
        """
        driver, gpio = _make_rpi_driver({1: 17, 2: 22, 3: 27}, active_low=True)
        gpio.output.reset_mock()

        # Pin 22 wirft einen Fehler
        def _output_with_failure(pin, val):
            if pin == 22:
                raise RuntimeError("GPIO schreibfehler")

        gpio.output.side_effect = _output_with_failure

        # Darf keinen Fehler nach außen werfen
        driver.close_all()

        # Pins 17 und 27 müssen trotzdem versucht worden sein
        called_pins = sorted([c.args[0] for c in gpio.output.call_args_list])
        assert 17 in called_pins
        assert 27 in called_pins

    def test_close_all_empty_mapping_does_not_raise(self):
        """Leeres Pin-Mapping darf nicht zu einem Fehler führen."""
        driver, gpio = _make_rpi_driver({}, active_low=True)
        gpio.output.reset_mock()

        driver.close_all()  # Darf nicht werfen

        gpio.output.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# get_valve_driver
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
        # Cleanup durch autouse sim_driver Fixture erledigt
