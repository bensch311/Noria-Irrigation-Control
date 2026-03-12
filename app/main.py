# app/main.py
"""
FastAPI-Anwendung: Einstiegspunkt und Middleware-Stack.

Dieses Modul instanziiert die FastAPI-App und konfiguriert:
  - Rate-Limiting (SlowAPI)
  - CORS (CORSMiddleware)
  - Security-Header (SecurityHeadersMiddleware)
  - Exception-Handler (errors.py)
  - Alle API-Router (health, system, queue, schedule, control, history, settings)

Der Lifecycle (Startup/Shutdown) wird vollständig in core/lifecycle.py verwaltet.
Die App wird von uvicorn gestartet, typischerweise via systemd-Service.

Swagger UI / ReDoc:
  Standardmäßig DEAKTIVIERT (docs_url=None, redoc_url=None).
  Aktivierung nur für Entwicklung via Umgebungsvariable ENABLE_DOCS=true.
  Im Produktionsbetrieb darf ENABLE_DOCS nicht gesetzt sein – die Docs-Endpunkte
  würden sonst die vollständige API-Struktur für alle Netzwerkteilnehmer
  ohne zusätzliche Authentifizierung sichtbar machen.

"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from version import APP_NAME, __version__
from api.middleware import SecurityHeadersMiddleware
from core.lifecycle import lifespan
from core.limiter import limiter
from core.config import ALLOWED_ORIGINS
from api.errors import register_error_handlers
from api.routes_health import router as health_router
from api.routes_system import router as system_router
from api.routes_queue import router as queue_router
from api.routes_schedule import router as schedule_router
from api.routes_control import router as control_router
from api.routes_history import router as history_router
from api.routes_settings import router as settings_router

# ---------------------------------------------------------------------------
# Swagger UI / ReDoc – nur in Entwicklung aktivieren.
#
# ENABLE_DOCS=true  → /docs und /redoc werden von FastAPI bereitgestellt.
# Nicht gesetzt     → docs_url=None, redoc_url=None: FastAPI registriert
#                     die Pfade gar nicht → 404, noch vor jeder Middleware.
# ---------------------------------------------------------------------------
_enable_docs = os.getenv("ENABLE_DOCS", "false").lower() == "true"
_docs_url    = "/docs"  if _enable_docs else None
_redoc_url   = "/redoc" if _enable_docs else None

app = FastAPI(
    title=APP_NAME,
    version=__version__,
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
)

# Rate Limiting: Limiter-Instanz in app.state setzen (wird von SlowAPIMiddleware genutzt).
app.state.limiter = limiter

# ---------------------------------------------------------------------------
# Middleware-Stack (Reihenfolge beachten!)
#
# In Starlette/FastAPI gilt: das ZULETZT hinzugefügte Middleware ist OUTERMOST
# und verarbeitet eingehende Requests als ERSTES (und ausgehende Responses
# als LETZTES, direkt vor Auslieferung an den Client).
#
# Gewünschte Request-Reihenfolge:
#   Client → SecurityHeadersMiddleware → CORSMiddleware → SlowAPIMiddleware → Route-Handler
#
# Damit gilt:
#   1. SecurityHeadersMiddleware setzt Security-Header auf ALLE Responses
#      (inkl. CORS-Preflight-Antworten), da es die äußerste Schicht ist.
#   2. CORSMiddleware antwortet OPTIONS-Preflight-Requests direkt, BEVOR
#      der Rate-Limiter sie zählen würde (kein sinnloser 429 auf Preflight).
#   3. Reguläre Requests passieren zuerst Security-Headers, dann CORS,
#      dann Rate-Limiting.
#
# Daher: SlowAPIMiddleware ZUERST hinzufügen (innermost),
#         CORSMiddleware als zweites (middle),
#         SecurityHeadersMiddleware ZULETZT hinzufügen (outermost).
# ---------------------------------------------------------------------------

# SlowAPIMiddleware VOR register_error_handlers hinzufügen, damit der
# RateLimitExceeded-Handler aus errors.py greift.
app.add_middleware(SlowAPIMiddleware)

# CORSMiddleware als mittlere Schicht: verarbeitet alle Requests nach SecurityHeaders.
# allow_credentials=False: kein Cookie-basiertes Auth, kein CSRF-Risiko.
# allow_methods/allow_headers: minimale Whitelist – nur was die API tatsächlich nutzt.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# SecurityHeadersMiddleware als äußerste Schicht: setzt Security-Header auf
# jede Response – inklusive CORS-Preflights und Fehlerantworten.
app.add_middleware(SecurityHeadersMiddleware)

register_error_handlers(app)

app.include_router(health_router)
app.include_router(system_router)
app.include_router(queue_router)
app.include_router(schedule_router)
app.include_router(control_router)
app.include_router(history_router)
app.include_router(settings_router)
