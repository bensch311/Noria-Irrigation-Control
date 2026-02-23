from datetime import datetime

from core.state import state, state_lock, QueueItem, ScheduleRule
from core.config import TZ, MAX_VALVES
from core.logging import log_event
from services.engine import _can_start_new_valve_locked, start_queue_item

def _jobs_for_schedule_rule(rule: ScheduleRule) -> list[QueueItem]:
    max_v = int(getattr(state, "max_valves", MAX_VALVES))
    if rule.zone == 0:
        return [QueueItem(zone=z, duration=rule.duration_s, time_unit=rule.time_unit, source="schedule")
                for z in range(1, max_v + 1)]
    return [QueueItem(zone=rule.zone, duration=rule.duration_s, time_unit=rule.time_unit, source="schedule")]

def scheduler_loop():
    from core.state import shutdown_event  # avoid circular import

    while not shutdown_event.is_set():
        if shutdown_event.wait(1.0):
            break

        try:
            now = datetime.now(TZ)
            weekday = now.weekday()
            hhmm = now.strftime("%H:%M")
            today_key = now.strftime("%Y-%m-%d")

            # Phase 1: Sammle Jobs die gestartet werden sollen (unter Lock)
            jobs_to_start = []
            
            with state_lock:
                if not getattr(state, "automation_enabled", True):
                    continue
                if state.schedules is None:
                    continue

                to_delete_ids: list[str] = []

                for rule in list(state.schedules):
                    if not rule.enabled:
                        continue
                    if weekday not in rule.weekdays:
                        continue
                    if hhmm not in rule.start_times:
                        continue

                    run_key = f"{today_key} {hhmm}"
                    if rule.last_run_on == run_key:
                        continue

                    if state.automation_block_run_key == run_key:
                        log_event(
                            "schedule_skipped",
                            level="warning",
                            source="system",
                            reason="automation_block_minute",
                            schedule_id=rule.id,
                            zone=rule.zone,
                            weekday=weekday,
                            hhmm=hhmm,
                        )
                        continue

                    combo_key = f"{weekday} {hhmm}"
                    if not rule.repeat:
                        if not rule.once_pending:
                            to_delete_ids.append(rule.id)
                            continue
                        if combo_key not in rule.once_pending:
                            continue

                    rule.last_run_on = run_key
                    jobs = _jobs_for_schedule_rule(rule)

                    log_event(
                        "schedule_trigger",
                        source="schedule",
                        schedule_id=rule.id,
                        zone=rule.zone,
                        expanded_zones=[j.zone for j in jobs],
                        jobs_count=len(jobs),
                        duration_s=rule.duration_s,
                        time_unit=rule.time_unit,
                        weekday=weekday,
                        hhmm=hhmm,
                        repeat=rule.repeat,
                        queue_state=state.queue_state,
                        queue_length=len(state.queue or []),
                        automation_enabled=state.automation_enabled,
                    )

                    if rule.zone == 0:
                        # Gruppe immer in Queue (Reihenfolge sichern)
                        state.queue = state.queue or []
                        state.queue.extend(jobs)
                        state.queue_dirty = True
                        if state.queue_state in ("bereit", "fertig"):
                            state.queue_state = "läuft"
                            state.queue_dirty = True
                    else:
                        job = jobs[0]
                        if _can_start_new_valve_locked() and (not state.paused) and state.queue_state != "pausiert":
                            # Merke Job für Start OHNE Lock
                            jobs_to_start.append(job)
                        else:
                            state.queue = state.queue or []
                            state.queue.append(job)
                            state.queue_dirty = True
                            if state.queue_state in ("bereit", "fertig"):
                                state.queue_state = "läuft"
                                state.queue_dirty = True

                    if not rule.repeat:
                        rule.once_pending.remove(combo_key)
                        if len(rule.once_pending) == 0:
                            to_delete_ids.append(rule.id)

                if to_delete_ids:
                    state.schedules = [r for r in (state.schedules or []) if r.id not in to_delete_ids]
                    state.schedules_dirty = True
                    log_event("schedule_done", source="system", deleted_ids=to_delete_ids)
            
            # Phase 2: Starte Jobs OHNE Lock
            for job in jobs_to_start:
                try:
                    start_queue_item(job)
                except Exception as e:
                    # Bei Fehler: Job in Queue einfügen (vorne)
                    with state_lock:
                        state.queue = state.queue or []
                        state.queue.insert(0, job)
                        state.queue_dirty = True
                        if state.queue_state in ("bereit", "fertig"):
                            state.queue_state = "läuft"
                    
                    log_event(
                        "schedule_start_failed",
                        level="error",
                        source="system",
                        zone=job.zone,
                        error=str(e),
                        action="moved_to_queue"
                    )

        except Exception:
            from core.logging import logger
            logger.exception("scheduler_loop crashed")
            log_event("scheduler_error", level="error", source="system")
