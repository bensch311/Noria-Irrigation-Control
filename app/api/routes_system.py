# api/routes_system.py
"""
System-Endpunkte: administrative Aktionen auf Systemebene.

Endpunkte:
  POST /system/ack-restart     – Neustart-Hinweis quittieren
  GET  /system/logs/download   – Alle Log-Dateien als ZIP herunterladen

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

Hintergrund (Log-Download):
  Der RotatingFileHandler erzeugt bis zu 11 Dateien:
    irrigation.jsonl          – aktuelle Log-Datei
    irrigation.jsonl.1 – .10 – rotierte Backup-Dateien (neueste = .1)

  GET /system/logs/download liest alle vorhandenen Dateien, zippt sie
  in-memory (kein temporäres File auf Disk) und liefert die ZIP als
  StreamingResponse. Der Dateiname enthält das aktuelle Datum.

  Der Zugriff wird geloggt (log_download_requested) inkl. Dateianzahl und
  ZIP-Gesamtgröße für Audit-Zwecke.
"""

import io
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from core.config import LOG_DIR, TZ
from core.state import state, state_lock
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT

# Log-Downloads sind I/O-intensiv (bis ~110 MB ZIP) – separates, engeres Limit.
_DOWNLOAD_LIMIT = "5/minute"

router = APIRouter(dependencies=[Depends(require_api_key)])

# Name der aktuellen Log-Datei (identisch mit core/logging.py)
_LOG_BASENAME = "irrigation.jsonl"


# ---------------------------------------------------------------------------
# POST /system/ack-restart
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# GET /system/logs/download
# ---------------------------------------------------------------------------

@router.get("/system/logs/download")
@limiter.limit(_DOWNLOAD_LIMIT)
def download_logs(request: Request):
    """Liefert alle vorhandenen Log-Dateien als ZIP-Archiv.

    Sammelt irrigation.jsonl sowie alle rotierten Backup-Dateien
    (irrigation.jsonl.1 bis .10) aus dem logs/-Verzeichnis, zippt sie
    in-memory und gibt sie als Download zurück.

    Dateiname im Response: noria-logs-YYYY-MM-DD.zip

    Enthält das Verzeichnis keine Log-Dateien (z.B. frisch installiertes
    System ohne ersten Lauf), wird eine leere ZIP zurückgegeben.

    Erfordert API-Key (X-API-Key Header).
    """
    log_dir = Path(LOG_DIR)

    # Alle vorhandenen Log-Dateien sammeln: aktuelle + rotierte Backups.
    # Sortierung: aktuelle Datei zuerst, dann .1, .2, ... (neueste zuerst).
    candidates = [log_dir / _LOG_BASENAME] + [
        log_dir / f"{_LOG_BASENAME}.{i}" for i in range(1, 11)
    ]
    log_files = [p for p in candidates if p.is_file()]

    # In-memory ZIP aufbauen – kein temporäres File auf Disk.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for log_path in log_files:
            # Im ZIP-Archiv nur den Dateinamen, kein Pfad-Prefix.
            zf.write(log_path, arcname=log_path.name)

    zip_bytes = buf.getvalue()
    zip_size  = len(zip_bytes)

    # Dateiname mit aktuellem Datum – erleichtert Archivierung beim Operator.
    today     = datetime.now(TZ).strftime("%Y-%m-%d")
    filename  = f"noria-logs-{today}.zip"

    log_event(
        "log_download_requested",
        source="operator",
        files_included=len(log_files),
        zip_size_bytes=zip_size,
        filename=filename,
    )

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
