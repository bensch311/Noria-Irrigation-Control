from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded

from core.logging import log_event, logger

# Status-Codes, die als request_rejected geloggt werden.
# 429 wird separat über den RateLimitExceeded-Handler geloggt (rate_limit_exceeded).
REJECT_LOG_STATUS_CODES = {401, 404, 409, 429}


def register_error_handlers(app: FastAPI):
    # --- RateLimitExceeded (429) – muss vor dem generischen HTTPException-Handler
    #     registriert werden, damit der spezifischere Handler greift.
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
        client_ip = request.client.host if request.client else "unknown"
        log_event(
            "rate_limit_exceeded",
            level="warning",
            source="security",
            client_ip=client_ip,
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
                method=request.method,
                path=request.url.path,
                status_code=exc.status_code,
                detail=str(exc.detail),
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        log_event(
            "request_validation_error",
            level="warning",
            source="manual",
            method=request.method,
            path=request.url.path,
            status_code=422,
            errors=exc.errors(),
        )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

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
