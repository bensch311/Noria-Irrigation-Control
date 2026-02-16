import os
from zoneinfo import ZoneInfo

MAX_VALVES = 6
MAX_RUNTIME_S = 60 * 60
MAX_HISTORY_ITEMS = 20

MAX_CONCURRENT_VALVES = 2
DEFAULT_PARALLEL_ENABLED = False

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
