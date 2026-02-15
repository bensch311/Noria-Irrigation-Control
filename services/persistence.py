import os
import json
from datetime import datetime

from core.state import state, state_lock, QueueItem, ScheduleRule, HistoryItem
from core.config import (
    DATA_DIR, SCHEDULES_FILE, QUEUE_FILE, HISTORY_FILE, SETTINGS_FILE,
    TZ, MAX_VALVES, MAX_RUNTIME_S, MAX_HISTORY_ITEMS, MAX_CONCURRENT_VALVES, DEFAULT_PARALLEL_ENABLED
)
from core.logging import log_event, logger

os.makedirs(DATA_DIR, exist_ok=True)

def _atomic_write_json(path: str, data: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

def _default_settings_payload() -> dict:
    return {
        "version": 1,
        "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "config": {
            "MAX_VALVES": int(MAX_VALVES),
            "MAX_RUNTIME_S": int(MAX_RUNTIME_S),
            "MAX_HISTORY_ITEMS": int(MAX_HISTORY_ITEMS),
            "MAX_CONCURRENT_VALVES": int(MAX_CONCURRENT_VALVES),
            "DEFAULT_PARALLEL_ENABLED": bool(DEFAULT_PARALLEL_ENABLED),
            "IRRIGATION_VALVE_DRIVER": "sim",
            "IRRIGATION_RELAY_ACTIVE_LOW": True,
            "IRRIGATION_GPIO_PINS": {},  # z.B. {"1":17,"2":27,...} (BCM)
        },
        "runtime": {
            "parallel_enabled": bool(DEFAULT_PARALLEL_ENABLED),
            "max_concurrent_valves": int(MAX_CONCURRENT_VALVES),
        },
    }

def _validate_settings_payload(payload: dict | None) -> dict:
    base = _default_settings_payload()
    if not isinstance(payload, dict):
        return base

    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    rt = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}

    def _int(x, default):
        try:
            return int(x)
        except Exception:
            return default

    max_valves = max(1, _int(cfg.get("MAX_VALVES", base["config"]["MAX_VALVES"]), base["config"]["MAX_VALVES"]))
    max_runtime_s = max(1, _int(cfg.get("MAX_RUNTIME_S", base["config"]["MAX_RUNTIME_S"]), base["config"]["MAX_RUNTIME_S"]))
    max_hist = max(1, _int(cfg.get("MAX_HISTORY_ITEMS", base["config"]["MAX_HISTORY_ITEMS"]), base["config"]["MAX_HISTORY_ITEMS"]))

    max_conc_default = _int(cfg.get("MAX_CONCURRENT_VALVES", base["config"]["MAX_CONCURRENT_VALVES"]), base["config"]["MAX_CONCURRENT_VALVES"])
    max_conc_default = max(1, min(max_valves, max_conc_default))

    default_parallel = bool(cfg.get("DEFAULT_PARALLEL_ENABLED", base["config"]["DEFAULT_PARALLEL_ENABLED"]))

    # Driver settings (config)
    drv = (cfg.get("IRRIGATION_VALVE_DRIVER", "sim") or "sim")
    drv = str(drv).strip().lower()
    if drv not in ("sim", "rpi"):
        drv = "sim"

    active_low = cfg.get("IRRIGATION_RELAY_ACTIVE_LOW", True)
    active_low = bool(active_low)

    pins_raw = cfg.get("IRRIGATION_GPIO_PINS", {})
    pins_norm: dict[str, int] = {}
    if isinstance(pins_raw, dict):
        for k, v in pins_raw.items():
            try:
                z = int(k)
                p = int(v)
                if z >= 1:
                    pins_norm[str(z)] = p
            except Exception:
                continue

    # --- GPIO pin coverage validation ---
    max_valves = int(cfg.get("MAX_VALVES", 1))

    if drv == "rpi":
        required_zones = set(range(1, max_valves + 1))
        defined_zones = set(int(k) for k in pins_norm.keys())

        missing = sorted(list(required_zones - defined_zones))

        if missing:
            log_event(
                "settings_gpio_coverage_invalid",
                level="error",
                source="system",
                missing_zones=missing,
                max_valves=max_valves,
                message="IRRIGATION_GPIO_PINS deckt nicht alle Zonen ab (für rpi).",
            )


    parallel_enabled = bool(rt.get("parallel_enabled", default_parallel))
    max_concurrent = _int(rt.get("max_concurrent_valves", max_conc_default), max_conc_default)
    max_concurrent = max(1, min(max_valves, max_concurrent))

    return {
        "version": 1,
        "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "config": {
            "MAX_VALVES": max_valves,
            "MAX_RUNTIME_S": max_runtime_s,
            "MAX_HISTORY_ITEMS": max_hist,
            "MAX_CONCURRENT_VALVES": max_conc_default,
            "DEFAULT_PARALLEL_ENABLED": default_parallel,
            "IRRIGATION_VALVE_DRIVER": drv,
            "IRRIGATION_RELAY_ACTIVE_LOW": active_low,
            "IRRIGATION_GPIO_PINS": pins_norm,
        },
        "runtime": {
            "parallel_enabled": parallel_enabled,
            "max_concurrent_valves": max_concurrent,
        },
    }

def save_settings_to_disk():
    with state_lock:
        payload = _default_settings_payload()
        payload["runtime"]["parallel_enabled"] = bool(getattr(state, "parallel_enabled", DEFAULT_PARALLEL_ENABLED))
        payload["runtime"]["max_concurrent_valves"] = int(getattr(state, "max_concurrent_valves", MAX_CONCURRENT_VALVES))
        payload = _validate_settings_payload(payload)
    _atomic_write_json(SETTINGS_FILE, payload)

def load_settings_from_disk():
    payload = None
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            logger.exception("load_settings_from_disk failed")
            payload = None

    payload = _validate_settings_payload(payload)

    from services.engine import _sync_legacy_single_fields_locked

    with state_lock:
        state.parallel_enabled = bool(payload["runtime"]["parallel_enabled"])
        state.max_concurrent_valves = int(payload["runtime"]["max_concurrent_valves"])
        # config-driven runtime parameters
        state.max_valves = int(payload["config"]["MAX_VALVES"])

        state.valve_driver_mode = str(payload["config"].get("IRRIGATION_VALVE_DRIVER", "sim")).strip().lower()
        state.relay_active_low = bool(payload["config"].get("IRRIGATION_RELAY_ACTIVE_LOW", True))

        pins = payload["config"].get("IRRIGATION_GPIO_PINS", {})
        # normalized as dict[str,int] in validator; keep as dict[int,int] in state
        pins_by_zone = {}
        if isinstance(pins, dict):
            for k, v in pins.items():
                try:
                    pins_by_zone[int(k)] = int(v)
                except Exception:
                    continue
        state.gpio_pins_by_zone = pins_by_zone
        
        _sync_legacy_single_fields_locked()
    
    try:
        from services.valve_driver import reset_valve_driver
        reset_valve_driver()
    except Exception:
        pass
    

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
    with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

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
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    items = payload.get("items", [])
    q = [_deserialize_queue_item(d) for d in items]

    with state_lock:
        state.queue = q
        state.queue_state = "bereit"
        state.queue_dirty = False

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
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", [])
    hist = [_deserialize_history_item(d) for d in items]

    with state_lock:
        state.run_history = hist[:MAX_HISTORY_ITEMS]
        state.history_dirty = False

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
