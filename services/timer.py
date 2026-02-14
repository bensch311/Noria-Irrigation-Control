import time

from core.state import state, state_lock
from core.logging import log_event, logger
from services.engine import (
    _sync_legacy_single_fields_locked,
    _can_start_new_valve_locked,
    start_queue_item,
    _history_add_locked,
    _calc_actual_run_s_primary,
)

def timer_loop():
    from core.state import shutdown_event

    while not shutdown_event.is_set():
        try:
            if shutdown_event.wait(0.1):
                break

            with state_lock:
                # parallel drain warning
                if (not state.parallel_enabled) and (state.active_runs and len(state.active_runs) > 1):
                    if state.queue_state == "läuft" and (state.queue and len(state.queue) > 0):
                        if not state.parallel_drain_logged:
                            state.parallel_drain_logged = True
                            log_event(
                                "parallel_disabled_waiting_for_drain",
                                level="warning",
                                source="system",
                                running_zones=sorted(list(state.active_runs.keys())),
                                queue_length=len(state.queue or []),
                            )
                else:
                    if state.parallel_drain_logged:
                        state.parallel_drain_logged = False

                # queue fill
                if state.queue_state == "läuft" and not state.paused and state.queue_state != "pausiert":
                    while state.queue and _can_start_new_valve_locked():
                        next_item = state.queue.pop(0)
                        state.queue_dirty = True
                        start_queue_item(next_item)

                    if (not state.queue) and not (state.active_runs and len(state.active_runs) > 0):
                        state.queue_state = "fertig"
                        state.queue_dirty = True
                        log_event(
                            "queue_finished",
                            source="system",
                            queue_state=state.queue_state,
                            queue_length=0,
                            parallel_enabled=state.parallel_enabled,
                            max_concurrent_valves=state.max_concurrent_valves,
                        )

                if state.paused:
                    continue

                now_m = time.monotonic()

                finished = []
                for zone, ar in (state.active_runs or {}).items():
                    if ar.end_time and now_m >= ar.end_time:
                        finished.append(zone)

                for zone in finished:
                    ar = state.active_runs.get(zone)
                    if not ar:
                        continue

                    if zone == state.running_zone:
                        actual_s = _calc_actual_run_s_primary(now_m)
                    else:
                        paused_total = ar.paused_total_s + ((now_m - ar.paused_at) if ar.paused_at else 0.0)
                        active = (now_m - ar.started_at) - paused_total
                        actual_s = max(0, int(active + 1e-6))

                    _history_add_locked(zone=zone, duration_s=actual_s, source=ar.started_source or "manual", time_unit=ar.time_unit)

                    del state.active_runs[zone]

                    log_event(
                        "valve_stop",
                        source="system",
                        zone=zone,
                        reason="duration_elapsed",
                        queue_state=state.queue_state,
                        queue_length=len(state.queue or []),
                        actual_s=actual_s,
                        automation_enabled=state.automation_enabled,
                        parallel_enabled=state.parallel_enabled,
                    )

                _sync_legacy_single_fields_locked()

        except Exception:
            logger.exception("timer_loop crashed")
            log_event("timer_error", level="error", source="system")
            shutdown_event.wait(0.5)
