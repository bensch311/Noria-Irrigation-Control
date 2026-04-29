# tests/test_zone_pause.py
"""
Tests für die Zonen-Pause-Funktion (zone_pause_s / zone_pause_slot_expires).

Design: Slot-basiertes Modell
  Jede fertig gewordene Zone fügt unabhängig einen Eintrag in
  state.zone_pause_slot_expires ein (Expires = now + zone_pause_s).
  Pausing-Slots belegen Kapazität wie laufende Zonen:
    effective_running = laufend + geplant + pausing_slots
  So kann jeder Parallel-Slot seine eigene Pause machen, auch bei
  unterschiedlichen Laufzeiten.

Getestet werden:
  - timer_loop: Slot-Eintrag nach Zonen-Ende (Queue wartet, zone_pause_s > 0)
  - timer_loop: Kein Slot wenn Queue leer oder zone_pause_s == 0
  - timer_loop: Im Parallelmodus unabhängige Slots pro Zone
  - timer_loop: Pausing-Slots blockieren Queue-Fill (seriell + parallel)
  - timer_loop: Abgelaufene Slots werden bereinigt
  - timer_loop: Kein Slot wenn queue_state != 'läuft'
  - /stop:      zone_pause_slot_expires wird geleert (empty + running)
  - /queue/clear: zone_pause_slot_expires wird geleert
  - GET /status: zone_pause_s + zone_pause_remaining_s korrekt
  - GET /settings: zone_pause_s erscheint
  - POST /settings: Validierung (0–3600), State + Persistenz
  - Persistenz: ZONE_PAUSE_S Roundtrip + Clamping + fehlender Key
"""

import time
from unittest.mock import patch

import pytest

from core.state import state, state_lock, ActiveRun, QueueItem
from services.io_worker import IOResult, IOCommand


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _make_elapsed_run(zone: int, duration_s: int = 60, source: str = "queue") -> ActiveRun:
    """ActiveRun dessen end_time bereits in der Vergangenheit liegt."""
    now = time.monotonic()
    return ActiveRun(
        zone=zone,
        end_time=now - 1.0,
        time_unit="Sekunden",
        started_at=now - duration_s,
        started_source=source,
        started_planned_s=duration_s,
    )


def _make_active_run(zone: int, duration_s: int = 60) -> ActiveRun:
    """ActiveRun der noch läuft (end_time in der Zukunft)."""
    now = time.monotonic()
    return ActiveRun(
        zone=zone,
        end_time=now + duration_s,
        time_unit="Sekunden",
        started_at=now,
        started_source="queue",
        started_planned_s=duration_s,
    )


def _run_timer_once() -> None:
    """Führt exakt einen vollständigen Durchlauf des timer_loop aus."""
    from core.state import shutdown_event
    from services.timer import timer_loop

    call_count = 0

    def mock_is_set() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 1

    with (
        patch.object(shutdown_event, "is_set", side_effect=mock_is_set),
        patch.object(shutdown_event, "wait", return_value=False),
    ):
        timer_loop()


# ─────────────────────────────────────────────────────────────────────────────
# timer_loop: Slot wird korrekt gesetzt
# ─────────────────────────────────────────────────────────────────────────────

class TestTimerZonePauseSlotSet:

    def test_slot_added_after_zone_finishes_with_queue_waiting(self, mock_io):
        """zone_pause_slot_expires bekommt einen Eintrag wenn Zone endet und Queue wartet."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = []
            state.active_runs = {1: _make_elapsed_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        before = time.monotonic()
        _run_timer_once()

        with state_lock:
            # Item wurde noch nicht gestartet (Pause aktiv), aber ein Slot wurde erzeugt
            slots = state.zone_pause_slot_expires
            assert len(slots) == 1, "Genau ein Pause-Slot erwartet"
            assert slots[0] > before + 290, \
                f"Slot-Expiry muss ~300 s in der Zukunft liegen, ist {slots[0] - before:.1f} s"

    def test_slot_log_event_emitted(self, mock_io):
        """zone_pause_started wird geloggt wenn ein Slot gesetzt wird."""
        logged = []

        import services.timer as timer_mod
        original_log = timer_mod.log_event

        def capture(event, **kwargs):
            logged.append(event)
            return original_log(event, **kwargs)

        with state_lock:
            state.zone_pause_s = 120
            state.zone_pause_slot_expires = []
            state.active_runs = {1: _make_elapsed_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        with patch.object(timer_mod, "log_event", side_effect=capture):
            _run_timer_once()

        assert "zone_pause_started" in logged

    def test_no_slot_when_queue_empty(self, mock_io):
        """Kein Slot wenn Queue nach Zone-Ende leer ist."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = []
            state.active_runs = {1: _make_elapsed_run(1)}
            state.queue = []
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            assert state.zone_pause_slot_expires == [], \
                "Kein Slot wenn Queue leer"

    def test_no_slot_when_zone_pause_s_zero(self, mock_io):
        """Kein Slot wenn zone_pause_s == 0."""
        with state_lock:
            state.zone_pause_s = 0
            state.zone_pause_slot_expires = []
            state.active_runs = {1: _make_elapsed_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            assert state.zone_pause_slot_expires == [], \
                "Kein Slot wenn zone_pause_s == 0"

    def test_no_slot_when_queue_not_running(self, mock_io):
        """Kein Slot wenn queue_state != 'läuft'."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = []
            state.active_runs = {1: _make_elapsed_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "pausiert"

        _run_timer_once()

        with state_lock:
            assert state.zone_pause_slot_expires == [], \
                "Kein Slot wenn Queue pausiert"


# ─────────────────────────────────────────────────────────────────────────────
# timer_loop: Parallel-Modus – unabhängige Slots pro Zone
# ─────────────────────────────────────────────────────────────────────────────

class TestTimerZonePauseParallel:

    def test_two_zones_finish_together_create_two_slots(self, mock_io):
        """Wenn beide parallele Zonen gleichzeitig enden, entstehen 2 Pause-Slots."""
        with state_lock:
            state.zone_pause_s = 180
            state.zone_pause_slot_expires = []
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {
                1: _make_elapsed_run(1),
                2: _make_elapsed_run(2),
            }
            state.queue = [
                QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=4, duration=60, time_unit="Sekunden", source="queue"),
            ]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            # 2 Zonen enden → 2 Slots → beide Slot-Kapazitäten belegt
            assert len(state.zone_pause_slot_expires) == 2, \
                "Zwei parallele Zonen müssen zwei unabhängige Pause-Slots erzeugen"
            # Queue-Items dürfen noch nicht gestartet sein
            assert len(state.queue) == 2, \
                "Keine Items sollen während der Pause gestartet werden"
            assert state.active_runs == {}, \
                "Beide Zonen müssen beendet sein"

    def test_first_of_two_zones_finishes_creates_one_slot(self, mock_io):
        """Zone 2 (5 min) endet während Zone 1 (10 min) noch läuft → 1 Slot.

        Kernfall: unterschiedliche Laufzeiten im Parallelmodus.
        Zone 1 läuft noch → active_runs nicht leer → trotzdem Slot für Zone 2.
        """
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = []
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {
                1: _make_active_run(1, duration_s=600),   # noch 10 min
                2: _make_elapsed_run(2, duration_s=300),  # abgelaufen
            }
            state.queue = [
                QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=4, duration=60, time_unit="Sekunden", source="queue"),
            ]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            # Zone 2 beendet → 1 Pause-Slot
            assert len(state.zone_pause_slot_expires) == 1, \
                "Zone 2 muss ihren eigenen Pause-Slot erzeugen"
            # Zone 1 läuft noch → 1 laufende Zone + 1 pausing_slot = 2 = max_conc
            # → kein Queue-Item darf starten
            assert len(state.queue) == 2, \
                "Kein Item darf starten wenn Kapazität voll (1 running + 1 pausing)"
            # Zone 1 muss noch laufen
            assert 1 in state.active_runs, "Zone 1 muss noch laufen"
            # Zone 2 muss entfernt sein
            assert 2 not in state.active_runs, "Zone 2 muss beendet sein"

    def test_pausing_slot_expires_allows_next_zone_to_start(self, mock_io):
        """Nach Ablauf des Pause-Slots kann das nächste Item in den Slot starten."""
        with state_lock:
            state.zone_pause_s = 300
            # Slot bereits abgelaufen (in der Vergangenheit)
            state.zone_pause_slot_expires = [time.monotonic() - 1.0]
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {
                1: _make_active_run(1, duration_s=600),  # Zone 1 läuft noch
            }
            state.queue = [
                QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
            ]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            # Slot abgelaufen → Zone 3 darf in Slot 2 starten
            # active_runs: Zone 1 (läuft) + Zone 3 (neu gestartet)
            assert 3 in state.active_runs, \
                "Nach Ablauf des Pause-Slots muss Zone 3 starten können"

    def test_serial_mode_pausing_slot_blocks_next_start(self, mock_io):
        """Seriell-Modus: 1 pausing_slot blockiert jeden weiteren Start."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = [time.monotonic() + 300.0]  # Slot aktiv
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            assert state.active_runs == {}, \
                "Kein Start wenn pausing_slot aktiv (seriell)"
            assert len(state.queue) == 1, "Item muss in der Queue bleiben"

    def test_serial_mode_expired_slot_allows_start(self, mock_io):
        """Seriell-Modus: abgelaufener Slot gibt den nächsten Start frei."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = [time.monotonic() - 1.0]  # abgelaufen
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            assert 2 in state.active_runs, \
                "Nach Ablauf des Slots muss Zone 2 starten"

    def test_expired_slots_are_cleaned_up(self, mock_io):
        """Abgelaufene Slots werden aus zone_pause_slot_expires entfernt."""
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = [
                time.monotonic() - 2.0,   # abgelaufen
                time.monotonic() - 1.0,   # abgelaufen
                time.monotonic() + 200.0, # noch aktiv
            ]
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        _run_timer_once()

        with state_lock:
            # Nur der noch aktive Slot bleibt (+ ggf. neuer durch Zone 1-Start)
            # Wichtig: Die 2 abgelaufenen Einträge müssen weg sein
            expired = [t for t in state.zone_pause_slot_expires if t < time.monotonic()]
            assert expired == [], "Abgelaufene Slots müssen bereinigt werden"


# ─────────────────────────────────────────────────────────────────────────────
# /stop und /queue/clear: zone_pause_slot_expires zurücksetzen
# ─────────────────────────────────────────────────────────────────────────────

class TestZonePauseReset:

    def test_stop_clears_slots_when_no_zones_running(self, client):
        """POST /stop löscht zone_pause_slot_expires auch wenn active_runs leer."""
        with state_lock:
            state.zone_pause_slot_expires = [time.monotonic() + 999.0]
            state.active_runs = {}

        client.post("/stop")

        with state_lock:
            assert state.zone_pause_slot_expires == []

    def test_stop_clears_slots_with_running_zone(self, client, mock_io):
        """POST /stop löscht zone_pause_slot_expires auch wenn Zonen laufen."""
        with state_lock:
            state.zone_pause_slot_expires = [
                time.monotonic() + 100.0,
                time.monotonic() + 200.0,
            ]
            state.active_runs = {
                1: ActiveRun(
                    zone=1,
                    end_time=time.monotonic() + 60.0,
                    time_unit="Sekunden",
                    started_at=time.monotonic(),
                    started_source="queue",
                    started_planned_s=60,
                )
            }

        client.post("/stop")

        with state_lock:
            assert state.zone_pause_slot_expires == []

    def test_queue_clear_clears_slots(self, client):
        """POST /queue/clear löscht zone_pause_slot_expires."""
        with state_lock:
            state.zone_pause_slot_expires = [
                time.monotonic() + 100.0,
                time.monotonic() + 200.0,
            ]
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
            state.queue_state = "läuft"

        resp = client.post("/queue/clear")
        assert resp.status_code == 200

        with state_lock:
            assert state.zone_pause_slot_expires == []

    def test_queue_clear_also_clears_queue_items(self, client):
        """POST /queue/clear leert Queue UND Pause-Slots."""
        with state_lock:
            state.zone_pause_slot_expires = [time.monotonic() + 300.0]
            state.queue = [
                QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
            ]
            state.queue_state = "läuft"

        resp = client.post("/queue/clear")
        assert resp.status_code == 200

        with state_lock:
            assert state.queue == []
            assert state.zone_pause_slot_expires == []


# ─────────────────────────────────────────────────────────────────────────────
# GET /status: zone_pause_s und zone_pause_remaining_s
# ─────────────────────────────────────────────────────────────────────────────

class TestStatusZonePauseFields:

    def test_status_contains_zone_pause_s(self, client):
        """GET /status enthält zone_pause_s."""
        with state_lock:
            state.zone_pause_s = 120
        data = client.get("/status").json()
        assert "zone_pause_s" in data
        assert data["zone_pause_s"] == 120

    def test_status_zone_pause_remaining_zero_when_no_slots(self, client):
        """zone_pause_remaining_s ist 0 wenn keine Pause-Slots aktiv."""
        with state_lock:
            state.zone_pause_s = 60
            state.zone_pause_slot_expires = []
        data = client.get("/status").json()
        assert data["zone_pause_remaining_s"] == 0

    def test_status_zone_pause_remaining_max_of_active_slots(self, client):
        """zone_pause_remaining_s ist das Maximum aller aktiven Slot-Restzeiten."""
        now = time.monotonic()
        with state_lock:
            state.zone_pause_s = 300
            state.zone_pause_slot_expires = [now + 100.0, now + 250.0]

        data = client.get("/status").json()
        assert data["zone_pause_remaining_s"] > 0
        assert data["zone_pause_remaining_s"] <= 250

    def test_status_zone_pause_remaining_zero_when_all_slots_expired(self, client):
        """zone_pause_remaining_s ist 0 wenn alle Slots abgelaufen sind."""
        with state_lock:
            state.zone_pause_s = 60
            state.zone_pause_slot_expires = [
                time.monotonic() - 2.0,
                time.monotonic() - 1.0,
            ]

        data = client.get("/status").json()
        assert data["zone_pause_remaining_s"] == 0

    def test_status_zone_pause_fields_present_when_zones_running(self, client):
        """Felder sind auch vorhanden wenn Ventile laufen (zweiter Status-Branch)."""
        with state_lock:
            state.zone_pause_s = 90
            state.zone_pause_slot_expires = []
            state.active_runs = {
                1: ActiveRun(
                    zone=1,
                    end_time=time.monotonic() + 60.0,
                    time_unit="Sekunden",
                    started_at=time.monotonic(),
                    started_source="queue",
                    started_planned_s=60,
                )
            }

        data = client.get("/status").json()
        assert "zone_pause_s" in data
        assert "zone_pause_remaining_s" in data
        assert data["zone_pause_s"] == 90


# ─────────────────────────────────────────────────────────────────────────────
# GET/POST /settings: zone_pause_s
# ─────────────────────────────────────────────────────────────────────────────

class TestSettingsZonePause:

    def _full_payload(self, **overrides) -> dict:
        base = {
            "max_history_items": 20,
            "navbar_title":      "Bewaesserungscomputer",
            "accent_color":      "#82372a",
            "default_duration":  5,
            "default_time_unit": "Minuten",
            "slider_max_minutes": 60,
            "zone_pause_s":      0,
        }
        base.update(overrides)
        return base

    def test_get_settings_contains_zone_pause_s(self, client):
        with state_lock:
            state.zone_pause_s = 180
        data = client.get("/settings").json()
        assert "zone_pause_s" in data
        assert data["zone_pause_s"] == 180

    def test_post_settings_sets_zone_pause_s_in_state(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk"):
            resp = client.post("/settings", json=self._full_payload(zone_pause_s=600))
        assert resp.status_code == 200
        with state_lock:
            assert state.zone_pause_s == 600

    def test_post_settings_returns_zone_pause_s(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk"):
            resp = client.post("/settings", json=self._full_payload(zone_pause_s=300))
        assert resp.status_code == 200
        assert resp.json()["zone_pause_s"] == 300

    def test_post_settings_zone_pause_s_zero_accepted(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk"):
            resp = client.post("/settings", json=self._full_payload(zone_pause_s=0))
        assert resp.status_code == 200
        assert resp.json()["zone_pause_s"] == 0

    def test_post_settings_zone_pause_s_max_accepted(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk"):
            resp = client.post("/settings", json=self._full_payload(zone_pause_s=3600))
        assert resp.status_code == 200
        assert resp.json()["zone_pause_s"] == 3600

    def test_post_settings_zone_pause_s_negative_rejected(self, client):
        resp = client.post("/settings", json=self._full_payload(zone_pause_s=-1))
        assert resp.status_code == 422

    def test_post_settings_zone_pause_s_over_max_rejected(self, client):
        resp = client.post("/settings", json=self._full_payload(zone_pause_s=3601))
        assert resp.status_code == 422

    def test_post_settings_zone_pause_s_calls_persist(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk") as mock_save:
            client.post("/settings", json=self._full_payload(zone_pause_s=120))
        mock_save.assert_called_once()

    def test_post_settings_roundtrip(self, client):
        with patch("api.routes_settings.save_user_settings_to_disk"):
            client.post("/settings", json=self._full_payload(zone_pause_s=900))
        data = client.get("/settings").json()
        assert data["zone_pause_s"] == 900

    def test_post_settings_zone_pause_s_default_zero_when_omitted(self, client):
        payload = {k: v for k, v in self._full_payload().items() if k != "zone_pause_s"}
        with patch("api.routes_settings.save_user_settings_to_disk"):
            resp = client.post("/settings", json=payload)
        assert resp.status_code == 200
        assert resp.json()["zone_pause_s"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Persistenz: ZONE_PAUSE_S Roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestZonePausePersistence:

    def test_save_load_user_settings_zone_pause_roundtrip(self, tmp_path, monkeypatch):
        import services.persistence as pers
        monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(tmp_path / "user_settings.json"))

        with state_lock:
            state.zone_pause_s = 450

        pers.save_user_settings_to_disk()

        with state_lock:
            state.zone_pause_s = 0

        pers.load_user_settings_from_disk()

        with state_lock:
            assert state.zone_pause_s == 450

    def test_load_user_settings_zone_pause_missing_uses_default(self, tmp_path, monkeypatch):
        import json
        import services.persistence as pers
        from core.config import DEFAULT_ZONE_PAUSE_S

        settings_file = tmp_path / "user_settings.json"
        settings_file.write_text(json.dumps({
            "version": 1,
            "user": {"MAX_HISTORY_ITEMS": 20},
        }), encoding="utf-8")

        monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))

        with state_lock:
            state.zone_pause_s = 999

        pers.load_user_settings_from_disk()

        with state_lock:
            assert state.zone_pause_s == DEFAULT_ZONE_PAUSE_S

    def test_load_user_settings_zone_pause_clamped_to_max(self, tmp_path, monkeypatch):
        import json
        import services.persistence as pers

        settings_file = tmp_path / "user_settings.json"
        settings_file.write_text(json.dumps({
            "version": 1,
            "user": {"MAX_HISTORY_ITEMS": 20, "ZONE_PAUSE_S": 9999},
        }), encoding="utf-8")

        monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))
        pers.load_user_settings_from_disk()

        with state_lock:
            assert state.zone_pause_s == 3600

    def test_load_user_settings_zone_pause_negative_clamped_to_zero(self, tmp_path, monkeypatch):
        import json
        import services.persistence as pers

        settings_file = tmp_path / "user_settings.json"
        settings_file.write_text(json.dumps({
            "version": 1,
            "user": {"MAX_HISTORY_ITEMS": 20, "ZONE_PAUSE_S": -100},
        }), encoding="utf-8")

        monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(settings_file))
        pers.load_user_settings_from_disk()

        with state_lock:
            assert state.zone_pause_s == 0

    def test_saved_json_contains_zone_pause_s_key(self, tmp_path, monkeypatch):
        import json
        import services.persistence as pers

        monkeypatch.setattr(pers, "USER_SETTINGS_FILE", str(tmp_path / "user_settings.json"))

        with state_lock:
            state.zone_pause_s = 240

        pers.save_user_settings_to_disk()

        raw = json.loads((tmp_path / "user_settings.json").read_text(encoding="utf-8"))
        assert raw["user"]["ZONE_PAUSE_S"] == 240
