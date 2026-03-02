"""
Tests für Step 5: Input Validation Hardening (models/requests.py)

Testet alle Pydantic-Validierungen aus models/requests.py:

StartRequest:
  - time_unit Literal: gültige Werte "Sekunden" und "Minuten"
  - time_unit Literal: ungültige Werte → 422
  - duration ge=1: Wert 0 → 422
  - zone ge=1: Wert 0 → 422

QueueAddRequest:
  - time_unit Literal: gültige Werte "Sekunden" und "Minuten"
  - time_unit Literal: ungültige Werte → 422
  - duration ge=1: Wert 0 → 422
  - zone ge=1: Wert 0 → 422

ScheduleAddRequest:
  - time_unit Literal: gültige Werte "Sekunden" und "Minuten"
  - time_unit Literal: ungültige Werte → 422
  - weekdays @field_validator: gültige Wochentage 0-6
  - weekdays @field_validator: ungültiger Wert 7 → 422
  - weekdays @field_validator: negativer Wert → 422
  - weekdays @field_validator: mehrere Werte, einer ungültig → 422
  - start_times @field_validator: gültiges Format "HH:MM"
  - start_times @field_validator: Stunde 25 → 422
  - start_times @field_validator: Minute 60 → 422
  - start_times @field_validator: fehlendes führendes Null "6:00" → 422
  - start_times @field_validator: komplett falsches Format → 422
  - start_times @field_validator: nicht-numerische Teile → 422
  - start_times @field_validator: mehrere Werte, einer ungültig → 422
  - min_length=1 weekdays: leere Liste → 422
  - min_length=1 start_times: leere Liste → 422
  - duration_s ge=1: Wert 0 → 422

Grenzwert-Tests (Boundary):
  - time_unit Default: "Minuten" (kein Wert angegeben)
  - weekday Grenzwerte: 0 und 6 sind gültig
  - start_times Grenzwerte: "00:00" und "23:59" sind gültig
  - start_times Grenzwerte: "00:00" und "24:00" → 422 (24 ungültig)
"""

import pytest

from core.state import state, state_lock


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _start_payload(**overrides) -> dict:
    """Gültiges StartRequest-Payload mit optionalen Overrides."""
    base = {"zone": 1, "duration": 30, "time_unit": "Sekunden"}
    base.update(overrides)
    return base


def _queue_payload(**overrides) -> dict:
    """Gültiges QueueAddRequest-Payload mit optionalen Overrides."""
    base = {"zone": 1, "duration": 30, "time_unit": "Sekunden"}
    base.update(overrides)
    return base


def _schedule_payload(**overrides) -> dict:
    """Gültiges ScheduleAddRequest-Payload mit optionalen Overrides."""
    base = {
        "zone": 1,
        "weekdays": [0],
        "start_times": ["06:00"],
        "duration_s": 60,
        "repeat": True,
        "time_unit": "Sekunden",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# StartRequest – time_unit
# ─────────────────────────────────────────────────────────────────────────────

class TestStartRequestTimeUnit:

    def test_time_unit_sekunden_accepted(self, client):
        resp = client.post("/start", json=_start_payload(time_unit="Sekunden"))
        assert resp.status_code == 200

    def test_time_unit_minuten_accepted(self, client, mock_io):
        # duration=1 Minute = 60s, liegt unter hard_max_runtime_s (3600)
        resp = client.post("/start", json=_start_payload(time_unit="Minuten", duration=1))
        assert resp.status_code == 200

    def test_time_unit_empty_string_rejected(self, client):
        resp = client.post("/start", json=_start_payload(time_unit=""))
        assert resp.status_code == 422

    def test_time_unit_lowercase_rejected(self, client):
        """Groß-/Kleinschreibung muss exakt passen."""
        resp = client.post("/start", json=_start_payload(time_unit="sekunden"))
        assert resp.status_code == 422

    def test_time_unit_arbitrary_string_rejected(self, client):
        resp = client.post("/start", json=_start_payload(time_unit="Stunden"))
        assert resp.status_code == 422

    def test_time_unit_injection_attempt_rejected(self, client):
        """Injection-Versuch mit Sonderzeichen wird abgelehnt."""
        resp = client.post("/start", json=_start_payload(time_unit="'; DROP TABLE--"))
        assert resp.status_code == 422

    def test_time_unit_default_is_minuten(self, client):
        """Wenn time_unit weggelassen, ist der Default 'Minuten'."""
        payload = {"zone": 1, "duration": 1}  # kein time_unit
        resp = client.post("/start", json=payload)
        # Duration=1 Minute < hard_max → 200
        assert resp.status_code == 200

    def test_duration_zero_rejected(self, client):
        """duration=0 verletzt ge=1 → 422."""
        resp = client.post("/start", json=_start_payload(duration=0))
        assert resp.status_code == 422

    def test_zone_zero_rejected(self, client):
        """zone=0 verletzt ge=1 → 422."""
        resp = client.post("/start", json=_start_payload(zone=0))
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# QueueAddRequest – time_unit
# ─────────────────────────────────────────────────────────────────────────────

class TestQueueAddRequestTimeUnit:

    def test_time_unit_sekunden_accepted(self, client):
        resp = client.post("/queue/add", json=_queue_payload(time_unit="Sekunden"))
        assert resp.status_code == 200

    def test_time_unit_minuten_accepted(self, client):
        resp = client.post("/queue/add", json=_queue_payload(time_unit="Minuten"))
        assert resp.status_code == 200

    def test_time_unit_invalid_rejected(self, client):
        resp = client.post("/queue/add", json=_queue_payload(time_unit="Stunden"))
        assert resp.status_code == 422

    def test_time_unit_null_rejected(self, client):
        """None als time_unit wird abgelehnt."""
        resp = client.post("/queue/add", json=_queue_payload(time_unit=None))
        assert resp.status_code == 422

    def test_duration_zero_rejected(self, client):
        resp = client.post("/queue/add", json=_queue_payload(duration=0))
        assert resp.status_code == 422

    def test_zone_zero_adds_all_valves(self, client):
        """zone=0 (Alle Zonen) ist gültig und fügt max_valves Items ein.

        QueueAddRequest erlaubt zone=0 seit der 'Alle Zonen'-Erweiterung.
        StartRequest (andere Klasse) lehnt zone=0 weiterhin mit 422 ab.
        """
        resp = client.post("/queue/add", json=_queue_payload(zone=0))
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["zones_added"] >= 1

    def test_zone_negative_rejected(self, client):
        """Negative zone-Werte verletzen ge=0 → 422."""
        resp = client.post("/queue/add", json=_queue_payload(zone=-1))
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# ScheduleAddRequest – time_unit
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduleRequestTimeUnit:

    def test_time_unit_sekunden_accepted(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(time_unit="Sekunden"))
        assert resp.status_code == 200

    def test_time_unit_minuten_accepted(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(time_unit="Minuten"))
        assert resp.status_code == 200

    def test_time_unit_invalid_rejected(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(time_unit="hours"))
        assert resp.status_code == 422

    def test_time_unit_number_rejected(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(time_unit=60))
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# ScheduleAddRequest – weekdays @field_validator
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduleRequestWeekdays:

    def test_all_valid_weekdays(self, client):
        """Alle Wochentage 0-6 sind gültig."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[0, 1, 2, 3, 4, 5, 6]))
        assert resp.status_code == 200

    def test_boundary_weekday_0(self, client):
        """Grenzwert 0 (Montag) ist gültig."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[0]))
        assert resp.status_code == 200

    def test_boundary_weekday_6(self, client):
        """Grenzwert 6 (Sonntag) ist gültig."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[6]))
        assert resp.status_code == 200

    def test_weekday_7_rejected(self, client):
        """7 liegt außerhalb 0-6 → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[7]))
        assert resp.status_code == 422

    def test_weekday_negative_rejected(self, client):
        """-1 ist kein gültiger Wochentag → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[-1]))
        assert resp.status_code == 422

    def test_mixed_valid_invalid_rejected(self, client):
        """Wenn auch nur ein Wochentag ungültig ist, wird die gesamte Liste abgelehnt."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[0, 3, 7]))
        assert resp.status_code == 422

    def test_empty_weekdays_rejected(self, client):
        """Leere Liste verletzt min_length=1 → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[]))
        assert resp.status_code == 422

    def test_weekdays_response_contains_error_detail(self, client):
        """422-Response enthält 'detail'-Feld mit Fehlerbeschreibung."""
        resp = client.post("/schedule/add", json=_schedule_payload(weekdays=[7]))
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data


# ─────────────────────────────────────────────────────────────────────────────
# ScheduleAddRequest – start_times @field_validator
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduleRequestStartTimes:

    def test_valid_start_time(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["06:00"]))
        assert resp.status_code == 200

    def test_multiple_valid_start_times(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["06:00", "12:00", "18:00"]))
        assert resp.status_code == 200

    def test_boundary_midnight(self, client):
        """00:00 ist eine gültige Mitternacht-Startzeit."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["00:00"]))
        assert resp.status_code == 200

    def test_boundary_last_minute_of_day(self, client):
        """23:59 ist die letzte gültige Tageszeit."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["23:59"]))
        assert resp.status_code == 200

    def test_hour_24_rejected(self, client):
        """24:00 ist ungültig (Stunden 0-23) → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["24:00"]))
        assert resp.status_code == 422

    def test_hour_25_rejected(self, client):
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["25:00"]))
        assert resp.status_code == 422

    def test_minute_60_rejected(self, client):
        """Minuten 0-59 sind gültig; 60 ist ungültig → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["06:60"]))
        assert resp.status_code == 422

    def test_missing_leading_zero_rejected(self, client):
        """'6:00' hat nur 4 Zeichen, Format muss 'HH:MM' sein → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["6:00"]))
        assert resp.status_code == 422

    def test_wrong_separator_rejected(self, client):
        """'06.00' verwendet Punkt statt Doppelpunkt → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["06.00"]))
        assert resp.status_code == 422

    def test_arbitrary_string_rejected(self, client):
        """Freitext-String ist kein gültiges Zeitformat → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["morning"]))
        assert resp.status_code == 422

    def test_non_numeric_digits_rejected(self, client):
        """'aa:bb' hat nicht-numerische Zeichen → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["aa:bb"]))
        assert resp.status_code == 422

    def test_mixed_valid_invalid_rejected(self, client):
        """Wenn auch nur eine Startzeit ungültig ist, wird die Liste abgelehnt."""
        resp = client.post("/schedule/add", json=_schedule_payload(
            start_times=["06:00", "25:00"]
        ))
        assert resp.status_code == 422

    def test_empty_start_times_rejected(self, client):
        """Leere Liste verletzt min_length=1 → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=[]))
        assert resp.status_code == 422

    def test_start_times_injection_attempt_rejected(self, client):
        """Injection-Versuch wird vom Format-Validator abgefangen → 422."""
        resp = client.post("/schedule/add", json=_schedule_payload(
            start_times=["'; DROP"]
        ))
        assert resp.status_code == 422

    def test_start_times_response_contains_error_detail(self, client):
        """422-Response enthält 'detail'-Feld mit Fehlerbeschreibung."""
        resp = client.post("/schedule/add", json=_schedule_payload(start_times=["25:00"]))
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data


# ─────────────────────────────────────────────────────────────────────────────
# Kombinierte Validierungen: Mehrere Fehler gleichzeitig
# ─────────────────────────────────────────────────────────────────────────────

class TestCombinedValidation:

    def test_multiple_errors_all_reported(self, client):
        """
        Pydantic sammelt alle Validierungsfehler und gibt sie gemeinsam zurück.
        Bei mehreren Fehlern muss die 'detail'-Liste mehrere Einträge haben.
        """
        payload = {
            "zone": 1,
            "weekdays": [7],       # ungültig
            "start_times": ["25:00"],  # ungültig
            "duration_s": 60,
            "repeat": True,
            "time_unit": "Stunden",    # ungültig
        }
        resp = client.post("/schedule/add", json=payload)
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        # Pydantic v2 gibt alle Fehler in einer Liste zurück
        assert len(data["detail"]) >= 2

    def test_valid_payload_passes_all_validators(self, client):
        """Ein vollständig gültiges Payload passiert alle Validierungen."""
        payload = {
            "zone": 1,
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "start_times": ["00:00", "06:00", "12:00", "18:00", "23:59"],
            "duration_s": 300,
            "repeat": True,
            "time_unit": "Sekunden",
        }
        resp = client.post("/schedule/add", json=payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Unit-Tests: _sanitize_pydantic_errors (api/errors.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestSanitizePydanticErrors:
    """
    Unit-Tests für api.errors._sanitize_pydantic_errors.

    Die Funktion muss Pydantic-v2-Fehlerstrukturen mit ctx["error"] als
    Exception-Objekt in JSON-serialisierbare Dicts umwandeln.
    """

    def test_no_ctx_field_unchanged(self):
        """Fehler ohne ctx-Feld werden unverändert zurückgegeben."""
        from api.errors import _sanitize_pydantic_errors
        errors = [{"type": "missing", "loc": ["zone"], "msg": "Field required"}]
        result = _sanitize_pydantic_errors(errors)
        assert result == errors

    def test_ctx_without_error_key_unchanged(self):
        """ctx ohne 'error'-Schlüssel bleibt unverändert."""
        from api.errors import _sanitize_pydantic_errors
        errors = [{"type": "int_parsing", "ctx": {"expected": "int"}, "msg": "..."}]
        result = _sanitize_pydantic_errors(errors)
        assert result[0]["ctx"]["expected"] == "int"

    def test_ctx_error_exception_converted_to_string(self):
        """ctx['error'] als Exception wird zu str(exc) konvertiert."""
        from api.errors import _sanitize_pydantic_errors
        exc = ValueError("Ungültiger Wochentag 7")
        errors = [{"type": "value_error", "ctx": {"error": exc}, "msg": "Value error"}]
        result = _sanitize_pydantic_errors(errors)
        assert result[0]["ctx"]["error"] == "Ungültiger Wochentag 7"
        assert isinstance(result[0]["ctx"]["error"], str)

    def test_ctx_error_string_stays_string(self):
        """ctx['error'] das bereits ein String ist, bleibt unverändert."""
        from api.errors import _sanitize_pydantic_errors
        errors = [{"type": "value_error", "ctx": {"error": "already a string"}, "msg": "..."}]
        result = _sanitize_pydantic_errors(errors)
        assert result[0]["ctx"]["error"] == "already a string"

    def test_empty_errors_list(self):
        """Leere Liste wird korrekt behandelt."""
        from api.errors import _sanitize_pydantic_errors
        assert _sanitize_pydantic_errors([]) == []

    def test_multiple_errors_all_sanitized(self):
        """Alle Einträge in der Liste werden bereinigt."""
        from api.errors import _sanitize_pydantic_errors
        errors = [
            {"type": "value_error", "ctx": {"error": ValueError("Fehler 1")}, "msg": "..."},
            {"type": "value_error", "ctx": {"error": ValueError("Fehler 2")}, "msg": "..."},
        ]
        result = _sanitize_pydantic_errors(errors)
        assert result[0]["ctx"]["error"] == "Fehler 1"
        assert result[1]["ctx"]["error"] == "Fehler 2"

    def test_original_list_not_mutated(self):
        """Die Originalliste und ihre Dicts werden nicht verändert (immutable)."""
        from api.errors import _sanitize_pydantic_errors
        exc = ValueError("test")
        original_error = {"type": "value_error", "ctx": {"error": exc}, "msg": "..."}
        errors = [original_error]
        _sanitize_pydantic_errors(errors)
        # Original-Dict muss unverändert sein
        assert original_error["ctx"]["error"] is exc

    def test_422_response_is_json_serializable(self, client):
        """
        End-to-End: Eine 422-Response durch @field_validator muss JSON-serialisierbar
        sein und einen 'detail'-Key enthalten.
        """
        import json
        resp = client.post("/schedule/add", json={
            "zone": 1,
            "weekdays": [7],  # löst @field_validator ValueError aus
            "start_times": ["06:00"],
            "duration_s": 60,
            "repeat": True,
            "time_unit": "Sekunden",
        })
        assert resp.status_code == 422
        # Wenn dieser Aufruf nicht wirft, ist die Response vollständig JSON-serialisierbar
        data = resp.json()
        assert "detail" in data
        # Doppelt sicher: auch manuelles json.loads funktioniert
        json.loads(resp.text)
