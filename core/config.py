"""
================================================================================
core.config
================================================================================

Diese Datei enthält AUSSCHLIESSLICH Code-interne Defaults und Hard-Limits.

WICHTIG:
Die hier definierten Konstanten sind KEINE User- oder Admin-Settings.
Sie dienen ausschließlich als:

1) Fallback-Werte,
   wenn eine Konfigurationsdatei (device_config.json, user_settings.json,
   runtime_state.json) fehlt oder korrupt ist.

2) sichere Initialisierungswerte beim Start,
   bevor Konfigurationsdateien geladen werden.

3) Hard-Limits,
   um fehlerhafte oder manipulierte Konfigurationswerte zu begrenzen.

Die eigentliche Konfiguration kommt aus:

- device_config.json  → Hardware / GPIO / MAX_VALVES / Driver
- user_settings.json  → vom Benutzer änderbare Werte (z.B. MAX_HISTORY_ITEMS)
- runtime_state.json  → persistierter Laufzeit-Zustand

Override-Reihenfolge:
ENV > device_config.json > Defaults aus dieser Datei

Diese Datei darf NICHT zur Laufzeit verändert werden.
Sie ist Teil des Programmcodes und wird nur durch Software-Updates geändert.
"""

import os
from zoneinfo import ZoneInfo

MAX_VALVES = 6
MAX_RUNTIME_S = 60 * 60
MAX_HISTORY_ITEMS = 20

MAX_CONCURRENT_VALVES = 2
DEFAULT_PARALLEL_ENABLED = False

# Hardware-Failsafe / Retry-Policy (Code-Defaults, NICHT User-Settings)
HW_CLOSE_MAX_RETRIES = 5          # wie oft close() je Zone maximal versucht wird
HW_RETRY_BACKOFF_BASE_S = 1.0     # 1,2,4,8,... Sekunden
HW_RETRY_BACKOFF_MAX_S = 30.0     # Cap
HW_FAULT_COOLDOWN_S = 60.0        # nach Fault: frühestens nach X Sekunden wieder freigeben (operator ack)


TZ = ZoneInfo("Europe/Berlin")

# Du startest uvicorn im app/-Ordner -> __file__ ist app/core/config.py
APP_DIR = os.path.dirname(os.path.dirname(__file__))  # .../app
DATA_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")

SCHEDULES_FILE = os.path.join(DATA_DIR, "schedules.json")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

DEVICE_CONFIG_FILE = os.path.join(DATA_DIR, "device_config.json")
USER_SETTINGS_FILE = os.path.join(DATA_DIR, "user_settings.json")
RUNTIME_STATE_FILE = os.path.join(DATA_DIR, "runtime_state.json")

# Security: API-Key wird beim ersten Start generiert und hier abgelegt.
# Datei sollte Berechtigungen 600 haben (nur Owner lesbar).
# Niemals in git einchecken – siehe .gitignore.
API_KEY_FILE = os.path.join(DATA_DIR, "api_key.txt")

# ---------------------------------------------------------------------------
# CORS: Erlaubte Origins für Browser-seitige Cross-Origin-Requests.
#
# Komma-separierte Liste aus der Umgebungsvariable ALLOWED_ORIGINS.
# Leerzeichen um einzelne Origins werden automatisch entfernt.
#
# Default: http://localhost:8080 (Shiny-Express-Entwicklungsstandard).
#
# Produktions-Beispiel in .env oder systemd-Service:
#   ALLOWED_ORIGINS=http://192.168.1.100:8080,http://localhost:8080
#
# HINWEIS: Dieser Wert wird beim Modulimport einmalig aus der Umgebung
# gelesen. Änderungen an der Umgebungsvariable zur Laufzeit haben
# KEINEN Effekt – ein Neustart des Servers ist erforderlich.
# ---------------------------------------------------------------------------
_raw_allowed_origins: str = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080")
ALLOWED_ORIGINS: list[str] = [
    origin.strip()
    for origin in _raw_allowed_origins.split(",")
    if origin.strip()
]
