# app_helpers.py
"""
Reine Hilfsfunktionen für das Bewässerungscomputer-Frontend.

Dieses Modul hat KEINE Shiny-Abhängigkeiten und kann daher direkt von
Unit-Tests importiert werden, ohne eine Shiny-Session zu benötigen.

Enthält:
  - Konfiguration laden (_load_frontend_config, _read_max_valves_from_device_config)
  - Formatierungsfunktionen (fmt_mmss, fmt_duration, fmt_weekdays)
  - HTTP-Hilfsfunktion (_json_or_none)
  - Konstanten (WEEKDAY_CHOICES)

app.py importiert alle diese Symbole von hier. Logik und UI sind damit sauber
getrennt – Logik ist testbar, UI-Code bleibt im Shiny-Kontext.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

WEEKDAY_CHOICES: dict[str, str] = {
    "0": "Mo", "1": "Di", "2": "Mi",
    "3": "Do", "4": "Fr", "5": "Sa", "6": "So",
}


# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------

def _load_frontend_config() -> dict:
    """Laedt frontend_config.json; bei Fehler: Fallback-Dict."""
    _defaults: dict[str, Any] = {
        "base_url":                "http://127.0.0.1:8000",
        "poll_status_s":           1,
        "poll_slow_s":             5,
        "backend_fail_threshold":  3,
        "health_timeout_s":        0.8,
        "anzahl_ventile_fallback": 6,
        "navbar_logo":             "",   # Dateiname im www/-Ordner, z.B. "logo.svg". Leer = kein Logo.
    }
    try:
        raw = Path("data/frontend_config.json").read_text(encoding="utf-8")
        data = _json.loads(raw)
        return {**_defaults, **{k: v for k, v in data.items() if not k.startswith("_")}}
    except Exception:
        return _defaults


def _read_max_valves_from_device_config(fallback: int) -> int:
    """Liest MAX_VALVES aus data/device_config.json (gleicher Pi).

    Frontend und Backend laufen auf derselben Maschine – direkter Dateizugriff
    ist die sauberste Loesung: kein API-Call beim Start, keine Race-Condition.
    """
    try:
        raw = Path("data/device_config.json").read_text(encoding="utf-8")
        cfg = _json.loads(raw)
        return max(1, int(cfg.get("device", {}).get("MAX_VALVES", fallback)))
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Formatierungsfunktionen
# ---------------------------------------------------------------------------

def fmt_mmss(total_s: int) -> str:
    """Formatiert Sekunden als 'M:SS'-String. Negative Werte werden auf 0 geclampt."""
    m, s = divmod(max(0, int(total_s)), 60)
    return f"{m}:{s:02d}"


def fmt_duration(duration_s: int, time_unit: str = "Sekunden") -> str:
    """Formatiert eine Dauer leserlich.

    Zeigt 'N Min' wenn time_unit=='Minuten' oder duration_s glatt durch 60 teilbar.
    Andernfalls 'N Sek'.
    """
    if time_unit == "Minuten" or duration_s % 60 == 0:
        return f"{duration_s // 60} Min"
    return f"{duration_s} Sek"


def fmt_weekdays(weekdays: list[int]) -> str:
    """Konvertiert eine Liste von Wochentags-Indizes (0=Mo..6=So) in einen
    kommaseparierten, sortierten String der deutschen Kurzbezeichnungen.
    Unbekannte Indizes werden als Zahl ausgegeben.
    """
    return ", ".join(WEEKDAY_CHOICES.get(str(w), str(w)) for w in sorted(weekdays))


# ---------------------------------------------------------------------------
# HTTP-Hilfsfunktion
# ---------------------------------------------------------------------------

def _json_or_none(r: requests.Response | None) -> dict | None:
    """Gibt den JSON-Body einer Response zurück oder None bei Fehler/None-Input."""
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        return None
