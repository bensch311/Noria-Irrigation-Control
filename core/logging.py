# core/logging.py
"""
Strukturiertes JSON-Logging für den Bewässerungscomputer.

Alle Events werden als einzelne JSON-Zeilen (JSONL) in logs/irrigation.jsonl
geschrieben. Das Format erlaubt einfache maschinelle Auswertung (z.B. mit
`jq`, `grep`, oder einem Log-Aggregator wie Loki).

Log-Rotation:
  RotatingFileHandler – max. 10 MB pro Datei, 10 Backup-Dateien behalten.
  Älteste Dateien werden automatisch überschrieben (kein Disk-Overflow).

Haupt-Interface:
  log_event(event, level, **fields) → strukturierter JSON-Eintrag
  logger                            → Standard-Python-Logger (für logger.exception etc.)

Jedes Event enthält immer:
  ts       – ISO-8601-Zeitstempel (Europe/Berlin)
  event    – Event-Name (z.B. "valve_start", "auth_failure")
  level    – "info" | "warning" | "error"
  event_id – 8-stellige UUID-Kurzform (für Korrelation in Logs)

Bei level="error" wird automatisch der aktuelle Traceback (letzte 15 Zeilen)
als `traceback`-Array angehängt, wenn ein aktiver Exception-Kontext vorhanden ist.
"""

import os
import json
import uuid
import logging
import traceback
from logging.handlers import RotatingFileHandler
from datetime import datetime

from core.config import LOG_DIR, TZ

os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "irrigation.jsonl")

logger = logging.getLogger("irrigation")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB pro Datei
        backupCount=10,              # 10 Backup-Dateien = max. ~110 MB Gesamtgröße
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def log_event(event: str, level: str = "info", **fields):
    """Schreibt ein strukturiertes Event als JSON-Zeile ins Log.

    Args:
        event:   Event-Name (snake_case, z.B. "valve_start", "auth_failure")
        level:   Log-Level – "info" | "warning" | "error"
        **fields: Beliebige zusätzliche Key-Value-Paare die ins JSON-Objekt
                  aufgenommen werden (z.B. zone=3, duration_s=120, source="manual")

    Automatisch hinzugefügte Felder:
        ts       – aktueller Zeitstempel (ISO-8601, Europe/Berlin)
        event_id – 8-stellige UUID-Kurzform zur Event-Korrelation
        traceback – nur bei level="error" und aktivem Exception-Kontext

    Beispiel:
        log_event("valve_start", source="manual", zone=2, duration_s=60)
        → {"ts": "2025-03-05T09:00:00+01:00", "event": "valve_start",
           "level": "info", "event_id": "a1b2c3d4",
           "source": "manual", "zone": 2, "duration_s": 60}
    """
    payload = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "event": event,
        "level": level,
        "event_id": str(uuid.uuid4())[:8],
        **fields,
    }

    if level == "error":
        tb = traceback.format_exc()
        if tb and tb != "NoneType: None\n":
            payload["traceback"] = tb.splitlines()[-15:]

    line = json.dumps(payload, ensure_ascii=False)

    if level == "error":
        logger.error(line)
    elif level == "warning":
        logger.warning(line)
    else:
        logger.info(line)
