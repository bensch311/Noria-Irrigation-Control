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
    zone: int = Field(..., ge=0)  # 0 = alle Ventile (wie ScheduleAddRequest)
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


class SimSensorSetRequest(BaseModel):
    """Request-Modell für POST /sensors/sim/set.

    Setzt beliebig viele Zonen gleichzeitig auf trocken oder feucht.
    Zonen die in beiden Listen auftauchen sind ein Validierungsfehler (422).

    Nur gueltig wenn sensor_driver_mode == "sim" – im Produktionsmodus
    antwortet der Endpunkt mit 404.

    dry_sensors:   Sensor-IDs die als trocken markiert werden (needs_irrigation=True).
    moist_sensors: Sensor-IDs die als feucht markiert werden (needs_irrigation=False).

    Beide Listen sind optional; ein leerer Body tut nichts und gibt den
    aktuellen Zustand zurueck. So kann der Endpunkt auch rein zum Abfragen
    des Sim-Zustands genutzt werden.
    """
    dry_sensors:   List[int] = Field(default_factory=list)
    moist_sensors: List[int] = Field(default_factory=list)

    @field_validator("dry_sensors", "moist_sensors")
    @classmethod
    def validate_sensor_ids_positive(cls, v: List[int]) -> List[int]:
        for s in v:
            if s < 1:
                raise ValueError(
                    f"Sensor-ID muss >= 1 sein, bekommen: {s}"
                )
        return v

    def model_post_init(self, __context: object) -> None:
        """Prueft dass keine Sensor-ID in beiden Listen vorkommt."""
        overlap = set(self.dry_sensors) & set(self.moist_sensors)
        if overlap:
            raise ValueError(
                f"Sensor-IDs {sorted(overlap)} kommen in dry_sensors UND moist_sensors vor."
            )


class SettingsUpdateRequest(BaseModel):
    """Request-Modell für POST /settings.

    max_history_items   : Anzahl Verlaufseintraege (1–500).
    navbar_title        : Angezeigter Titel in der Navigationsleiste (1–50 Zeichen).
    accent_color        : Akzentfarbe als Hex-String, z.B. "#82372a".
    default_duration    : Standardwert der Dauer-Slider (1–1440).
                          Muss ≤ slider_max_minutes sein – wird im Route-Handler
                          dynamisch gegen den gecappten slider_max_minutes geprüft.
    default_time_unit   : Standard-Zeiteinheit fuer Dauer-Radiobuttons.
    slider_max_minutes  : Maximaler Anzeigewert der Laufzeit-Slider in Minuten (1–1440).
                          Darf hard_max_runtime_s // 60 nicht übersteigen –
                          wird im Route-Handler dynamisch geprüft.

    max_history_items ist required (bestehende API-Kompatibilitaet).
    Alle anderen Felder haben Defaults und sind optional.
    """
    max_history_items: int = Field(..., ge=1, le=500)
    navbar_title: str = Field("Bewaesserungscomputer", min_length=1, max_length=50)
    accent_color: str = Field("#82372a")
    # le=1440 statt le=120: default_duration muss ≤ slider_max_minutes sein; da
    # slider_max_minutes bis 1440 gehen kann, braucht default_duration denselben
    # absoluten Pydantic-Cap. Die Prüfung ≤ slider_max_minutes folgt im Route-Handler.
    default_duration: int = Field(5, ge=1, le=1440)
    default_time_unit: Literal["Sekunden", "Minuten"] = "Minuten"
    # 1440 = 24 h als absoluter Pydantic-Cap; dynamische Prüfung gegen
    # hard_max_runtime_s // 60 erfolgt im Route-Handler.
    slider_max_minutes: int = Field(60, ge=1, le=1440)

    @field_validator("navbar_title")
    @classmethod
    def validate_navbar_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Navbar-Titel darf nicht leer sein.")
        return v

    @field_validator("accent_color")
    @classmethod
    def validate_accent_color(cls, v: str) -> str:
        import re
        v = v.strip().lower()
        if not re.match(r'^#[0-9a-f]{6}$', v):
            raise ValueError(
                f"Ungueltige Akzentfarbe {v!r}: muss ein 6-stelliger Hex-Farbwert sein "
                "(z.B. '#82372a')."
            )
        return v

class SensorSettingsRequest(BaseModel):
    """Request-Modell für PATCH /sensors/settings.

    Setzt die Sensor-Betriebsparameter Cooldown und Standard-Bewässerungsdauer.

    Diese zwei Parameter sind als Operator-Einstellungen im UI editierbar.
    Alle anderen Sensor-Parameter (Pins, Treiber, Pull-Up, Polling-Intervall)
    sind Hardware-Admin-Konfiguration und ausschliesslich via install.sh setzbar.

    cooldown_s         : Sperrzeit nach einem Sensor-Trigger in Sekunden.
                         0 = kein Cooldown (Sensor kann sofort erneut auslösen).
                         Maximaler sinnvoller Wert: 86400 (24 Stunden).

    default_duration_s : Standard-Bewässerungsdauer bei Sensor-Trigger in Sekunden.
                         Minimum 60 s (1 Minute), Maximum 3600 s (1 Stunde –
                         entspricht dem Standard-Hard-Limit MAX_RUNTIME_S).
                         Die genaue Prüfung gegen hard_max_runtime_s
                         erfolgt im Route-Handler.
    """
    cooldown_s:          int = Field(..., ge=0, le=86400)
    default_duration_s:  int = Field(..., ge=60, le=3600)


class SensorSingleSettings(BaseModel):
    """Betriebsparameter für einen einzelnen Sensor.

    cooldown_s  : Sperrzeit nach einem Sensor-Trigger in Sekunden.
                  0 = kein Cooldown. Maximum 14400 s (4 Stunden).
    duration_s  : Bewässerungsdauer bei Sensor-Trigger in Sekunden.
                  Minimum 60 s (1 Minute). Absolutes Pydantic-Cap 3600 s (1 Stunde);
                  dynamische Prüfung gegen hard_max_runtime_s erfolgt im Route-Handler.
    """
    cooldown_s:  int = Field(..., ge=0, le=14400)
    duration_s:  int = Field(..., ge=60, le=3600)


class SensorSettingsRequest(BaseModel):
    """Request-Modell für PATCH /sensors/settings.

    Setzt Cooldown und Bewässerungsdauer für jeden Sensor individuell.
    PUT-Semantik: die gesamte bisherige settings-Map wird ersetzt.

    settings: Dict sensor_id (str) → SensorSingleSettings.
              Leerer Dict = alle Einstellungen zurücksetzen (nicht empfohlen).
              Sensor-IDs die nicht in IRRIGATION_SENSOR_PINS konfiguriert sind
              werden akzeptiert und gespeichert – der Route-Handler warnt nicht
              (analog zur assignments-Logik).

    Beispiel: {"1": {"cooldown_s": 3600, "duration_s": 600},
               "2": {"cooldown_s": 7200, "duration_s": 300}}
    """
    settings: dict[str, SensorSingleSettings] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def validate_sensor_ids(cls, v: dict) -> dict:
        for sid_str in v:
            try:
                sid = int(sid_str)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Sensor-ID muss eine ganze Zahl sein, bekommen: {sid_str!r}"
                )
            if sid < 1:
                raise ValueError(f"Sensor-ID muss >= 1 sein, bekommen: {sid}")
        return v


class SensorAssignmentRequest(BaseModel):
    """Request-Modell für POST /sensors/assignments.

    Setzt die Zuordnung Sensor-ID → Ventil-Zonen vollständig neu.
    Die gesamte bisherige Zuordnung wird durch die übermittelte ersetzt.

    assignments: Dict mit sensor_id (str) → Liste der zugeordneten Zonen.
                 Leere Liste = Sensor hat keine Zonen (deaktiviert).
                 Nicht enthaltene Sensor-IDs behalten ihre bisherige Zuordnung NICHT –
                 die gesamte Zuordnung wird ersetzt (PUT-Semantik).

    Beispiel: {"1": [1, 2, 3], "2": [4, 5]}
    """
    assignments: dict[str, List[int]] = Field(default_factory=dict)

    @field_validator("assignments")
    @classmethod
    def validate_assignments(cls, v: dict) -> dict:
        for sid_str, zones in v.items():
            try:
                sid = int(sid_str)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Sensor-ID muss eine ganze Zahl sein, bekommen: {sid_str!r}"
                )
            if sid < 1:
                raise ValueError(f"Sensor-ID muss >= 1 sein, bekommen: {sid}")
            for z in zones:
                try:
                    zi = int(z)
                except (ValueError, TypeError):
                    raise ValueError(
                        f"Zonen-Nummer muss eine ganze Zahl sein, bekommen: {z!r}"
                    )
                if zi < 1:
                    raise ValueError(f"Zonen-Nummer muss >= 1 sein, bekommen: {zi}")
        return v
