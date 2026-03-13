# app_helpers.py
"""
Reine Hilfsfunktionen für das Bewässerungscomputer-Frontend.

Dieses Modul hat KEINE Shiny-Abhängigkeiten und kann daher direkt von
Unit-Tests importiert werden, ohne eine Shiny-Session zu benötigen.

Enthält:
  - Konfiguration laden (_load_frontend_config, _read_max_valves_from_device_config)
  - Formatierungsfunktionen (fmt_mmss, fmt_duration, fmt_weekdays,
                              fmt_uptime, fmt_disk, fmt_signal)
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
        raw = (Path(__file__).parent / "data" / "frontend_config.json").read_text(encoding="utf-8")
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
        raw = (Path(__file__).parent / "data" / "device_config.json").read_text(encoding="utf-8")
        cfg = _json.loads(raw)
        return max(1, int(cfg.get("device", {}).get("MAX_VALVES", fallback)))
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Formatierungsfunktionen – Zeit und Dauer
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


def fmt_uptime(seconds: float | int) -> str:
    """Formatiert Uptime-Sekunden als lesbaren String.

    Beispiele:
      fmt_uptime(45)      → "0 Min"
      fmt_uptime(90)      → "1 Min"
      fmt_uptime(3600)    → "1 Std"
      fmt_uptime(86400)   → "1 Tag"
      fmt_uptime(90061)   → "1 Tag 1 Std 1 Min"
      fmt_uptime(172800)  → "2 Tage"
    """
    total_s = max(0, int(seconds))
    days,    remainder = divmod(total_s, 86400)
    hours,   remainder = divmod(remainder, 3600)
    minutes, _         = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} {'Tag' if days == 1 else 'Tage'}")
    if hours:
        parts.append(f"{hours} Std")
    if minutes or not parts:
        parts.append(f"{minutes} Min")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Formatierungsfunktionen – Systemmetriken
# ---------------------------------------------------------------------------

def fmt_disk(free_gb: float | None, total_gb: float | None,
             used_pct: float | None) -> str:
    """Formatiert Disk-Nutzung als lesbaren String.

    Beispiele:
      fmt_disk(12.3, 29.8, 58.7) → "12,3 GB frei / 29,8 GB (59 %)"
      fmt_disk(None, None, None) → "–"
    """
    if free_gb is None or total_gb is None or used_pct is None:
        return "–"
    return f"{free_gb:,.1f} GB frei / {total_gb:,.1f} GB ({used_pct:.0f} %)".replace(",", "\u202f")


def fmt_memory(used_mb: int | None, total_mb: int | None,
               used_pct: float | None) -> str:
    """Formatiert RAM-Nutzung als lesbaren String.

    Beispiele:
      fmt_memory(312, 1024, 30.5) → "312 MB / 1024 MB (31 %)"
      fmt_memory(None, None, None) → "–"
    """
    if used_mb is None or total_mb is None or used_pct is None:
        return "–"
    return f"{used_mb} MB / {total_mb} MB ({used_pct:.0f} %)"


def fmt_signal(signal_pct: int | None) -> str:
    """Formatiert WLAN-Signalstärke als lesbaren String mit Qualitätsstufe.

    Qualitätsstufen:
      0–33 %   → Schwach
      34–66 %  → Mittel
      67–100 % → Gut

    Beispiele:
      fmt_signal(75)  → "Gut (75 %)"
      fmt_signal(20)  → "Schwach (20 %)"
      fmt_signal(None) → "–"
    """
    if signal_pct is None:
        return "–"
    if signal_pct >= 67:
        label = "Gut"
    elif signal_pct >= 34:
        label = "Mittel"
    else:
        label = "Schwach"
    return f"{label} ({signal_pct} %)"


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
