# ---------------------------
# ToDos:
# - Persistenz der Zeitpläne (Datei/DB) -> erledigt
# - Persistenz der Queue (Datei/DB) -> erledigt
# - GPIO Ansteuerung (Raspberry Pi) -> erledigt
# - Nach Stromausfall: alle Ventile stoppen, Zeitplan nachholen? -> erledigt (alle Ventile stoppen, Zeitplan wird nicht nachgeholt)
# - GPIO-Simulation für Tests auf Nicht-Raspberry Pi Systemen -> erledigt
# - GPIO-Errors behandeln (z.B. kein Zugriff auf /sys/class/...)
# - GPIO-Error-Logging
# - API Authentifizierung (Basic Auth / Token)?
# - Historie -> erledigt
# - Refactoring
# ---------------------------

from fastapi import FastAPI

from api import routes_history
from core.lifecycle import lifespan
from api.errors import register_error_handlers
from api.routes_health import router as health_router
from api.routes_queue import router as queue_router
from api.routes_schedule import router as schedule_router
from api.routes_control import router as control_router
from api.routes_history import router as history_router

app = FastAPI(lifespan=lifespan)

register_error_handlers(app)

app.include_router(health_router)
app.include_router(queue_router)
app.include_router(schedule_router)
app.include_router(control_router)
app.include_router(history_router)