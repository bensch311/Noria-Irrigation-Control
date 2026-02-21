# core/security.py
"""
API Key Authentication für das Bewässerungs-Backend.

Design:
- Beim ersten Start wird ein kryptografisch sicherer 256-bit-Key (64 Hex-Zeichen)
  generiert und atomar in data/api_key.txt gespeichert.
- Bei jedem Folgestart wird der Key aus der Datei geladen und validiert.
- Der Key wird in einer Modulvariable gehalten (nie im RunState / auf der Disk via
  persistence.py), damit er nicht versehentlich serialisiert wird.
- Alle Routen außer GET /health prüfen den Header X-API-Key via FastAPI-Dependency.
- Fehlversuche werden mit Client-IP geloggt (Event: auth_failure).

Konstante Zeitvergleich via secrets.compare_digest() verhindert Timing-Angriffe.
"""

import os
import secrets

from fastapi import Header, HTTPException, Request

from core.config import API_KEY_FILE
from core.logging import log_event, logger

# ---------------------------------------------------------------------------
# Modulvariable – wird einmal beim Startup gesetzt, danach read-only
# ---------------------------------------------------------------------------
_api_key: str = ""

_KEY_LENGTH = 64  # Zeichen; entspricht 256 bit (secrets.token_hex(32))


def _is_valid_key_format(key: str) -> bool:
    """Prüft, ob ein Key das erwartete Format hat (64 Hex-Zeichen)."""
    return (
        isinstance(key, str)
        and len(key) == _KEY_LENGTH
        and all(c in "0123456789abcdef" for c in key)
    )


def load_or_create_api_key() -> str:
    """
    Lädt den API-Key von Disk oder erstellt einen neuen.

    Muss genau einmal beim Startup aufgerufen werden (core/lifecycle.py).
    Setzt die globale Modulvariable _api_key.

    Returns:
        Den aktiven API-Key (64 Hex-Zeichen).
    """
    global _api_key

    # --- Versuche, bestehenden Key zu laden ---
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()

            if _is_valid_key_format(key):
                _api_key = key
                log_event("api_key_loaded", source="system")
                return _api_key

            # Key vorhanden, aber ungültiges Format → neu generieren
            log_event(
                "api_key_invalid_format",
                level="warning",
                source="system",
                detail="Key hat nicht das erwartete Format (64 Hex-Zeichen). Neuer Key wird generiert.",
            )

        except (OSError, UnicodeDecodeError):
            logger.exception("api_key: Datei konnte nicht gelesen werden")
            log_event(
                "api_key_read_error",
                level="error",
                source="system",
                detail="Lesefehler – neuer Key wird generiert.",
            )

    # --- Neuen Key generieren ---
    new_key = secrets.token_hex(32)  # 32 Bytes = 64 Hex-Zeichen = 256 bit

    try:
        os.makedirs(os.path.dirname(API_KEY_FILE), exist_ok=True)
        tmp_path = API_KEY_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_key)
        os.replace(tmp_path, API_KEY_FILE)  # atomares Ersetzen
        log_event("api_key_generated", source="system")
    except OSError:
        logger.exception("api_key: Datei konnte nicht geschrieben werden")
        log_event(
            "api_key_write_error",
            level="error",
            source="system",
            detail="Schreibfehler – Key nur im Speicher, NICHT persistent!",
        )

    _api_key = new_key
    return _api_key


def get_api_key() -> str:
    """Gibt den aktuell aktiven API-Key zurück (read-only, nur für Tests / Info)."""
    return _api_key


# ---------------------------------------------------------------------------
# FastAPI Dependency
# ---------------------------------------------------------------------------

async def require_api_key(
    request: Request,
    x_api_key: str = Header(default=""),
) -> None:
    """
    FastAPI-Dependency: Validiert den X-API-Key-Header.

    Wirft HTTPException 401, wenn:
    - kein Key gesendet wurde,
    - der Key nicht mit dem gespeicherten Key übereinstimmt,
    - der gespeicherte Key nicht initialisiert ist (Konfigurationsfehler).

    Verwendung:
        router = APIRouter(dependencies=[Depends(require_api_key)])

    GET /health ist explizit ausgenommen (kein Auth nötig für Monitoring).
    """
    if not _api_key:
        # Sollte in der Produktion nie eintreten (lifecycle ruft load_or_create_api_key auf).
        # Schutz für den Fall eines Fehlstarts.
        logger.error("require_api_key: _api_key nicht initialisiert!")
        raise HTTPException(status_code=500, detail="API key not initialized")

    # Konstante Zeitvergleich verhindert Timing-Angriffe
    key_ok = bool(x_api_key) and secrets.compare_digest(x_api_key, _api_key)

    if not key_ok:
        client_ip = request.client.host if request.client else "unknown"
        log_event(
            "auth_failure",
            level="warning",
            source="security",
            client_ip=client_ip,
            method=request.method,
            path=str(request.url.path),
            key_present=bool(x_api_key),
        )
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "ApiKey"},
        )
