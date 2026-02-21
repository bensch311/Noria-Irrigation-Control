"""
Tests für services/valve_driver.py

Getestet werden:
  - validate_gpio_pins    (valide Konfiguration, Fehler, Duplikate)
  - SimValveDriver        (open/close/close_all – kein Fehler erwartet)
  - get_valve_driver      (sim-Modus, unbekannter Modus → Fallback)
"""

import pytest
from unittest.mock import patch

from services.valve_driver import (
    validate_gpio_pins,
    SimValveDriver,
    get_valve_driver,
    reset_valve_driver,
    set_valve_driver,
    ValveDriverError,
)
from core.state import state, state_lock


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
        drv.open(1)  # Darf keinen Fehler werfen

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
