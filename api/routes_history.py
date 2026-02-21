# app/api/routes_history.py
from fastapi import APIRouter, Depends

from core.state import state, state_lock
from core.security import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])

@router.get("/history")
def get_history():
    with state_lock:
        hist = state.run_history or []
        return {
            "count": len(hist),
            "items": [
                {
                    "ts_end": h.ts_end,
                    "zone": h.zone,
                    "duration_s": h.duration_s,
                    "source": h.source,
                    "time_unit": getattr(h, "time_unit", "Sekunden"),
                }
                for h in hist
            ],
        }
