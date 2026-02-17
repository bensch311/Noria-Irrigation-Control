from core.state import state, state_lock
from services.engine import _can_start_new_valve_locked

def test_hw_fault_blocks_new_starts():
    with state_lock:
        state.hw_faulted = True
        state.active_runs = {}
    with state_lock:
        assert _can_start_new_valve_locked() is False
    with state_lock:
        state.hw_faulted = False
