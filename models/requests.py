# models/requests.py
"""
Pydantic-Modelle für eingehende API-Requests.

Step 5 – Input Validation Hardening:
  - time_unit ist jetzt Literal["Sekunden", "Minuten"] statt freiem str.
    Ungültige Werte werden von Pydantic mit 422 Unprocessable Entity abgelehnt,
    bevor der Route-Handler überhaupt ausgeführt wird.
  - ScheduleAddRequest validiert weekdays (0-6) und start_times (HH:MM-Format)
    direkt im Pydantic-Modell via @field_validator.
    Damit entfallen die manuellen Validierungsschleifen in routes_schedule.py.
  - source-Felder werden ausschließlich intern gesetzt und erscheinen bewusst
    NICHT in den Request-Modellen (kein Injection-Risiko).

Status-Codes für Validierungsfehler:
  - Pydantic-Validierungsfehler (Literal, ge/le, field_validator) → 422
  - Laufzeit-abhängige Grenzen (zone > max_valves, duration > hard_max) → 400
    (diese können erst im Route-Handler geprüft werden, da sie State benötigen)
"""

from typing import List, Literal

from pydantic import BaseModel, Field, field_validator


class StartRequest(BaseModel):
    zone: int = Field(..., ge=1)
    duration: int = Field(..., ge=1)
    time_unit: Literal["Sekunden", "Minuten"] = "Minuten"


class QueueAddRequest(BaseModel):
    zone: int = Field(..., ge=1)
    duration: int = Field(..., ge=1)
    time_unit: Literal["Sekunden", "Minuten"] = "Minuten"


class ScheduleAddRequest(BaseModel):
    zone: int = Field(..., ge=0)  # 0 = alle Ventile
    weekdays: List[int] = Field(..., min_length=1)
    start_times: List[str] = Field(..., min_length=1)
    duration_s: int = Field(..., ge=1)
    repeat: bool = True
    time_unit: Literal["Sekunden", "Minuten"] = "Minuten"

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, v: List[int]) -> List[int]:
        """Jeder Wochentag muss im Bereich 0 (Montag) bis 6 (Sonntag) liegen."""
        for wd in v:
            if wd < 0 or wd > 6:
                raise ValueError(
                    f"Ungültiger Wochentag {wd!r}: muss 0 (Montag) bis 6 (Sonntag) sein."
                )
        return v

    @field_validator("start_times")
    @classmethod
    def validate_start_times(cls, v: List[str]) -> List[str]:
        """Jede Startzeit muss das Format 'HH:MM' haben (00:00–23:59)."""
        for t in v:
            # Strukturprüfung: genau 5 Zeichen, Trennzeichen an Position 2
            if len(t) != 5 or t[2] != ":":
                raise ValueError(
                    f"Ungültige Startzeit {t!r}: Format muss 'HH:MM' sein (z.B. '06:00')."
                )
            hh_str, mm_str = t[0:2], t[3:5]
            if not (hh_str.isdigit() and mm_str.isdigit()):
                raise ValueError(
                    f"Ungültige Startzeit {t!r}: Stunden und Minuten müssen Ziffern sein."
                )
            hh, mm = int(hh_str), int(mm_str)
            if not (0 <= hh <= 23):
                raise ValueError(
                    f"Ungültige Startzeit {t!r}: Stunden müssen 00–23 sein, nicht {hh}."
                )
            if not (0 <= mm <= 59):
                raise ValueError(
                    f"Ungültige Startzeit {t!r}: Minuten müssen 00–59 sein, nicht {mm}."
                )
        return v


class ParallelModeRequest(BaseModel):
    enabled: bool
