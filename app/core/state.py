# core/state.py
"""
Zentraler In-Memory-Zustand des Bewässerungscomputers.

Dieses Modul definiert:
  - Alle Dataclasses (ActiveRun, QueueItem, ScheduleRule, HistoryItem, RunState)
  - Die globale Singleton-Instanz `state` (RunState)
  - Den globalen `state_lock` (threading.Lock) für thread-sicheren Zugriff
  - `shutdown_event` und `threads` für koordinierten Lifecycle

WICHTIG – Thread-Safety:
  Jeder Zugriff auf `state` muss unter `state_lock` erfolgen, außer in
  Funktionen die explizit "_locked" im Namen tragen (diese erwarten, dass
  der Lock bereits gehalten wird).

  Einzige Ausnahme: read-only Zugriff auf rein immutable Felder wie
  `state.max_valves` ist in Einzelfällen ohne Lock tolerierbar, sollte
  aber als Ausnahme dokumentiert werden.

WICHTIG – active_runs ist die einzige Quelle der Wahrheit für Ventilzustand:
  Ein Ventil gilt genau dann als "läuft", wenn es in state.active_runs vorhanden
  ist. Kein anderes Flag darf diesen Zustand duplizieren oder widersprechen.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, List, Dict

from core.config import MAX_CONCURRENT_VALVES, DEFAULT_PARALLEL_ENABLED, NAVBAR_TITLE, ACCENT_COLOR, DEFAULT_DURATION, DEFAULT_TIME_UNIT, SLIDER_MAX_MINUTES

shutdown_event = threading.Event()
threads: list[threading.Thread] = []

state_lock = threading.Lock()


@dataclass
class ActiveRun:
    """Repräsentiert einen aktiven Bewässerungslauf für eine Zone.

    Wird in RunState.active_runs unter dem Zonen-Key gespeichert.
    Wird nur unter state_lock erstellt/verändert/gelesen.
    """
    zone: int
    end_time: float           # monotonic timestamp – Ablaufzeitpunkt (0.0 wenn pausiert)
    time_unit: str            # "Sekunden" | "Minuten" – nur für Anzeige
    started_at: float         # monotonic timestamp – Startzeitpunkt
    started_source: str       # "manual" | "queue" | "schedule" | "sensor"
    started_planned_s: int    # ursprünglich geplante Laufzeit in Sekunden

    paused_at: float = 0.0          # monotonic timestamp des letzten Pause-Zeitpunkts (0.0 = nicht pausiert)
    paused_total_s: float = 0.0     # kumulierte Pausenzeit in Sekunden (alle bisherigen Pausen)
    remaining_s: int = 0            # verbleibende Sekunden zum Zeitpunkt der letzten Pause

    # Retry/Backoff-Mechanismus für Hardware-Fehler beim Schließen (timer.py):
    # Nach einem fehlgeschlagenen close() bleibt die Zone in active_runs und wird
    # mit exponentiellem Backoff erneut versucht, bis HW_CLOSE_MAX_RETRIES erreicht ist.
    hw_close_failures: int = 0      # Anzahl bisheriger fehlgeschlagener close()-Versuche
    hw_next_retry_at: float = 0.0   # monotonic timestamp: frühester nächster Retry-Zeitpunkt
    hw_last_error: str = ""         # Fehlermeldung des letzten Hardware-Fehlers (für Logging)


@dataclass
class QueueItem:
    """Ein Eintrag in der Bewässerungswarteschlange."""
    zone: int
    duration: int       # Laufzeit in Sekunden
    time_unit: str
    source: str = "queue"   # "manual" | "queue" | "schedule" | "sensor" – Ursprung des Eintrags


@dataclass
class ScheduleRule:
    """Eine Zeitplan-Regel: wann welche Zone wie lange bewässert werden soll."""
    id: str
    zone: int           # 1..MAX_VALVES oder 0 = alle Ventile sequenziell
    weekdays: List[int] # 0=Montag .. 6=Sonntag (Python-Standard)
    start_times: List[str]  # ["HH:MM", ...] im Format "06:00"
    duration_s: int
    time_unit: str
    repeat: bool        # True: wiederholt sich wöchentlich. False: Einmalregel.
    enabled: bool = True
    last_run_on: Optional[str] = None  # run_key "YYYY-MM-DD HH:MM" des letzten Auslösens

    # Für Einmal-Regeln (repeat=False): Liste der noch ausstehenden run_keys.
    # Format: ["Wochentag HH:MM", ...] z.B. ["2 06:00"].
    # Wenn die Liste leer ist, wird die Regel automatisch gelöscht.
    once_pending: Optional[List[str]] = None


@dataclass
class HistoryItem:
    """Ein abgeschlossener Bewässerungslauf im Verlauf."""
    ts_end: str         # ISO-8601-Zeitstempel des Laufendes (Europa/Berlin)
    zone: int
    duration_s: int     # tatsächlich gelaufene Sekunden
    source: str         # "manual" | "queue" | "schedule" | "sensor"
    time_unit: str = "Sekunden"


@dataclass
class RunState:
    """Gesamter Laufzeit-Zustand des Bewässerungscomputers.

    Instanziiert als globales Singleton `state`.
    Alle Felder sind unter state_lock zu lesen und zu schreiben.

    Felder-Gruppen:
      - Ventil-Zustand:         paused, active_runs
      - Queue:                  queue, queue_state, queue_state_before_valve_pause
      - Zeitpläne:              schedules, automation_enabled, automation_block_run_key
      - Dirty-Flags:            schedules_dirty, queue_dirty, history_dirty
      - Hardware-Fault-Latch:   hw_faulted, hw_fault_*
      - Parallel-Modus:         parallel_enabled, max_concurrent_valves, parallel_drain_logged
      - Device-Konfiguration:   max_valves, valve_driver_mode, relay_active_low, gpio_pins_by_zone
      - Sensor-Konfiguration:   sensor_driver_mode, sensor_gpio_pins,
                                sensor_internal_pull_up, sensor_polling_interval_s,
                                sensor_cooldown_s, sensor_default_duration_s
      - Sensor-Betriebsparameter: sensor_settings_by_id (pro Sensor: cooldown_s, duration_s)
      - Sensor-Zuordnung:       sensor_zone_assignments (sensor_id → [zone, ...])
      - Sensor-Laufzeit:        sensor_readings, sensor_last_triggered
      - User-Settings:          max_history_items, navbar_title, accent_color, …
      - Hard-Limits:            hard_max_runtime_s, hard_max_concurrent_valves
      - Neustart-Erkennung:     unclean_restart, restart_detected_at
      - Verlauf:                run_history
    """

    # ── Ventil-Zustand ────────────────────────────────────────────────────────
    paused: bool = False
    # active_runs: Zone → ActiveRun. EINZIGE Quelle der Wahrheit für laufende Ventile.
    active_runs: Dict[int, ActiveRun] | None = None

    # ── Queue ─────────────────────────────────────────────────────────────────
    queue: List[QueueItem] | None = None
    queue_state: str = "bereit"   # "bereit" | "läuft" | "pausiert" | "fertig"

    # Speichert queue_state unmittelbar vor einer Ventil-Pause, damit /resume
    # den korrekten Zustand ("läuft" vs "bereit") wiederherstellen kann.
    queue_state_before_valve_pause: str = "bereit"

    # ── Zeitpläne ─────────────────────────────────────────────────────────────
    schedules: List[ScheduleRule] | None = None
    automation_enabled: bool = True

    # Blockiert den scheduler_loop für genau eine Minute nach dem Start/Reload
    # der Zeitpläne. Format: "YYYY-MM-DD HH:MM" des aktuellen run_key.
    # Verhindert, dass ein Zeitplan beim Serverstart für die aktuelle Minute
    # sofort ausgelöst wird, wenn er kurz vor dem Start bereits hätte laufen sollen.
    automation_block_run_key: Optional[str] = None

    # ── Dirty-Flags (persistence_loop) ───────────────────────────────────────
    schedules_dirty: bool = False
    queue_dirty: bool = False
    history_dirty: bool = False

    # ── Hardware-Fault-Latch ──────────────────────────────────────────────────
    # Wird gesetzt wenn alle HW_CLOSE_MAX_RETRIES fehlschlagen.
    # Verhindert das Starten neuer Ventile bis /fault/clear aufgerufen wird.
    hw_faulted: bool = False
    hw_fault_reason: str = ""
    hw_fault_zone: Optional[int] = None
    hw_fault_since: str = ""
    hw_fault_close_all_attempted: bool = False  # True = close_all nach Fault bereits versucht

    # ── Parallel-Modus ────────────────────────────────────────────────────────
    parallel_enabled: bool = DEFAULT_PARALLEL_ENABLED
    max_concurrent_valves: int = MAX_CONCURRENT_VALVES

    # Verhindert wiederholtes Logging beim Übergang "parallel drain → bereit".
    # Wird auf True gesetzt wenn das Log-Event gesendet wurde, auf False zurück
    # wenn neue Ventile starten.
    parallel_drain_logged: bool = False

    # ── Device-Konfiguration (aus device_config.json) ─────────────────────────
    max_valves: int = 6
    valve_driver_mode: str = "sim"      # "sim" | "rpi"
    relay_active_low: bool = True       # True = Relais-Board mit Active-Low-Logik
    gpio_pins_by_zone: Dict[int, int] | None = None  # {zone: BCM-Pin}

    # ── Sensor-Konfiguration (aus device_config.json) ─────────────────────────
    sensor_driver_mode: str = "sim"                    # "sim" | "rpi_switch"
    sensor_gpio_pins: Dict[int, int] | None = None   # {sensor_id: BCM-Pin} – Hardware-Konfig
    sensor_internal_pull_up: bool = False  # True = internen Pi-Pull-Up (~50kΩ) verwenden

    # Polling-Intervall: wie oft alle Sensor-Zonen gelesen werden (Sekunden).
    # Minimum ist 5s (wird in sensor_engine_loop geclampt).
    sensor_polling_interval_s: int = 30

    # Cooldown: Mindestabstand zwischen zwei sensor-getriggerten Läufen pro Zone (Sekunden).
    # Verhindert Dauerbewässerung bei dauerhaft trockenem Boden oder defektem Sensor.
    sensor_cooldown_s: int = 600

    # Standard-Laufzeit sensor-getriggerter Bewässerungsläufe (Sekunden).
    # Wird als duration in QueueItem.duration verwendet (source="sensor").
    sensor_default_duration_s: int = 300

    # Sensor-Zonen-Zuordnung: welcher Sensor welche Ventil-Zonen steuert.
    # {sensor_id: [zone, ...]} – z.B. {1: [1, 2, 3], 2: [4, 5]}.
    # Wird aus sensor_assignments.json geladen und via POST /sensors/assignments
    # gespeichert. None = noch nicht geladen.
    sensor_zone_assignments: Dict[int, list] | None = None

    # Sensor-Betriebsparameter: Cooldown und Standard-Bewässerungsdauer pro Sensor.
    # {sensor_id: {"cooldown_s": int, "duration_s": int}}
    # Wird zusammen mit sensor_zone_assignments in sensor_assignments.json persistiert.
    # Nicht vorhanden = noch nicht geladen; bei Erststart mit globalen Defaults befüllt.
    sensor_settings_by_id: Dict[int, dict] | None = None

    # Dirty-Flag für persistence_loop
    sensor_assignments_dirty: bool = False

    # ── Sensor-Laufzeit (rein in-memory, kein Persist) ────────────────────────
    # sensor_readings:       Letzter bekannter Feuchtezustand pro Sensor.
    #                        None = noch kein Lesevorgang seit Start.
    #                        {sensor_id: needs_irrigation} – True = trocken.
    sensor_readings: Dict[int, bool] | None = None

    # sensor_last_triggered: Monotonic-Timestamp des letzten sensor-getriggerten
    #                        Laufstarts pro Sensor. Für Cooldown-Berechnung.
    #                        None = noch kein Trigger seit Start.
    sensor_last_triggered: Dict[int, float] | None = None

    # ── User-Settings (aus user_settings.json) ────────────────────────────────
    max_history_items: int = 20
    navbar_title: str = NAVBAR_TITLE
    accent_color: str = ACCENT_COLOR
    default_duration: int = DEFAULT_DURATION
    default_time_unit: str = DEFAULT_TIME_UNIT
    # Maximaler Anzeigewert der Laufzeit-Slider in Minuten.
    # Wird in den Einstellungen konfiguriert; darf hard_max_runtime_s // 60 nicht
    # übersteigen (wird beim Laden und beim POST /settings geprüft).
    slider_max_minutes: int = SLIDER_MAX_MINUTES

    # ── Hard-Limits (aus device_config.json) ──────────────────────────────────
    # Diese Limits überschreiben User-Eingaben im Route-Handler.
    hard_max_runtime_s: int = 60 * 60           # maximale Einzellaufzeit in Sekunden
    hard_max_concurrent_valves: int = 2         # maximale parallele Ventile (Hardware-Limit)

    # ── Neustart-Erkennung (Sentinel-File-Muster) ─────────────────────────────
    # unclean_restart=True: letzter Shutdown war nicht sauber (Stromausfall/SIGKILL/OOM).
    # Wird beim Startup gesetzt wenn running.lock noch existiert (→ kein sauberer Shutdown).
    # Wird über POST /system/ack-restart quittiert (setzt beide Felder zurück).
    # Rein in-memory: überlebt keinen weiteren Neustart (dann wird neu geprüft).
    unclean_restart: bool = False
    restart_detected_at: str = ""     # ISO-8601-Zeitstempel des erkannten Neustarts

    # ── Verlauf ───────────────────────────────────────────────────────────────
    run_history: List[HistoryItem] | None = None


state = RunState()
