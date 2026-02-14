from fastapi import APIRouter
from datetime import datetime

from core.state import state, state_lock
from core.config import TZ

router = APIRouter()

@router.get("/health")
def health():
    with state_lock:
        running = sorted(list((state.active_runs or {}).keys()))
        qlen = len(state.queue or [])
        return {
            "ok": True,
            "service": "irrigation",
            "version": 1,
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "running_zones": running,
            "queue_length": qlen,
            "parallel_enabled": bool(getattr(state, "parallel_enabled", False)),
            "max_concurrent_valves": int(getattr(state, "max_concurrent_valves", 1)),
        }
