# app/api/routes_settings.py
"""
User-Settings-Routen.

GET  /settings  – liest aktuelle User-Settings aus dem State
POST /settings  – validiert, schreibt in State + persistiert sofort

Abgrenzung:
  user_settings.json  → User-aenderbar, wird hier verwaltet
  device_config.json  → Admin-only (Hardware/GPIO), read-only im Betrieb
  runtime_state.json  → Laufzeit-Toggles (parallel_enabled etc.)

Warum sofortiges Persistieren statt dirty-Flag?
  Settings-Aenderungen sind selten aber nutzerkritisch.
  Der persistence_loop laeuft alle 2s – wuerde ein Stromausfall
  genau dazwischen treffen, ginge die Aenderung verloren.
  save_user_settings_to_disk() schreibt atomar (tmp + os.replace).
"""

from fastapi import APIRouter, Depends, Request

from core.state import state, state_lock
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT
from models.requests import SettingsUpdateRequest
from services.persistence import save_user_settings_to_disk

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/settings")
def get_settings():
    """Gibt aktuelle User-Settings zurueck.

    max_valves: readonly, kommt aus device_config.json. Damit kann das
    Frontend bei ANZAHL_VENTILE != max_valves warnen.
    """
    with state_lock:
        return {
            "max_history_items": int(getattr(state, "max_history_items", 20)),
            "navbar_title":       str(getattr(state, "navbar_title", "Bewaesserungscomputer")),
            "accent_color":       str(getattr(state, "accent_color", "#82372a")),
            "default_duration":   int(getattr(state, "default_duration", 5)),
            "default_time_unit":  str(getattr(state, "default_time_unit", "Minuten")),
            "max_valves":         int(getattr(state, "max_valves", 6)),  # readonly
        }


@router.post("/settings")
@limiter.limit(MUTATION_LIMIT)
def update_settings(request: Request, req: SettingsUpdateRequest):
    """Aktualisiert User-Settings und persistiert sie sofort.

    Das Frontend schreibt niemals direkt in Dateien – ausschliesslich
    via Backend-API, damit Validierung, State-Lock und atomares Schreiben
    garantiert sind.
    """
    with state_lock:
        state.max_history_items = req.max_history_items
        state.navbar_title      = req.navbar_title
        state.accent_color      = req.accent_color
        state.default_duration  = req.default_duration
        state.default_time_unit = req.default_time_unit

    save_user_settings_to_disk()

    log_event(
        "settings_updated",
        source="manual",
        max_history_items=req.max_history_items,
        navbar_title=req.navbar_title,
        accent_color=req.accent_color,
        default_duration=req.default_duration,
        default_time_unit=req.default_time_unit,
    )

    return {
        "ok":               True,
        "max_history_items": req.max_history_items,
        "navbar_title":      req.navbar_title,
        "accent_color":      req.accent_color,
        "default_duration":  req.default_duration,
        "default_time_unit": req.default_time_unit,
    }
