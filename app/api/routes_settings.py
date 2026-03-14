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

    max_valves:         readonly, aus device_config.json. Damit kann das Frontend
                        bei ANZAHL_VENTILE != max_valves warnen.
    hard_max_runtime_s: readonly, aus device_config.json. Gibt dem Frontend die
                        absolute Obergrenze für den slider_max_minutes-Slider.
    """
    with state_lock:
        return {
            "max_history_items": int(getattr(state, "max_history_items", 20)),
            "navbar_title":       str(getattr(state, "navbar_title", "Bewaesserungscomputer")),
            "accent_color":       str(getattr(state, "accent_color", "#82372a")),
            "default_duration":   int(getattr(state, "default_duration", 5)),
            "default_time_unit":  str(getattr(state, "default_time_unit", "Minuten")),
            "slider_max_minutes": int(getattr(state, "slider_max_minutes", 60)),
            "max_valves":         int(getattr(state, "max_valves", 6)),        # readonly
            "valve_driver":       str(getattr(state, "valve_driver_mode", "?")),  # readonly
            "hard_max_runtime_s": int(getattr(state, "hard_max_runtime_s", 3600)),  # readonly
        }


@router.post("/settings")
@limiter.limit(MUTATION_LIMIT)
def update_settings(request: Request, req: SettingsUpdateRequest):
    """Aktualisiert User-Settings und persistiert sie sofort.

    Das Frontend schreibt niemals direkt in Dateien – ausschliesslich
    via Backend-API, damit Validierung, State-Lock und atomares Schreiben
    garantiert sind.

    slider_max_minutes wird zusätzlich zur Pydantic-Prüfung dynamisch gegen
    hard_max_runtime_s // 60 validiert. So ist garantiert, dass kein UI-Element
    eine Laufzeit oberhalb des Hardware-Limits einstellbar macht.
    """
    with state_lock:
        hard_max_min = int(getattr(state, "hard_max_runtime_s", 3600)) // 60

    clamped_slider_max = min(req.slider_max_minutes, hard_max_min)

    with state_lock:
        state.max_history_items = req.max_history_items
        state.navbar_title      = req.navbar_title
        state.accent_color      = req.accent_color
        state.default_duration  = req.default_duration
        state.default_time_unit = req.default_time_unit
        state.slider_max_minutes = clamped_slider_max

    save_user_settings_to_disk()

    log_event(
        "settings_updated",
        source="manual",
        max_history_items=req.max_history_items,
        navbar_title=req.navbar_title,
        accent_color=req.accent_color,
        default_duration=req.default_duration,
        default_time_unit=req.default_time_unit,
        slider_max_minutes=clamped_slider_max,
    )

    return {
        "ok":                True,
        "max_history_items": req.max_history_items,
        "navbar_title":      req.navbar_title,
        "accent_color":      req.accent_color,
        "default_duration":  req.default_duration,
        "default_time_unit": req.default_time_unit,
        "slider_max_minutes": clamped_slider_max,
    }
