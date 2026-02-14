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
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

def log_event(event: str, level: str = "info", **fields):
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
