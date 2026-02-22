# ---------------------------
# ToDos:
# - Persistenz der Zeitpläne (Datei/DB) -> erledigt
# - Persistenz der Queue (Datei/DB) -> erledigt
# - GPIO Ansteuerung (Raspberry Pi) -> erledigt
# - Nach Stromausfall: alle Ventile stoppen, Zeitplan nachholen? -> erledigt (alle Ventile stoppen, Zeitplan wird nicht nachgeholt)
# - GPIO-Simulation für Tests auf Nicht-Raspberry Pi Systemen -> erledigt
# - GPIO-Errors behandeln (z.B. kein Zugriff auf /sys/class/...)
# - GPIO-Error-Logging
# - API Authentifizierung (Basic Auth / Token)? -> erledigt (Step 1)
# - Rate Limiting -> erledigt (Step 2)
# - CORS -> erledigt (Step 3)
# - Historie -> erledigt
# - Software / Hardware Watchdog (Raspberry PI)
# - Refactoring
# ---------------------------

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from api import routes_history
from core.lifecycle import lifespan
from core.limiter import limiter
from core.config import ALLOWED_ORIGINS
from api.errors import register_error_handlers
from api.routes_health import router as health_router
from api.routes_queue import router as queue_router
from api.routes_schedule import router as schedule_router
from api.routes_control import router as control_router
from api.routes_history import router as history_router

app = FastAPI(lifespan=lifespan)

# Rate Limiting: Limiter-Instanz in app.state setzen (wird von SlowAPIMiddleware genutzt).
app.state.limiter = limiter

# ---------------------------------------------------------------------------
# Middleware-Stack (Reihenfolge beachten!)
#
# In Starlette/FastAPI gilt: das ZULETZT hinzugefügte Middleware ist OUTERMOST
# und verarbeitet eingehende Requests als ERSTES.
#
# Gewünschte Request-Reihenfolge:
#   Browser → CORSMiddleware → SlowAPIMiddleware → Route-Handler
#
# Damit gilt:
#   1. CORSMiddleware antwortet OPTIONS-Preflight-Requests direkt, BEVOR
#      der Rate-Limiter sie zählen würde (kein sinnloser 429 auf Preflight).
#   2. Reguläre Requests passieren zuerst CORS, dann Rate-Limiting.
#
# Daher: SlowAPIMiddleware ZUERST hinzufügen (innermost),
#         CORSMiddleware ZULETZT hinzufügen (outermost).
# ---------------------------------------------------------------------------

# SlowAPIMiddleware VOR register_error_handlers hinzufügen, damit der
# RateLimitExceeded-Handler aus errors.py greift.
app.add_middleware(SlowAPIMiddleware)

# CORSMiddleware als äußerste Schicht: verarbeitet alle Requests zuerst.
# allow_credentials=False: kein Cookie-basiertes Auth, kein CSRF-Risiko.
# allow_methods/allow_headers: minimale Whitelist – nur was die API tatsächlich nutzt.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

register_error_handlers(app)

app.include_router(health_router)
app.include_router(queue_router)
app.include_router(schedule_router)
app.include_router(control_router)
app.include_router(history_router)
