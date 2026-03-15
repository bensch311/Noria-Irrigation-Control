"""
Tests für services/valve_driver.py

Getestet werden:
  - validate_gpio_pins       (valide Konfiguration, Fehler, Duplikate)
  - SimValveDriver           (open/close/close_all – kein Fehler erwartet)
  - RpiGpioValveDriver       (Logik: active_low, Pin-Mapping, Fehlerbehandlung)
  - get_valve_driver         (sim-Modus, unbekannter Modus → Fallback)

Hinweis zur Mock-Strategie:
  RpiGpioValveDriver importiert lgpio beim __init__. Der Mock wird über
  sys.modules["lgpio"] eingehängt, bevor der Import erfolgt, und danach
  wieder entfernt, damit andere Tests nicht beeinflusst werden.

  lgpio-API im Vergleich zu RPi.GPIO:
    lgpio.gpiochip_open(0)              → handle (int)
    lgpio.gpio_claim_output(h, pin, v)  → Pin als Output mit Initialwert
    lgpio.gpio_write(h, pin, v)         → Wert setzen (0 oder 1)
    lgpio.gpiochip_close(h)             → Chip-Handle freigeben (Cleanup)
"""

import sys
import pytest
from unittest.mock import MagicMock, patch

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


# ─────────────────────────────────────────────────────────────────────────────
# cleanup()
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
