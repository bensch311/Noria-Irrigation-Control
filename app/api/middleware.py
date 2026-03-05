# api/middleware.py
"""
SecurityHeadersMiddleware – Step 4 des Security-Plans.

Ziel: Informationsleckage und typische Web-Angriffe (Clickjacking etc.) verhindern.

Wird als OUTERMOST Middleware eingebunden (letztes add_middleware in main.py),
damit die Header auf ALLE Responses gesetzt werden:
  - Normale 2xx-Antworten
  - Fehler-Responses (4xx, 5xx) aus Error-Handlern
  - CORS-Preflight-Responses (OPTIONS) aus CORSMiddleware

Request-/Response-Durchfluss (äußerste → innerste Schicht):
  Client → SecurityHeadersMiddleware → CORSMiddleware → SlowAPIMiddleware → Route-Handler
  Response: Route-Handler → SlowAPIMiddleware → CORSMiddleware → SecurityHeadersMiddleware → Client

Technische Wahl: BaseHTTPMiddleware
  Für diese API (reine JSON-Responses, kein Streaming) ist BaseHTTPMiddleware
  die sauberste und lesbarste Lösung. Die bekannten Streaming-Limitierungen
  von BaseHTTPMiddleware sind in diesem Kontext nicht relevant.

Docs-Ausnahme (CSP):
  /docs, /redoc und /openapi.json benötigen eine weniger restriktive CSP, da
  Swagger UI / ReDoc Assets (JS, CSS) von https://cdn.jsdelivr.net laden.
  Alle anderen Pfade behalten die maximale Restriktion (default-src 'none').
  Im Produktionsbetrieb sind diese Pfade via ENABLE_DOCS ohnehin deaktiviert,
  sodass die Ausnahme nur im Entwicklungsbetrieb aktiv ist.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Security-Header-Definitionen
#
# Alle Werte sind bewusst restriktiv gewählt, da dieses Backend ausschließlich
# JSON liefert und keinerlei HTML, Skripte oder Medien ausliefert.
# ---------------------------------------------------------------------------

_SECURITY_HEADERS: dict[str, str] = {
    # Verhindert MIME-Type-Sniffing (z.B. JSON wird nicht als HTML interpretiert).
    "X-Content-Type-Options": "nosniff",

    # Verhindert Einbettung in Frames/iFrames → kein Clickjacking möglich.
    "X-Frame-Options": "DENY",

    # Legacy-XSS-Filter deaktivieren.
    # OWASP-Empfehlung: "0" setzen, da der Filter selbst XSS-Lücken in älteren
    # Browsern einführen kann. Moderne Browser ignorieren diesen Header ohnehin.
    "X-XSS-Protection": "0",

    # Kein Referer-Header bei ausgehenden Requests – schützt URL-Pfade.
    "Referrer-Policy": "no-referrer",

    # Maximale CSP-Restriktion für eine reine JSON-API:
    # Kein Script, kein Style, kein Frame, kein Bild – nichts darf geladen werden.
    "Content-Security-Policy": "default-src 'none'",
}

# Generischer Server-Bezeichner, der die konkrete Server-Software verschleiert.
# uvicorn setzt standardmäßig "server: uvicorn" – das verrät die verwendete
# Software und Version, was Angreifern gezielte Exploits erleichtert.
_OPAQUE_SERVER_HEADER = "webserver"

# ---------------------------------------------------------------------------
# Docs-spezifische CSP
#
# Swagger UI (FastAPI /docs) und ReDoc (/redoc) laden JS + CSS von
# https://cdn.jsdelivr.net. Ohne diese Ausnahme blockiert der Browser
# alle Assets still und zeigt eine leere weiße Seite.
#
# Minimal notwendige Direktiven:
#   script-src  – SwaggerUI-Bundle JS vom CDN
#   style-src   – SwaggerUI CSS + inline Styles (SwaggerUI nutzt style-Attribute)
#   img-src     – data: URIs (Inline-Icons), CDN-Favicon von fastapi.tiangolo.com
#   worker-src  – blob: (SwaggerUI nutzt Web Worker für Syntax-Highlighting)
#
# Alle anderen Direktiven erben 'none' aus default-src.
# Im Produktionsbetrieb sind /docs und /redoc via ENABLE_DOCS deaktiviert,
# sodass diese CSP-Ausnahme dort nie greift.
# ---------------------------------------------------------------------------
_DOCS_CSP = (
    "default-src 'none'; "
    "script-src https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src data: https://fastapi.tiangolo.com; "
    "worker-src blob:; "
    "connect-src 'self' https://cdn.jsdelivr.net"
)

# Pfade, die die Docs-CSP erhalten statt der maximalen Restriktion.
_DOCS_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", "/openapi.json"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Fügt zu jeder HTTP-Response Security-Header hinzu und ersetzt den
    informativen 'Server'-Header durch einen generischen Wert.

    Alle Header werden nach der eigentlichen Route-/Middleware-Verarbeitung
    gesetzt, sodass sie auf jede Response – auch auf Fehler- und
    CORS-Preflight-Antworten – angewendet werden.

    Für Docs-Pfade (/docs, /redoc, /openapi.json) wird eine dedizierte CSP
    gesetzt, die CDN-Assets für Swagger UI / ReDoc erlaubt.
    Im Produktionsbetrieb sind diese Pfade deaktiviert (ENABLE_DOCS nicht gesetzt).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        for header_name, header_value in _SECURITY_HEADERS.items():
            response.headers[header_name] = header_value

        # Docs-Pfade: CSP-Ausnahme für Swagger UI / ReDoc CDN-Assets.
        if request.url.path in _DOCS_PATHS:
            response.headers["Content-Security-Policy"] = _DOCS_CSP

        # Server-Header neutralisieren: vorhandenen Wert überschreiben.
        response.headers["Server"] = _OPAQUE_SERVER_HEADER

        return response
