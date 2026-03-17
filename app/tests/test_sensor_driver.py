"""
Tests für services/sensor_driver.py

Getestet werden:
  - validate_sensor_pins         (valide Konfiguration, Fehler, Duplikate)
  - SensorReading                (Feld-Inhalte, Typ-Korrektheit)
  - SimSensorDriver              (read, set_zone_dry, set_zone_moist, set_all_dry)
  - RpiGpioSwitchSensorDriver    (Init, read bei HIGH/LOW, Fehlerbehandlung, cleanup)
  - get_sensor_driver            (sim-Modus, unbekannter Modus → Fallback, Singleton,
                                  rpi_switch ohne Pins → Fallback, ENV-Override)

Mock-Strategie (identisch zu test_valve_driver.py):
  RpiGpioSwitchSensorDriver importiert lgpio beim __init__. Der Mock wird über
  sys.modules["lgpio"] eingehängt, bevor der Import erfolgt, und danach
  wieder entfernt, damit andere Tests nicht beeinflusst werden.

  lgpio-API für Inputs:
    lgpio.gpiochip_open(0)                     → handle (int)
    lgpio.gpio_claim_input(h, pin, lflags)     → Pin als Input konfigurieren
    lgpio.gpio_read(h, pin)                    → 0 oder 1 lesen
    lgpio.gpiochip_close(h)                    → Chip-Handle freigeben
"""

import sys
import time
import pytest
from unittest.mock import MagicMock, patch, call

from services.sensor_driver import (
    validate_sensor_pins,
    SensorReading,
    SensorDriverError,
    SimSensorDriver,
    RpiGpioSwitchSensorDriver,
    BaseSensorDriver,
    get_sensor_driver,
    reset_sensor_driver,
    set_sensor_driver,
)
from core.state import state, state_lock

# Fester Test-Handle, den gpiochip_open() zurückgeben soll
_TEST_HANDLE = 99


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: Erstellt einen gemockten RpiGpioSwitchSensorDriver
# ─────────────────────────────────────────────────────────────────────────────

def _make_rpi_sensor_driver(
    pins_by_zone: dict[int, int],
    internal_pull_up: bool = False,
) -> tuple[RpiGpioSwitchSensorDriver, MagicMock]:
    """
    Instanziiert RpiGpioSwitchSensorDriver mit einem vollständig gemockten lgpio-Modul.

    Gibt (driver, lgpio_mock) zurück, damit Tests auf
    lgpio_mock.gpio_read, lgpio_mock.gpio_claim_input etc. prüfen können.

    gpiochip_open() gibt _TEST_HANDLE zurück, damit der Handle-Wert in
    allen Read-/Cleanup-Assertions konsistent ist.
    """
    lgpio_mock = MagicMock()
    lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE

    sys.modules["lgpio"] = lgpio_mock
    try:
        driver = RpiGpioSwitchSensorDriver(
            pins_by_zone=pins_by_zone,
            internal_pull_up=internal_pull_up,
        )
    finally:
        sys.modules.pop("lgpio", None)

    # Nach der Instanziierung bleibt lgpio_mock im driver._lgpio – alle
    # späteren Aufrufe (read/cleanup) gehen durch diesen Mock.
    return driver, lgpio_mock


# ─────────────────────────────────────────────────────────────────────────────
# validate_sensor_pins
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateSensorPins:
    def test_valid_pins(self):
        pins = {1: 17, 2: 18, 3: 27, 4: 22}
        result = validate_sensor_pins(pins)
        assert result["ok"] is True
        assert result["invalid_pins"] == []
        assert result["duplicate_pins"] == []

    def test_empty_pins_is_valid(self):
        result = validate_sensor_pins({})
        assert result["ok"] is True

    def test_pin_out_of_range_low(self):
        result = validate_sensor_pins({1: 1})  # Pin 1 < 2 → ungültig
        assert result["ok"] is False
        assert len(result["invalid_pins"]) == 1
        assert result["invalid_pins"][0]["reason"] == "out_of_range_2_27"

    def test_pin_out_of_range_high(self):
        result = validate_sensor_pins({1: 28})  # Pin 28 > 27 → ungültig
        assert result["ok"] is False
        assert len(result["invalid_pins"]) == 1

    def test_duplicate_pins(self):
        pins = {1: 24, 2: 24}  # Beide Zonen nutzen Pin 24
        result = validate_sensor_pins(pins)
        assert result["ok"] is False
        assert len(result["duplicate_pins"]) == 1
        assert result["duplicate_pins"][0]["pin"] == 24
        assert sorted(result["duplicate_pins"][0]["zones"]) == [1, 2]

    def test_multiple_errors(self):
        pins = {1: 0, 2: 24, 3: 24}  # Pin 0 out of range + Duplikat
        result = validate_sensor_pins(pins)
        assert result["ok"] is False
        assert len(result["invalid_pins"]) >= 1
        assert len(result["duplicate_pins"]) >= 1

    def test_boundary_pin_2_is_valid(self):
        result = validate_sensor_pins({1: 2})
        assert result["ok"] is True

    def test_boundary_pin_27_is_valid(self):
        result = validate_sensor_pins({1: 27})
        assert result["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# SensorReading
# ─────────────────────────────────────────────────────────────────────────────


class TestSensorReading:
    def test_fields_are_stored_correctly(self):
        ts = time.monotonic()
        reading = SensorReading(
            zone=3,
            needs_irrigation=True,
            raw_gpio_value=0,
            timestamp=ts,
            driver_name="sim",
        )
        assert reading.zone == 3
        assert reading.needs_irrigation is True
        assert reading.raw_gpio_value == 0
        assert reading.timestamp == ts
        assert reading.driver_name == "sim"

    def test_moist_reading(self):
        reading = SensorReading(
            zone=1,
            needs_irrigation=False,
            raw_gpio_value=1,
            timestamp=time.monotonic(),
            driver_name="sim",
        )
        assert reading.needs_irrigation is False
        assert reading.raw_gpio_value == 1


# ─────────────────────────────────────────────────────────────────────────────
# SimSensorDriver
# ─────────────────────────────────────────────────────────────────────────────


class TestSimSensorDriver:
    def test_driver_name_is_sim(self):
        drv = SimSensorDriver()
        assert drv.name == "sim"

    def test_default_zone_is_moist(self):
        drv = SimSensorDriver()
        reading = drv.read(1)
        assert reading.needs_irrigation is False
        assert reading.raw_gpio_value == 1

    def test_set_zone_dry_makes_needs_irrigation_true(self):
        drv = SimSensorDriver()
        drv.set_zone_dry(2)
        reading = drv.read(2)
        assert reading.needs_irrigation is True
        assert reading.raw_gpio_value == 0

    def test_set_zone_moist_reverts_dry(self):
        drv = SimSensorDriver()
        drv.set_zone_dry(1)
        drv.set_zone_moist(1)
        reading = drv.read(1)
        assert reading.needs_irrigation is False

    def test_set_all_dry_marks_multiple_zones(self):
        drv = SimSensorDriver()
        drv.set_all_dry([1, 2, 3])
        for zone in [1, 2, 3]:
            assert drv.read(zone).needs_irrigation is True

    def test_set_all_dry_does_not_affect_other_zones(self):
        drv = SimSensorDriver()
        drv.set_all_dry([1, 2])
        assert drv.read(3).needs_irrigation is False

    def test_reading_contains_correct_zone(self):
        drv = SimSensorDriver()
        reading = drv.read(5)
        assert reading.zone == 5

    def test_reading_driver_name_matches(self):
        drv = SimSensorDriver()
        reading = drv.read(1)
        assert reading.driver_name == "sim"

    def test_reading_timestamp_is_recent(self):
        drv = SimSensorDriver()
        before = time.monotonic()
        reading = drv.read(1)
        after = time.monotonic()
        assert before <= reading.timestamp <= after

    def test_cleanup_does_not_raise(self):
        drv = SimSensorDriver()
        drv.cleanup()  # No-Op – darf keinen Fehler werfen

    def test_multiple_zones_independent(self):
        drv = SimSensorDriver()
        drv.set_zone_dry(1)
        assert drv.read(1).needs_irrigation is True
        assert drv.read(2).needs_irrigation is False
        assert drv.read(3).needs_irrigation is False

    def test_zone_int_coercion(self):
        """set_zone_dry/set_zone_moist akzeptieren auch numerische Strings implizit via int()."""
        drv = SimSensorDriver()
        drv.set_zone_dry(1)
        # read() muss auch int akzeptieren
        assert drv.read(1).needs_irrigation is True

    def test_read_logs_event(self):
        drv = SimSensorDriver()
        with patch("services.sensor_driver.log_event") as mock_log:
            drv.read(3)
        assert mock_log.called
        event_name = mock_log.call_args.args[0]
        assert event_name == "sensor_hw_read"


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioSwitchSensorDriver – Initialisierung
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioSwitchSensorDriverInit:
    def test_gpiochip_open_called_with_chip_0(self):
        """gpiochip_open(0) muss beim Init aufgerufen werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpiochip_open.assert_called_once_with(0)

    def test_all_pins_claimed_as_input(self):
        """gpio_claim_input() muss für jeden konfigurierten Pin aufgerufen werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24, 2: 25, 3: 26})
        claimed_pins = [c.args[1] for c in lgpio.gpio_claim_input.call_args_list]
        assert sorted(claimed_pins) == [24, 25, 26]

    def test_all_pins_use_correct_handle(self):
        """gpio_claim_input() muss mit dem von gpiochip_open() zurückgegebenen Handle aufgerufen werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24, 2: 25})
        for c in lgpio.gpio_claim_input.call_args_list:
            assert c.args[0] == _TEST_HANDLE

    def test_no_internal_pull_up_uses_lflags_zero(self):
        """
        Ohne internen Pull-Up (default) muss gpio_claim_input() mit lflags=0
        aufgerufen werden – kein interner Widerstand aktiviert.
        """
        driver, lgpio = _make_rpi_sensor_driver({1: 24}, internal_pull_up=False)
        for c in lgpio.gpio_claim_input.call_args_list:
            lflags = c.args[2]
            assert lflags == 0, f"Pin {c.args[1]}: erwartet lflags=0, bekommen: {lflags}"

    def test_internal_pull_up_uses_correct_lflags(self):
        """
        Mit internal_pull_up=True muss lflags=0x20 (SET_PULL_UP) verwendet werden.
        """
        driver, lgpio = _make_rpi_sensor_driver({1: 24}, internal_pull_up=True)
        expected_lflags = RpiGpioSwitchSensorDriver._LGPIO_SET_PULL_UP
        for c in lgpio.gpio_claim_input.call_args_list:
            lflags = c.args[2]
            assert lflags == expected_lflags, (
                f"Pin {c.args[1]}: erwartet lflags={expected_lflags:#x}, bekommen: {lflags:#x}"
            )

    def test_driver_name_is_rpi_switch(self):
        driver, _ = _make_rpi_sensor_driver({1: 24})
        assert driver.name == "rpi_switch"

    def test_lgpio_not_available_raises_sensor_driver_error(self):
        """Wenn lgpio nicht importiert werden kann, muss SensorDriverError geworfen werden."""
        sys.modules.pop("lgpio", None)
        with patch.dict("sys.modules", {"lgpio": None}):
            with pytest.raises(SensorDriverError, match="lgpio"):
                RpiGpioSwitchSensorDriver(pins_by_zone={1: 24}, internal_pull_up=False)

    def test_gpiochip_open_failure_raises_sensor_driver_error(self):
        """Wenn gpiochip_open() fehlschlägt, muss SensorDriverError geworfen werden."""
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.side_effect = RuntimeError("Chip nicht gefunden")
        sys.modules["lgpio"] = lgpio_mock
        try:
            with pytest.raises(SensorDriverError, match="GPIO-Chip"):
                RpiGpioSwitchSensorDriver(pins_by_zone={1: 24}, internal_pull_up=False)
        finally:
            sys.modules.pop("lgpio", None)

    def test_gpio_claim_input_failure_closes_chip_handle(self):
        """
        Wenn gpio_claim_input() für einen Pin fehlschlägt, muss gpiochip_close()
        aufgerufen werden um einen Ressourcen-Leak zu vermeiden.
        """
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE
        lgpio_mock.gpio_claim_input.side_effect = RuntimeError("Pin belegt")
        sys.modules["lgpio"] = lgpio_mock
        try:
            with pytest.raises(SensorDriverError):
                RpiGpioSwitchSensorDriver(pins_by_zone={1: 24}, internal_pull_up=False)
        finally:
            sys.modules.pop("lgpio", None)

        lgpio_mock.gpiochip_close.assert_called_once_with(_TEST_HANDLE)

    def test_gpio_claim_input_failure_raises_sensor_driver_error(self):
        """Wenn gpio_claim_input() fehlschlägt, muss SensorDriverError mit Pin-Info geworfen werden."""
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE
        lgpio_mock.gpio_claim_input.side_effect = RuntimeError("Pin belegt")
        sys.modules["lgpio"] = lgpio_mock
        try:
            with pytest.raises(SensorDriverError, match="BCM 24"):
                RpiGpioSwitchSensorDriver(pins_by_zone={1: 24}, internal_pull_up=False)
        finally:
            sys.modules.pop("lgpio", None)

    def test_setup_log_event_emitted(self):
        """Beim Init muss ein sensor_driver_gpio_setup-Event geloggt werden."""
        lgpio_mock = MagicMock()
        lgpio_mock.gpiochip_open.return_value = _TEST_HANDLE
        sys.modules["lgpio"] = lgpio_mock
        try:
            with patch("services.sensor_driver.log_event") as mock_log:
                RpiGpioSwitchSensorDriver(pins_by_zone={1: 24, 2: 25}, internal_pull_up=False)
        finally:
            sys.modules.pop("lgpio", None)

        setup_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_driver_gpio_setup"
        ]
        assert len(setup_events) == 1
        kw = setup_events[0].kwargs
        assert sorted(kw["zones"]) == [1, 2]
        assert kw["internal_pull_up"] is False


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioSwitchSensorDriver – read()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioSwitchSensorDriverRead:
    def test_gpio_high_means_moist(self):
        """
        GPIO HIGH (1) = Kontakt offen = Boden feucht = kein Bewässerungsbedarf.
        """
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 1

        reading = driver.read(1)

        assert reading.needs_irrigation is False
        assert reading.raw_gpio_value == 1

    def test_gpio_low_means_dry(self):
        """
        GPIO LOW (0) = Kontakt geschlossen = Boden trocken = Bewässerung nötig.
        """
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 0

        reading = driver.read(1)

        assert reading.needs_irrigation is True
        assert reading.raw_gpio_value == 0

    def test_read_calls_gpio_read_with_correct_handle_and_pin(self):
        """gpio_read() muss mit dem korrekten Handle und Pin aufgerufen werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 1

        driver.read(1)

        lgpio.gpio_read.assert_called_once_with(_TEST_HANDLE, 24)

    def test_read_returns_correct_zone(self):
        driver, lgpio = _make_rpi_sensor_driver({1: 24, 2: 25})
        lgpio.gpio_read.return_value = 1

        reading = driver.read(2)

        assert reading.zone == 2

    def test_read_returns_correct_driver_name(self):
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 1

        reading = driver.read(1)

        assert reading.driver_name == "rpi_switch"

    def test_read_timestamp_is_recent(self):
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 1

        before = time.monotonic()
        reading = driver.read(1)
        after = time.monotonic()

        assert before <= reading.timestamp <= after

    def test_read_unknown_zone_raises_sensor_driver_error(self):
        """Wenn eine Zone nicht konfiguriert ist, muss SensorDriverError geworfen werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})

        with pytest.raises(SensorDriverError, match="zone=99"):
            driver.read(99)

    def test_gpio_read_hardware_error_raises_sensor_driver_error(self):
        """Wenn gpio_read() eine Exception wirft, muss SensorDriverError weitergegeben werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.side_effect = RuntimeError("Bus-Fehler")

        with pytest.raises(SensorDriverError, match="GPIO-Lesefehler"):
            driver.read(1)

    def test_read_selects_correct_pin_for_zone(self):
        """Jede Zone muss den ihr zugeordneten Pin lesen."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24, 2: 25, 3: 26})
        lgpio.gpio_read.return_value = 1

        driver.read(3)

        lgpio.gpio_read.assert_called_once_with(_TEST_HANDLE, 26)

    def test_read_logs_sensor_hw_read_event(self):
        """read() muss ein sensor_hw_read-Event loggen."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpio_read.return_value = 0  # trocken

        with patch("services.sensor_driver.log_event") as mock_log:
            driver.read(1)

        read_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_hw_read"
        ]
        assert len(read_events) == 1
        kw = read_events[0].kwargs
        assert kw["zone"] == 1
        assert kw["pin"] == 24
        assert kw["raw_gpio_value"] == 0
        assert kw["needs_irrigation"] is True

    def test_multiple_reads_from_same_driver(self):
        """Mehrere Lesevorgänge vom selben Driver müssen unabhängig funktionieren."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24, 2: 25})
        lgpio.gpio_read.return_value = 0  # trocken

        r1 = driver.read(1)
        r2 = driver.read(2)

        assert r1.needs_irrigation is True
        assert r2.needs_irrigation is True
        assert r1.zone == 1
        assert r2.zone == 2


# ─────────────────────────────────────────────────────────────────────────────
# RpiGpioSwitchSensorDriver – cleanup()
# ─────────────────────────────────────────────────────────────────────────────


class TestRpiGpioSwitchSensorDriverCleanup:
    def test_cleanup_calls_gpiochip_close_with_correct_handle(self):
        """cleanup() muss gpiochip_close() mit dem korrekten Handle aufrufen."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        driver.cleanup()
        lgpio.gpiochip_close.assert_called_once_with(_TEST_HANDLE)

    def test_cleanup_does_not_raise_on_lgpio_error(self):
        """Wenn gpiochip_close() eine Exception wirft, darf cleanup() nicht werfen."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpiochip_close.side_effect = RuntimeError("Chip bereits geschlossen")
        driver.cleanup()  # kein raise erwartet

    def test_cleanup_logs_success(self):
        """Bei erfolgreichem cleanup() muss sensor_driver_gpio_cleanup geloggt werden."""
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        with patch("services.sensor_driver.log_event") as mock_log:
            driver.cleanup()
        cleanup_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_driver_gpio_cleanup"
        ]
        assert len(cleanup_events) == 1

    def test_cleanup_logs_error_on_lgpio_failure(self, caplog):
        """Bei lgpio-Fehler muss ein sensor_driver_gpio_cleanup_error-Event geloggt werden."""
        import logging
        driver, lgpio = _make_rpi_sensor_driver({1: 24})
        lgpio.gpiochip_close.side_effect = RuntimeError("Chip bereits geschlossen")
        with caplog.at_level(logging.ERROR):
            driver.cleanup()
        assert any("sensor_driver_gpio_cleanup_error" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# get_sensor_driver
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSensorDriver:
    def test_sim_mode_returns_sim_driver(self):
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "sim"

        drv = get_sensor_driver()
        assert drv.name == "sim"
        reset_sensor_driver()

    def test_unknown_mode_falls_back_to_sim(self):
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "unbekannter_modus_xyz"

        drv = get_sensor_driver()
        assert drv.name == "sim"
        reset_sensor_driver()

    def test_singleton_returns_same_instance(self):
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "sim"

        drv1 = get_sensor_driver()
        drv2 = get_sensor_driver()
        assert drv1 is drv2
        reset_sensor_driver()

    def test_set_sensor_driver_overrides_singleton(self):
        custom = SimSensorDriver()
        set_sensor_driver(custom)
        assert get_sensor_driver() is custom
        reset_sensor_driver()

    def test_rpi_switch_without_pins_falls_back_to_sim(self):
        """rpi_switch-Modus ohne konfigurierte Pins → Fallback auf sim."""
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {}  # keine Pins konfiguriert

        drv = get_sensor_driver()
        assert drv.name == "sim"
        reset_sensor_driver()

    def test_rpi_switch_with_invalid_pins_falls_back_to_sim(self):
        """rpi_switch-Modus mit ungültigen Pins (out_of_range) → Fallback auf sim."""
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {1: 1}  # Pin 1 < 2 → ungültig

        drv = get_sensor_driver()
        assert drv.name == "sim"
        reset_sensor_driver()

    def test_env_override_sim_mode(self, monkeypatch):
        """ENV IRRIGATION_SENSOR_DRIVER=sim überschreibt State-Einstellung."""
        reset_sensor_driver()
        monkeypatch.setenv("IRRIGATION_SENSOR_DRIVER", "sim")
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {}

        drv = get_sensor_driver()
        assert drv.name == "sim"
        reset_sensor_driver()

    def test_reset_sensor_driver_clears_singleton(self):
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "sim"

        drv1 = get_sensor_driver()
        reset_sensor_driver()
        drv2 = get_sensor_driver()
        # Nach reset_sensor_driver() muss eine NEUE Instanz erstellt werden
        assert drv1 is not drv2
        reset_sensor_driver()

    def test_fallback_is_logged_on_unknown_mode(self):
        """Bei unbekanntem Modus muss sensor_driver_init_fallback geloggt werden."""
        reset_sensor_driver()
        with state_lock:
            state.sensor_driver_mode = "gibts_nicht"

        with patch("services.sensor_driver.log_event") as mock_log:
            drv = get_sensor_driver()

        fallback_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_driver_init_fallback"
        ]
        assert len(fallback_events) == 1
        assert fallback_events[0].kwargs.get("level") == "warning"
        reset_sensor_driver()
