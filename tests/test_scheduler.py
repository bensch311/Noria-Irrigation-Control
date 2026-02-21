"""
Tests für services/scheduler.py

Getestet werden:
  - _jobs_for_schedule_rule  (Unit-Tests, direkt)
  - scheduler_loop-Logik     (Integration via einmaligem Loop-Durchlauf)
    - Phase 1: Filterung (disabled, weekday, time, last_run_on, automation, block_key)
    - Phase 1: once-Regeln (combo_key passt/passt nicht, sofortige Löschung)
    - Phase 1: zone=0 → Queue, zone>0 → direkt oder Queue
    - Phase 1: paused / pausiert → Queue statt Direktstart
    - Phase 2: start_queue_item erfolgreich
    - Phase 2: start_queue_item wirft Exception → Job landet vorne in Queue
"""

from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from core.config import TZ
from core.state import state, state_lock, QueueItem
from services.scheduler import _jobs_for_schedule_rule
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: einmaliger scheduler_loop-Durchlauf
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
# Phase 1: Filterung und Trigger-Logik
# ─────────────────────────────────────────────────────────────────────────────


class TestSchedulerLoopTrigger:
    def test_triggers_matching_schedule(self, mock_io, make_schedule):
        """Passender Zeitpunkt setzt last_run_on."""
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.schedules[0].last_run_on == "2025-01-06 06:00"

    def test_starts_zone_directly_when_possible(self, mock_io, make_schedule):
        """Im Serielmodus mit freiem System: Job wird direkt gestartet."""
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
            all_zones = {ar for ar in state.active_runs} | {i.zone for i in state.queue}
            assert 1 in all_zones or 2 in all_zones

    def test_once_rule_removed_after_all_pending_run(self, mock_io, make_schedule):
        """repeat=False: Regel wird gelöscht wenn once_pending nach dem Lauf leer ist."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: once-Logik (Randfälle)
# ─────────────────────────────────────────────────────────────────────────────


class TestSchedulerOnceRules:
    def test_once_rule_skipped_when_combo_key_not_in_pending(self, mock_io, make_schedule):
        """
        repeat=False und combo_key passt nicht zu once_pending → Rule wird übersprungen,
        aber NICHT gelöscht (sie hat noch andere pending-Einträge).
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)  # Montag 06:00 → combo "0 06:00"
        rule = make_schedule(
            zone=1,
            weekdays=[0],
            start_times=["06:00"],
            repeat=False,
        )
        # Manuell auf eine andere combo setzen → aktueller Slot ist NICHT pending
        rule.once_pending = ["2 06:00"]  # Mittwoch statt Montag

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            # Zone wurde NICHT gestartet
            assert state.active_runs == {}
            # Regel ist noch vorhanden (sie hat noch pending-Einträge)
            assert len(state.schedules) == 1

    def test_once_rule_with_empty_once_pending_deleted_immediately(self, mock_io, make_schedule):
        """
        repeat=False, once_pending ist leer → Regel wird sofort gelöscht,
        kein Start.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(
            zone=1,
            weekdays=[0],
            start_times=["06:00"],
            repeat=False,
        )
        rule.once_pending = []  # Bereits leer → sofortige Löschung

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.active_runs == {}
            assert state.schedules == []

    def test_once_rule_with_none_once_pending_deleted_immediately(self, mock_io, make_schedule):
        """
        repeat=False, once_pending ist None → Regel wird sofort gelöscht,
        kein Start.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(
            zone=1,
            weekdays=[0],
            start_times=["06:00"],
            repeat=False,
        )
        rule.once_pending = None  # None → ebenfalls sofortige Löschung

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True

        _run_scheduler_once(now)

        with state_lock:
            assert state.schedules == []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Paused / Queue-State pausiert → Queue statt Direktstart
# ─────────────────────────────────────────────────────────────────────────────


class TestSchedulerQueueFallback:
    def test_paused_state_sends_job_to_queue(self, mock_io, make_schedule):
        """
        state.paused=True → kann kein neues Ventil starten → Job kommt in Queue.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.paused = True

        _run_scheduler_once(now)

        with state_lock:
            # Zone 1 muss in der Queue sein, NICHT in active_runs
            assert 1 not in state.active_runs
            assert any(item.zone == 1 for item in state.queue)

    def test_queue_state_pausiert_sends_job_to_queue(self, mock_io, make_schedule):
        """
        queue_state="pausiert" → Job kommt in Queue, nicht direkter Start.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.queue_state = "pausiert"

        _run_scheduler_once(now)

        with state_lock:
            assert 1 not in state.active_runs
            assert any(item.zone == 1 for item in state.queue)

    def test_busy_serial_mode_sends_job_to_queue(self, mock_io, make_schedule):
        """
        Im Seriell-Modus läuft bereits ein Ventil → neuer Job kommt in Queue.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=2, weekdays=[0], start_times=["06:00"], duration_s=60)

        set_running_zone(zone=1, duration_s=300)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.parallel_enabled = False

        _run_scheduler_once(now)

        with state_lock:
            assert any(item.zone == 2 for item in state.queue)

    def test_queue_state_set_to_lauft_when_job_queued(self, mock_io, make_schedule):
        """
        Wenn Job in Queue landet und queue_state "bereit" war, wird er auf "läuft" gesetzt.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        set_running_zone(zone=2, duration_s=300)  # Blockiert Direktstart (serial)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.parallel_enabled = False
            state.queue_state = "bereit"

        _run_scheduler_once(now)

        with state_lock:
            assert state.queue_state == "läuft"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Direktstart schlägt fehl → Job wandert vorne in die Queue
# ─────────────────────────────────────────────────────────────────────────────


class TestSchedulerPhase2Failure:
    def test_failed_start_inserts_job_at_front_of_queue(self, mock_io, make_schedule):
        """
        Wenn start_queue_item() in Phase 2 eine Exception wirft,
        muss der Job vorne in die Queue eingefügt werden – nicht verloren gehen.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.queue = []
            state.queue_state = "bereit"

        with patch("services.scheduler.start_queue_item", side_effect=Exception("HW down")):
            _run_scheduler_once(now)

        with state_lock:
            assert len(state.queue) >= 1
            assert state.queue[0].zone == 1

    def test_failed_start_sets_queue_state_lauft(self, mock_io, make_schedule):
        """
        Nach einem fehlgeschlagenen Direktstart muss queue_state auf "läuft" gesetzt werden,
        damit der Timer-Loop den Job aus der Queue übernehmen kann.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.queue = []
            state.queue_state = "bereit"

        with patch("services.scheduler.start_queue_item", side_effect=Exception("HW down")):
            _run_scheduler_once(now)

        with state_lock:
            assert state.queue_state == "läuft"

    def test_failed_start_job_inserted_before_existing_queue_items(self, mock_io, make_schedule):
        """
        Der fehlgeschlagene Job wird vorne in die Queue eingefügt (insert(0, ...)),
        nicht hinten angehängt. Er hat Vorrang.
        """
        now = datetime(2025, 1, 6, 6, 0, 0, tzinfo=TZ)
        rule = make_schedule(zone=1, weekdays=[0], start_times=["06:00"], duration_s=60)

        pre_existing = QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue")

        with state_lock:
            state.schedules = [rule]
            state.automation_enabled = True
            state.queue = [pre_existing]
            state.queue_state = "läuft"

        with patch("services.scheduler.start_queue_item", side_effect=Exception("HW down")):
            _run_scheduler_once(now)

        with state_lock:
            # Zone 1 muss an Index 0 stehen – vor Zone 3
            assert state.queue[0].zone == 1
            assert state.queue[1].zone == 3
