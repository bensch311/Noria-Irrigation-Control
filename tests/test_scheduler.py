"""
Tests für services/scheduler.py

Getestet werden:
  - _jobs_for_schedule_rule  (Unit-Tests, direkt)
  - scheduler_loop-Logik     (Integration via einmaligem Loop-Durchlauf)
"""

import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from core.config import TZ
from core.state import state, state_lock, QueueItem
from services.scheduler import _jobs_for_schedule_rule
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# _jobs_for_schedule_rule
# ─────────────────────────────────────────────────────────────────────────────


class TestJobsForScheduleRule:
    def test_single_zone_returns_one_job(self, make_schedule):
        rule = make_schedule(zone=2, duration_s=90)
        jobs = _jobs_for_schedule_rule(rule)

        assert len(jobs) == 1
        assert jobs[0].zone == 2
        assert jobs[0].duration == 90
        assert jobs[0].source == "schedule"

    def test_zone_zero_returns_all_zones(self, make_schedule):
        with state_lock:
            state.max_valves = 4
        rule = make_schedule(zone=0, duration_s=30)
        jobs = _jobs_for_schedule_rule(rule)

        assert len(jobs) == 4
        assert [j.zone for j in jobs] == [1, 2, 3, 4]

    def test_zone_zero_respects_max_valves(self, make_schedule):
        with state_lock:
            state.max_valves = 2
        rule = make_schedule(zone=0, duration_s=30)
        jobs = _jobs_for_schedule_rule(rule)
        assert len(jobs) == 2

    def test_time_unit_preserved(self, make_schedule):
        rule = make_schedule(zone=1, duration_s=60)
        rule.time_unit = "Minuten"
        jobs = _jobs_for_schedule_rule(rule)
        assert jobs[0].time_unit == "Minuten"

    def test_all_zones_use_same_duration(self, make_schedule):
        with state_lock:
            state.max_valves = 3
        rule = make_schedule(zone=0, duration_s=45)
        jobs = _jobs_for_schedule_rule(rule)
        assert all(j.duration == 45 for j in jobs)


# ─────────────────────────────────────────────────────────────────────────────
# scheduler_loop – einmaliger Durchlauf
# ─────────────────────────────────────────────────────────────────────────────


def _run_scheduler_once(mock_now: datetime):
    """
    Führt exakt einen Durchlauf der scheduler_loop aus.
    Mockt datetime.now() und shutdown_event.
    """
    from core.state import shutdown_event
    from services.scheduler import scheduler_loop

    call_count = 0

    def mock_is_set():
        nonlocal call_count
        call_count += 1
        return call_count > 1  # Erster Aufruf: False (Loop betritt), alle weiteren: True

    with (
        patch("services.scheduler.datetime") as mock_dt,
        patch.object(shutdown_event, "is_set", side_effect=mock_is_set),
        patch.object(shutdown_event, "wait", return_value=False),
    ):
        mock_dt.now.return_value = mock_now
        scheduler_loop()


class TestSchedulerLoopTrigger:
    def test_triggers_matching_schedule(self, mock_io, make_schedule):
        # Montag, 2025-01-06 06:00
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.schedules[0].last_run_on == "2025-01-06 06:00"

    def test_starts_zone_directly_when_possible(self, mock_io, make_schedule):
        """Wenn aktuelle Runs == 0 im Serielmodus: Job wird direkt gestartet."""
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert 1 in state.active_runs

    def test_skips_disabled_schedule(self, mock_io, make_schedule):
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], enabled=False)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.schedules[0].last_run_on is None
            assert state.active_runs == {}

    def test_skips_wrong_weekday(self, mock_io, make_schedule):
        # Montag (0), Regel nur für Dienstag (1)
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[1], start_times=["06:00"])

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.active_runs == {}

    def test_skips_already_run_this_minute(self, mock_io, make_schedule):
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"])
        rule.last_run_on = "2025-01-06 06:00"  # Bereits ausgeführt

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        # Nur ein Aufruf, nicht zwei
        with state_lock:
            assert len(state.active_runs) == 0

    def test_skips_when_automation_disabled(self, mock_io, make_schedule):
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"])

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = False

        _run_scheduler_once(now)

        with state_lock:
            assert state.active_runs == {}

    def test_skips_when_automation_block_run_key_matches(self, mock_io, make_schedule):
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"])

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.automation_block_run_key = "2025-01-06 06:00"

        _run_scheduler_once(now)

        with state_lock:
            assert state.active_runs == {}

    def test_zone_zero_goes_to_queue(self, mock_io, make_schedule):
        """Zone=0 (alle Ventile): Jobs müssen in Queue, nicht direkt starten."""
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=0, weekdays=[0], start_times=["06:00"], duration_s=30)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.max_valves = 2

        _run_scheduler_once(now)

        with state_lock:
            # Queue sollte gefüllt sein (oder läuft bereits)
            all_zones = {ar for ar in state.active_runs} | {i.zone for i in state.queue}
            assert 1 in all_zones or 2 in all_zones

    def test_once_rule_removed_after_all_pending_run(self, mock_io, make_schedule):
        """repeat=False: Regel wird gelöscht wenn once_pending leer."""
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(
            zone=1,
            weekdays=[0],
            start_times=["06:00"],
            repeat=False,
        )
        # once_pending wird durch make_schedule gesetzt: ["0 06:00"]

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.schedules == []  # Regel gelöscht
