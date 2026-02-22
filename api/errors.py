# api/errors.py
"""
Zentrales Error-Handling für das Bewässerungs-Backend.

Registriert Exception-Handler für:
  - RateLimitExceeded  → 429 (mit Logging inkl. Client-IP)
  - HTTPException      → passender Status-Code (mit Logging für REJECT_LOG_STATUS_CODES inkl. Client-IP)
  - RequestValidationError → 422 (mit Sanitizing der Pydantic-v2-Fehlerstruktur inkl. Client-IP)
  - Exception          → 500 Internal Server Error (mit Logging)

Step 6 – Audit Logging mit Client-IP:
  Alle sicherheitsrelevanten Fehler-Events (401, 422, 429, 409, 404) enthalten
  jetzt das Feld client_ip. Die IP-Extraktion erfolgt über get_client_ip() aus
  core.security, die auch X-Forwarded-For für Proxy-Setups berücksichtigt.

Pydantic-v2-Besonderheit (RequestValidationError / @field_validator):
  Wenn ein @field_validator eine ValueError wirft, speichert Pydantic v2 das
  Exception-Objekt selbst in ctx["error"]. Dieses Objekt ist nicht JSON-serialisierbar.
  _sanitize_pydantic_errors() konvertiert ctx["error"] zu str(exc), damit JSONResponse
  die Fehlerstruktur sicher serialisieren kann.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded

from core.logging import log_event, logger
from core.security import get_client_ip

# Status-Codes, die als request_rejected geloggt werden.
# 429 wird separat über den RateLimitExceeded-Handler geloggt (rate_limit_exceeded).
REJECT_LOG_STATUS_CODES = {401, 404, 409, 429}


def _sanitize_pydantic_errors(errors: list) -> list:
    """
    Konvertiert Pydantic-v2-Fehlerstrukturen in JSON-serialisierbares Format.

    Pydantic v2 speichert in Fehlern aus @field_validator das rohe Exception-Objekt
    unter ctx["error"]. JSONResponse kann Exception-Objekte nicht serialisieren.

    Diese Funktion:
      - Kopiert alle Fehler-Dicts flach
      - Konvertiert ctx["error"] (falls Exception) zu str(exc)
      - Gibt sicher serialisierbare Dicts zurück

    Args:
        errors: Rückgabewert von RequestValidationError.errors()

    Returns:
        Liste von Dicts ohne nicht-serialisierbare Werte.
    """
    sanitized = []
    for error in errors:
        entry = dict(error)

        # ctx ist optional; wenn vorhanden, ctx["error"] ggf. zu String konvertieren
        if "ctx" in entry and isinstance(entry["ctx"], dict):
            ctx = dict(entry["ctx"])
            if "error" in ctx and isinstance(ctx["error"], Exception):
                ctx["error"] = str(ctx["error"])
            entry["ctx"] = ctx

        sanitized.append(entry)
    return sanitized


def register_error_handlers(app: FastAPI):
    # --- RateLimitExceeded (429) – muss vor dem generischen HTTPException-Handler
    #     registriert werden, damit der spezifischere Handler greift.
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
        log_event(
            "rate_limit_exceeded",
            level="warning",
            source="security",
            client_ip=get_client_ip(request),
            method=request.method,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too Many Requests – Rate Limit überschritten."},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code in REJECT_LOG_STATUS_CODES:
            log_event(
                "request_rejected",
                level="warning",
                source="manual",
                client_ip=get_client_ip(request),
                method=request.method,
                path=request.url.path,
                status_code=exc.status_code,
                detail=str(exc.detail),
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Pydantic v2: ctx["error"] kann ein Exception-Objekt enthalten → sanitizen.
        # include_url=False: entfernt Pydantic-Docs-URLs aus der Fehlerstruktur
        # (z.B. "url": "https://errors.pydantic.dev/...") – unnötige Info für Clients.
        try:
            raw_errors = exc.errors(include_url=False)
        except TypeError:
            # Ältere Pydantic-Versionen kennen include_url nicht → Fallback
            raw_errors = exc.errors()

        sanitized = _sanitize_pydantic_errors(raw_errors)

        log_event(
            "request_validation_error",
            level="warning",
            source="manual",
            client_ip=get_client_ip(request),
            method=request.method,
            path=request.url.path,
            status_code=422,
            error_count=len(sanitized),
        )
        return JSONResponse(status_code=422, content={"detail": sanitized})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("unhandled_exception")
        log_event(
            "internal_error",
            level="error",
            source="system",
            method=request.method,
            path=request.url.path,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
