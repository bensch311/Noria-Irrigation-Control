from services.valve_driver import validate_gpio_pins

def test_gpio_validation_detects_out_of_range():
    v = validate_gpio_pins({1: 99})
    assert v["ok"] is False
    assert len(v["invalid_pins"]) == 1

def test_gpio_validation_detects_duplicates():
    v = validate_gpio_pins({1: 17, 2: 17})
    assert v["ok"] is False
    assert len(v["duplicate_pins"]) == 1
