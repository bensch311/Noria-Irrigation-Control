from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, List, Dict

from core.config import MAX_CONCURRENT_VALVES, DEFAULT_PARALLEL_ENABLED, NAVBAR_TITLE, ACCENT_COLOR, DEFAULT_DURATION, DEFAULT_TIME_UNIT

shutdown_event = threading.Event()
threads: list[threading.Thread] = []

state_lock = threading.Lock()

@dataclass
class ActiveRun:
    zone: int
    end_time: float
    time_unit: str
    started_at: float
    started_source: str
    started_planned_s: int
    paused_at: float = 0.0
    paused_total_s: float = 0.0
    remaining_s: int = 0
    hw_close_failures: int = 0
    hw_next_retry_at: float = 0.0
    hw_last_error: str = ""


@dataclass
class QueueItem:
    zone: int
    duration: int
    time_unit: str
    source: str = "queue"  # manual | queue | schedule

@dataclass
class ScheduleRule:
    id: str
    zone: int  # 1..MAX_VALVES oder 0=alle Ventile
    weekdays: List[int]
    start_times: List[str]
    duration_s: int
    time_unit: str
    repeat: bool
    enabled: bool = True
    last_run_on: Optional[str] = None
    once_pending: Optional[List[str]] = None

@dataclass
class HistoryItem:
    ts_end: str
    zone: int
    duration_s: int
    source: str
    time_unit: str = "Sekunden"

@dataclass
class RunState:
    running_zone: Optional[int] = None
    end_time: float = 0.0
    time_unit: str = "Minuten"

    queue: List[QueueItem] | None = None
    queue_state: str = "bereit"
    queue_state_before_valve_pause: str = "bereit"

    paused: bool = False
    remaining_s: int = 0

    schedules: List[ScheduleRule] | None = None
    automation_enabled: bool = True
    automation_block_run_key: Optional[str] = None

    schedules_dirty: bool = False
    queue_dirty: bool = False
    history_dirty: bool = False

        # hardware fault latch (prevents starting new valves until cleared)
    hw_faulted: bool = False
    hw_fault_reason: str = ""
    hw_fault_zone: Optional[int] = None
    hw_fault_since: str = ""
    hw_fault_close_all_attempted: bool = False

    # legacy runtime accounting (primary)
    started_at: float = 0.0
    started_source: str = "manual"
    started_planned_s: int = 0
    paused_at: float = 0.0
    paused_total_s: float = 0.0

    # parallel
    parallel_enabled: bool = DEFAULT_PARALLEL_ENABLED
    max_concurrent_valves: int = MAX_CONCURRENT_VALVES
    parallel_drain_logged: bool = False

    # device/admin config (loaded from device_config.json)
    max_valves: int = 6
    valve_driver_mode: str = "sim"
    relay_active_low: bool = True
    gpio_pins_by_zone: Dict[int, int] | None = None

    # user settings (user_settings.json)
    max_history_items: int = 20

    # user settings – display & defaults (user_settings.json)
    navbar_title: str = NAVBAR_TITLE
    accent_color: str = ACCENT_COLOR
    default_duration: int = DEFAULT_DURATION
    default_time_unit: str = DEFAULT_TIME_UNIT

    # hard limits (device_config.json)
    hard_max_runtime_s: int = 60 * 60
    hard_max_concurrent_valves: int = 2
    
    active_runs: Dict[int, ActiveRun] | None = None
    run_history: List[HistoryItem] | None = None

state = RunState()
