# services/persistence.py
import os
import json
from datetime import datetime
from core.state import state, state_lock, QueueItem, ScheduleRule, HistoryItem
from core.config import (
    DATA_DIR, SCHEDULES_FILE, QUEUE_FILE, HISTORY_FILE,
    DEVICE_CONFIG_FILE, USER_SETTINGS_FILE, RUNTIME_STATE_FILE,
    TZ, MAX_VALVES, MAX_RUNTIME_S, MAX_HISTORY_ITEMS, MAX_CONCURRENT_VALVES, DEFAULT_PARALLEL_ENABLED,
    NAVBAR_TITLE, ACCENT_COLOR, DEFAULT_DURATION, DEFAULT_TIME_UNIT,
    CORRUPT_FILE_MAX_KEEP,
)
from core.logging import log_event, logger

os.makedirs(DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data: dict):
    """Schreibt JSON atomar: erst .tmp, dann os.replace – crash-safe."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _cleanup_old_corrupt_files(path: str, max_keep: int) -> None:
    """
    Löscht ältere .corrupt-<ts>-Dateien für einen gegebenen Basispfad.

    Behält die max_keep neuesten Backups (Sortierung nach Dateiname, da das
    Timestamp-Format %Y%m%d-%H%M%S lexikografisch chronologisch ist).
    Best-effort: Fehler beim Löschen werden ignoriert.
    """
    directory = os.path.dirname(os.path.abspath(path))
    prefix = os.path.basename(path) + ".corrupt-"
    try:
        candidates = sorted([
            f for f in os.listdir(directory)
            if f.startswith(prefix)
        ])
        # Älteste zuerst (aufsteigend), die letzten max_keep behalten
        to_delete = candidates[:-max_keep] if len(candidates) > max_keep else []
        for fname in to_delete:
            try:
                os.remove(os.path.join(directory, fname))
                log_event("corrupt_file_cleaned", source="system", file=fname)
            except Exception:
                pass  # best effort
    except Exception:
        pass  # best effort


def _backup_corrupt_file(path: str):
    """
    Benennt eine korrupte Datei in <n>.corrupt-<ts> um (best effort) und
    bereinigt anschließend ältere Backups (behält maximal CORRUPT_FILE_MAX_KEEP).
    """
    try:
        ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        os.replace(path, f"{path}.corrupt-{ts}")
    except Exception:
        # best effort – falls Umbenennung nicht möglich ist, ignorieren
        pass
    # Cleanup läuft immer (auch wenn Umbenennung scheiterte), best effort
    _cleanup_old_corrupt_files(path, max_keep=CORRUPT_FILE_MAX_KEEP)


def _default_device_config_payload() -> dict:
    return {
        "version": 1,
        "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "device": {
            "MAX_VALVES": int(MAX_VALVES),
            "IRRIGATION_VALVE_DRIVER": "sim",          # sim | rpi
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},                # {"1": 17, ...} BCM
        },
        "hard_limits": {
            "MAX_RUNTIME_S": int(MAX_RUNTIME_S),
            "MAX_CONCURRENT_VALVES": int(MAX_CONCURRENT_VALVES),
        },
    }


def _default_user_settings_payload() -> dict:
    return {
        "version": 1,
        "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "user": {
            "MAX_HISTORY_ITEMS": int(MAX_HISTORY_ITEMS),
            "NAVBAR_TITLE": NAVBAR_TITLE,
            "ACCENT_COLOR": ACCENT_COLOR,
            "DEFAULT_DURATION": int(DEFAULT_DURATION),
            "DEFAULT_TIME_UNIT": DEFAULT_TIME_UNIT,
        },
    }


def _default_runtime_state_payload() -> dict:
    return {
        "version": 1,
        "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "runtime": {
            "parallel_enabled": bool(DEFAULT_PARALLEL_ENABLED),
            "max_concurrent_valves": int(MAX_CONCURRENT_VALVES),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# device_config (Admin-Konfiguration – read-only im Betrieb)
# ─────────────────────────────────────────────────────────────────────────────

def load_device_config_from_disk():
    payload = None
    if os.path.exists(DEVICE_CONFIG_FILE):
        try:
            with open(DEVICE_CONFIG_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            logger.exception("load_device_config_from_disk failed")
            log_event("device_config_corrupt", level="error", source="system")
            _backup_corrupt_file(DEVICE_CONFIG_FILE)
            payload = None

    if not isinstance(payload, dict):
        payload = _default_device_config_payload()
        try:
            _atomic_write_json(DEVICE_CONFIG_FILE, payload)
            log_event("device_config_created_template", level="warning", source="system")
        except Exception:
            logger.exception("device_config template write failed")

    dev = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    hl = payload.get("hard_limits") if isinstance(payload.get("hard_limits"), dict) else {}

    def _int(x, default):
        try:
            return int(x)
        except Exception:
            return default

    max_valves = max(1, _int(dev.get("MAX_VALVES", MAX_VALVES), MAX_VALVES))

    drv = str(dev.get("IRRIGATION_VALVE_DRIVER", "sim") or "sim").strip().lower()
    if drv not in ("sim", "rpi"):
        drv = "sim"

    active_low = bool(dev.get("IRRIGATION_RELAY_ACTIVE_LOW", True))

    pins_raw = dev.get("IRRIGATION_GPIO_PINS", {})
    pins_norm: dict[int, int] = {}
    if isinstance(pins_raw, dict):
        for k, v in pins_raw.items():
            try:
                z = int(k)
                p = int(v)
                if z >= 1:
                    pins_norm[z] = p
            except Exception:
                continue

    hard_max_runtime_s = max(1, _int(hl.get("MAX_RUNTIME_S", MAX_RUNTIME_S), MAX_RUNTIME_S))
    hard_max_conc = max(1, _int(hl.get("MAX_CONCURRENT_VALVES", MAX_CONCURRENT_VALVES), MAX_CONCURRENT_VALVES))
    hard_max_conc = min(hard_max_conc, max_valves)

    with state_lock:
        state.max_valves = max_valves
        state.valve_driver_mode = drv
        state.relay_active_low = active_low
        state.gpio_pins_by_zone = pins_norm
        state.hard_max_runtime_s = hard_max_runtime_s
        state.hard_max_concurrent_valves = hard_max_conc

    try:
        from services.valve_driver import reset_valve_driver
        reset_valve_driver()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# user_settings
# ─────────────────────────────────────────────────────────────────────────────

def load_user_settings_from_disk():
    payload = None
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            logger.exception("load_user_settings_from_disk failed")
            log_event("user_settings_corrupt", level="error", source="system")
            _backup_corrupt_file(USER_SETTINGS_FILE)
            payload = None

    if not isinstance(payload, dict):
        payload = _default_user_settings_payload()
        try:
            _atomic_write_json(USER_SETTINGS_FILE, payload)
            log_event("user_settings_created", level="warning", source="system")
        except Exception:
            logger.exception("user_settings write failed")

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    try:
        max_hist = int(user.get("MAX_HISTORY_ITEMS", MAX_HISTORY_ITEMS))
    except Exception:
        max_hist = MAX_HISTORY_ITEMS
    max_hist = max(1, max_hist)

    navbar_title = str(user.get("NAVBAR_TITLE", NAVBAR_TITLE) or NAVBAR_TITLE).strip()
    if not navbar_title:
        navbar_title = NAVBAR_TITLE

    accent_color = str(user.get("ACCENT_COLOR", ACCENT_COLOR) or ACCENT_COLOR).strip()
    import re as _re
    if not _re.match(r'^#[0-9a-fA-F]{6}$', accent_color):
        accent_color = ACCENT_COLOR

    try:
        default_duration = max(1, min(120, int(user.get("DEFAULT_DURATION", DEFAULT_DURATION))))
    except Exception:
        default_duration = DEFAULT_DURATION

    default_time_unit = str(user.get("DEFAULT_TIME_UNIT", DEFAULT_TIME_UNIT))
    if default_time_unit not in ("Sekunden", "Minuten"):
        default_time_unit = DEFAULT_TIME_UNIT

    with state_lock:
        state.max_history_items = max_hist
        state.navbar_title = navbar_title
        state.accent_color = accent_color
        state.default_duration = default_duration
        state.default_time_unit = default_time_unit


def save_user_settings_to_disk():
    with state_lock:
        max_hist = int(getattr(state, "max_history_items", MAX_HISTORY_ITEMS))
        navbar_title = str(getattr(state, "navbar_title", NAVBAR_TITLE))
        accent_color = str(getattr(state, "accent_color", ACCENT_COLOR))
        default_duration = int(getattr(state, "default_duration", DEFAULT_DURATION))
        default_time_unit = str(getattr(state, "default_time_unit", DEFAULT_TIME_UNIT))

    payload = _default_user_settings_payload()
    payload["user"]["MAX_HISTORY_ITEMS"] = max_hist
    payload["user"]["NAVBAR_TITLE"] = navbar_title
    payload["user"]["ACCENT_COLOR"] = accent_color
    payload["user"]["DEFAULT_DURATION"] = default_duration
    payload["user"]["DEFAULT_TIME_UNIT"] = default_time_unit
    payload["saved_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    _atomic_write_json(USER_SETTINGS_FILE, payload)


# ─────────────────────────────────────────────────────────────────────────────
# runtime_state (Laufzeit-Toggles: parallel, max_concurrent_valves)
# ─────────────────────────────────────────────────────────────────────────────

def load_runtime_state_from_disk():
    payload = None
    if os.path.exists(RUNTIME_STATE_FILE):
        try:
            with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            logger.exception("load_runtime_state_from_disk failed")
            log_event("runtime_state_corrupt", level="error", source="system")
            _backup_corrupt_file(RUNTIME_STATE_FILE)
            payload = None

    if not isinstance(payload, dict):
        payload = _default_runtime_state_payload()
        try:
            _atomic_write_json(RUNTIME_STATE_FILE, payload)
            log_event("runtime_state_created", level="warning", source="system")
        except Exception:
            logger.exception("runtime_state write failed")

    rt = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    parallel_enabled = bool(rt.get("parallel_enabled", DEFAULT_PARALLEL_ENABLED))

    try:
        max_conc = int(rt.get("max_concurrent_valves", MAX_CONCURRENT_VALVES))
    except Exception:
        max_conc = MAX_CONCURRENT_VALVES

    with state_lock:
        max_valves = int(getattr(state, "max_valves", MAX_VALVES))
        hard_max = int(getattr(state, "hard_max_concurrent_valves", MAX_CONCURRENT_VALVES))
        max_conc = max(1, min(max_valves, min(hard_max, max_conc)))

        state.parallel_enabled = parallel_enabled
        state.max_concurrent_valves = max_conc


def save_runtime_state_to_disk():
    with state_lock:
        parallel_enabled = bool(getattr(state, "parallel_enabled", DEFAULT_PARALLEL_ENABLED))
        max_conc = int(getattr(state, "max_concurrent_valves", MAX_CONCURRENT_VALVES))

    payload = _default_runtime_state_payload()
    payload["runtime"]["parallel_enabled"] = parallel_enabled
    payload["runtime"]["max_concurrent_valves"] = max_conc
    payload["saved_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    _atomic_write_json(RUNTIME_STATE_FILE, payload)


# ─────────────────────────────────────────────────────────────────────────────
# Serializer / Deserializer
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_schedule(rule: ScheduleRule) -> dict:
    return {
        "id": rule.id,
        "zone": rule.zone,
        "weekdays": rule.weekdays,
        "start_times": rule.start_times,
        "duration_s": rule.duration_s,
        "time_unit": rule.time_unit,
        "repeat": rule.repeat,
        "enabled": rule.enabled,
        "last_run_on": rule.last_run_on,
        "once_pending": rule.once_pending,
    }


def _deserialize_schedule(d: dict) -> ScheduleRule:
    return ScheduleRule(
        id=d["id"],
        zone=d["zone"],
        weekdays=d.get("weekdays", []),
        start_times=d.get("start_times", []),
        duration_s=d.get("duration_s", 0),
        time_unit=d.get("time_unit", "Minuten"),
        repeat=d.get("repeat", True),
        enabled=d.get("enabled", True),
        last_run_on=d.get("last_run_on"),
        once_pending=d.get("once_pending"),
    )


def _serialize_queue_item(item: QueueItem) -> dict:
    return {
        "zone": item.zone,
        "duration": item.duration,
        "time_unit": item.time_unit,
        "source": getattr(item, "source", "queue"),
    }


def _deserialize_queue_item(d: dict) -> QueueItem:
    return QueueItem(
        zone=d["zone"],
        duration=d["duration"],
        time_unit=d.get("time_unit", "Minuten"),
        source=d.get("source", "queue"),
    )


def _serialize_history_item(item: HistoryItem) -> dict:
    return {
        "ts_end": item.ts_end,
        "zone": item.zone,
        "duration_s": item.duration_s,
        "source": item.source,
        "time_unit": getattr(item, "time_unit", "Sekunden"),
    }


def _deserialize_history_item(d: dict) -> HistoryItem:
    return HistoryItem(
        ts_end=d.get("ts_end", ""),
        zone=d.get("zone", 0),
        duration_s=d.get("duration_s", 0),
        source=d.get("source", "manual"),
        time_unit=d.get("time_unit", "Sekunden"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schedules
# ─────────────────────────────────────────────────────────────────────────────

def save_schedules_to_disk():
    with state_lock:
        rules = list(state.schedules or [])
        payload = {
            "version": 1,
            "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "automation_enabled": state.automation_enabled,
            "items": [_serialize_schedule(r) for r in rules],
        }
        state.schedules_dirty = False
    _atomic_write_json(SCHEDULES_FILE, payload)


def load_schedules_from_disk():
    if not os.path.exists(SCHEDULES_FILE):
        return
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        logger.exception("load_schedules_from_disk failed")
        log_event("schedules_corrupt", level="error", source="system")
        _backup_corrupt_file(SCHEDULES_FILE)
        return  # State bleibt unverändert (leer/initialisiert)

    auto = payload.get("automation_enabled", True)
    items = payload.get("items", [])
    rules = [_deserialize_schedule(d) for d in items]

    with state_lock:
        state.automation_enabled = bool(auto)
        state.automation_block_run_key = None
        if state.automation_enabled:
            now = datetime.now(TZ)
            state.automation_block_run_key = now.strftime("%Y-%m-%d %H:%M")
        state.schedules = rules
        state.schedules_dirty = False


# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────

def save_queue_to_disk():
    with state_lock:
        q = list(state.queue or [])
        payload = {
            "version": 1,
            "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "queue_state": state.queue_state,
            "items": [_serialize_queue_item(i) for i in q],
        }
        state.queue_dirty = False
    _atomic_write_json(QUEUE_FILE, payload)


def load_queue_from_disk():
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        logger.exception("load_queue_from_disk failed")
        log_event("queue_corrupt", level="error", source="system")
        _backup_corrupt_file(QUEUE_FILE)
        return  # State bleibt unverändert

    items = payload.get("items", [])
    q = [_deserialize_queue_item(d) for d in items]

    with state_lock:
        state.queue = q
        state.queue_state = "bereit"
        state.queue_dirty = False


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────

def save_history_to_disk():
    with state_lock:
        items = list(state.run_history or [])
        payload = {
            "version": 1,
            "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "items": [_serialize_history_item(i) for i in items],
        }
        state.history_dirty = False
    _atomic_write_json(HISTORY_FILE, payload)


def load_history_from_disk():
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        logger.exception("load_history_from_disk failed")
        log_event("history_corrupt", level="error", source="system")
        _backup_corrupt_file(HISTORY_FILE)
        return  # State bleibt unverändert

    items = payload.get("items", [])
    hist = [_deserialize_history_item(d) for d in items]

    with state_lock:
        limit = int(getattr(state, "max_history_items", MAX_HISTORY_ITEMS))
        limit = max(1, limit)
        state.run_history = hist[:limit]
        state.history_dirty = False


# ─────────────────────────────────────────────────────────────────────────────
# Persistence Loop
# ─────────────────────────────────────────────────────────────────────────────

def persistence_loop():
    from core.state import shutdown_event

    while not shutdown_event.is_set():
        if shutdown_event.wait(2.0):
            break
        try:
            with state_lock:
                do_sched = bool(state.schedules_dirty)
                do_queue = bool(state.queue_dirty)
                do_hist = bool(state.history_dirty)

            if do_sched:
                save_schedules_to_disk()
                log_event("persist_schedules", source="system")
            if do_queue:
                save_queue_to_disk()
                log_event("persist_queue", source="system")
            if do_hist:
                save_history_to_disk()
                log_event("persist_history", source="system")

        except Exception:
            logger.exception("persistence_loop crashed")
            log_event("persistence_error", level="error", source="system")
            shutdown_event.wait(2.0)
