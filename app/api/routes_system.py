# api/routes_system.py
"""
System-Endpunkte: administrative Aktionen auf Systemebene.

Endpunkte:
  POST /system/ack-restart  – Neustart-Hinweis quittieren

Alle Endpunkte erfordern API-Key-Authentifizierung (X-API-Key Header).

Hintergrund (Neustart-Erkennung):
  Das Backend legt beim Start eine Sentinel-Datei (running.lock) an und löscht
  sie beim sauberen Shutdown als allererstes. Existiert die Datei beim nächsten
  Start noch, wurde der letzte Shutdown nicht sauber durchgeführt (Stromausfall,
  SIGKILL, OOM-Kill). In diesem Fall setzt lifecycle.py state.unclean_restart=True
  und state.restart_detected_at auf den Erkennungszeitstempel.

  Das Frontend erkennt unclean_restart=True im /health-Response und zeigt
  einmalig ein Modal an. Nach Bestätigung durch den Bediener ruft das Frontend
  POST /system/ack-restart auf, was das Flag zurücksetzt und das Modal schließt.

  Das Flag ist rein in-memory: beim nächsten Start wird es erneut korrekt gesetzt,
  abhängig davon ob running.lock existiert oder nicht.
"""

from fastapi import APIRouter, Depends, Request

from core.state import state, state_lock
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/system/ack-restart")
@limiter.limit(MUTATION_LIMIT)
def ack_restart(request: Request):
    """Quittiert einen unclean Restart.

    Setzt state.unclean_restart=False und state.restart_detected_at="" zurück,
    damit das Frontend-Modal geschlossen wird und beim nächsten Poll nicht
    erneut erscheint.

    Idempotent: mehrfache Aufrufe sind harmlos (keine 409-Logik nötig).

    Erfordert API-Key (X-API-Key Header).
    """
    with state_lock:
        was_set = bool(state.unclean_restart)
        state.unclean_restart = False
        state.restart_detected_at = ""

    if was_set:
        log_event(
            "unclean_restart_acknowledged",
            source="operator",
        )

    return {"ok": True}
