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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Fügt zu jeder HTTP-Response Security-Header hinzu und ersetzt den
    informativen 'Server'-Header durch einen generischen Wert.

    Alle Header werden nach der eigentlichen Route-/Middleware-Verarbeitung
    gesetzt, sodass sie auf jede Response – auch auf Fehler- und
    CORS-Preflight-Antworten – angewendet werden.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        for header_name, header_value in _SECURITY_HEADERS.items():
            response.headers[header_name] = header_value

        # Server-Header neutralisieren: vorhandenen Wert überschreiben.
        response.headers["Server"] = _OPAQUE_SERVER_HEADER

        return response
