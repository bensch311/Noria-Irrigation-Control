# app.py - Noria - Irrigation Control Frontend
# Shiny Express | FastAPI Backend: http://127.0.0.1:8000
#
# Rate-Limit-Strategie:
#   - Alle shared @reactive.calc (status, automation, parallel) werden
#     auf Seitenebene EINMAL pro Poll-Tick definiert.
#   - Shiny cached @reactive.calc-Ergebnisse innerhalb eines reaktiven
#     Ticks: Egal wie viele Renders darauf zugreifen, es entsteht nur
#     EIN HTTP-Request pro Intervall.
#   - Kein Render darf _get("/status") direkt aufrufen - immer _status_data().

from __future__ import annotations

import datetime
import json as _json_mod
import re
from pathlib import Path
from typing import Any

import requests
from shiny import reactive
from shiny.express import input, output, render, ui
from faicons import icon_svg as icon

# --- Konfiguration -----------------------------------------------------------
# Wird einmalig beim Start aus data/frontend_config.json geladen.
# Faellt die Datei weg: sichere Fallback-Werte greifen, kein Crash.
# ANZAHL_VENTILE liest MAX_VALVES aus data/device_config.json –
# Single Source of Truth, kein manuelles Doppelpflegen.

from version import __version__
from app_helpers import (
    WEEKDAY_CHOICES as _WEEKDAY_CHOICES_IMPORT,
    _load_frontend_config,
    _read_max_valves_from_device_config,
    fmt_mmss,
    fmt_duration,
    fmt_weekdays,
    fmt_uptime,
    fmt_disk,
    fmt_memory,
    fmt_signal,
    _json_or_none,
)

_cfg = _load_frontend_config()

BASE_URL               = str(_cfg["base_url"])
POLL_STATUS_S          = int(_cfg["poll_status_s"])
POLL_SLOW_S            = int(_cfg["poll_slow_s"])
BACKEND_FAIL_THRESHOLD = int(_cfg["backend_fail_threshold"])
HEALTH_TIMEOUT_S       = float(_cfg["health_timeout_s"])

# Navbar-Logo: nur setzen wenn Dateiname konfiguriert UND Datei im www/-Ordner
# vorhanden ist. Shiny serviert www/ als statischen Root → src="logo.svg" reicht.
# Leerer String oder fehlende Datei → kein Logo, kein Crash.
_logo_filename = str(_cfg.get("navbar_logo", "")).strip()
NAVBAR_LOGO_PATH: str = (
    _logo_filename
    if _logo_filename and (Path(__file__).parent / "www" / _logo_filename).is_file()
    else ""
)

# Single Source of Truth: device_config.json → MAX_VALVES.
# Aenderungen erfordern Neustart des Frontends (MAX_VALVES ist Hardware-Konfig).
ANZAHL_VENTILE = _read_max_valves_from_device_config(_cfg["anzahl_ventile_fallback"])

# WEEKDAY_CHOICES: importiert aus app_helpers (zusammen mit fmt_weekdays etc.)
WEEKDAY_CHOICES = _WEEKDAY_CHOICES_IMPORT

# --- API-Key -----------------------------------------------------------------

_API_KEY_PATH = Path(__file__).parent / "data" / "api_key.txt"

_api_key = reactive.Value("")          # session-scoped durch Shiny Express
_auth_ok = reactive.Value(True)
_auth_modal_open = reactive.Value(False)


def _load_api_key_from_disk() -> str:
    try:
        key = _API_KEY_PATH.read_text(encoding="utf-8").strip()
        return key
    except OSError:
        return ""


def _apply_api_key_to_session(key: str) -> None:
    # immer explizit setzen (auch wenn leer), damit es keine Altwerte gibt
    _session.headers["X-API-Key"] = key


_session = requests.Session()

# initial load (fail-closed)
_initial_key = _load_api_key_from_disk()
_=_api_key.set(_initial_key)
_apply_api_key_to_session(_initial_key)

if not _initial_key:
    # Kein Key -> UI wird "locked" (kein stiller Betrieb ohne Auth)
    _auth_ok.set(False)

# --- HTTP-Hilfsfunktionen ----------------------------------------------------

def _get(path: str, timeout: float = 2.0) -> requests.Response | None:
    try:
        return _wrap_auth(_session.get(BASE_URL + path, timeout=timeout))
    except Exception:
        return None

def _post(path: str, json: Any = None, timeout: float = 3.0) -> requests.Response | None:
    try:
        return _wrap_auth(_session.post(BASE_URL + path, json=json, timeout=timeout))
    except Exception:
        return None

def _delete(path: str, json: Any = None, timeout: float = 3.0) -> requests.Response | None:
    try:
        return _wrap_auth(_session.delete(BASE_URL + path, json=json, timeout=timeout))
    except Exception:
        return None


# --- Auth Handling -----------------------------------------------------------

def _show_auth_modal(reason: str):
    if _auth_modal_open.get():
        return
    _auth_modal_open.set(True)
    ui.modal_show(
        ui.modal(
            ui.tags.div(
                ui.tags.p(ui.tags.strong("API-Key Problem!"), class_="text-danger"),
                ui.tags.p(reason),
                ui.tags.p(f"Key-Datei: {_API_KEY_PATH}", class_="font-monospace text-muted small"),
                ui.tags.p("Aktion: Key neu laden (z.B. falls Backend einen neuen Key erzeugt hat)."),
            ),
            title="Authentifizierung",
            easy_close=False,
            footer=ui.div(
                ui.input_action_button("btn_reload_key", "Key neu laden", class_="btn btn-primary me-2"),
                ui.modal_button("OK", class_="btn btn-secondary"),
            ),
        )
    )


def _auth_fail(reason: str):
    _auth_ok.set(False)
    _show_auth_modal(reason)


def _auth_recover():
    _auth_ok.set(True)
    if _auth_modal_open.get():
        _auth_modal_open.set(False)
        ui.modal_remove()


def _wrap_auth(r: requests.Response | None) -> requests.Response | None:
    # None => Netzwerk/Timeout; das wird über Backend-offline behandelt
    if r is None:
        return None
    if r.status_code == 401:
        _auth_fail("Backend antwortet mit 401 Unauthorized (Key fehlt, falsch oder veraltet).")
    return r

# --- Formatierungshilfsfunktionen --------------------------------------------

# fmt_mmss, fmt_duration, fmt_weekdays: importiert aus app_helpers

def state_badge(state_str: str) -> ui.Tag:
    label_map = {
        "laeuft":   ("success",   "Laeuft"),
        "pausiert": ("warning",   "Pausiert"),
        "bereit":   ("secondary", "Bereit"),
        "fertig":   ("info",      "Fertig"),
    }
    key = (state_str
           .replace("\u00e4", "ae")
           .replace("\u00f6", "oe")
           .replace("\u00fc", "ue"))
    color, label = label_map.get(key, ("secondary", state_str))
    return ui.span(label, class_=f"badge text-bg-{color} app-badge")

# --- Reaktive Werte (modul-global, session-scoped durch Shiny Express) -------

_backend_fail_streak = reactive.Value(0)
_backend_ok          = reactive.Value(True)
_backend_modal_open  = reactive.Value(False)

# Manuelle Trigger fuer sofortige UI-Aktualisierung nach Button-Klicks
_status_trigger   = reactive.Value(0)
_queue_trigger    = reactive.Value(0)
_schedule_trigger = reactive.Value(0)
_history_trigger  = reactive.Value(0)
_sensor_trigger   = reactive.Value(0)

def _bump_status():   _status_trigger.set(_status_trigger.get() + 1)
def _bump_queue():    _queue_trigger.set(_queue_trigger.get() + 1)
def _bump_schedule(): _schedule_trigger.set(_schedule_trigger.get() + 1)
def _bump_history():  _history_trigger.set(_history_trigger.get() + 1)
def _bump_sensor():   _sensor_trigger.set(_sensor_trigger.get() + 1)

# --- Backend-Health ----------------------------------------------------------

def _ping_health() -> tuple[bool, dict]:
    """Fragt /health ab. Gibt (is_ok, health_data) zurück.

    is_ok:       True wenn Backend erreichbar und ok=True
    health_data: vollständiger Response-Body, {} bei Fehler oder HTTP != 200

    Ein einzelner Request pro Poll-Zyklus – wird von _health_poll() ausgewertet
    um gleichzeitig Backend-Erreichbarkeit UND Neustart-Erkennung zu prüfen.
    """
    try:
        r = _session.get(BASE_URL + "/health", timeout=HEALTH_TIMEOUT_S)
        if r.status_code != 200:
            return False, {}
        data = r.json()
        return bool(data.get("ok", False)), data
    except Exception:
        return False, {}

def _show_backend_modal():
    if _backend_modal_open.get():
        return
    _backend_modal_open.set(True)
    ui.modal_show(
        ui.modal(
            ui.tags.div(
                ui.tags.p(ui.tags.strong("Das Backend ist nicht erreichbar!"),
                          class_="text-danger"),
                ui.tags.p("Bitte pruefen ob main.py laeuft und die URL korrekt ist."),
                ui.tags.p(f"URL: {BASE_URL}", class_="font-monospace text-muted small"),
            ),
            title="Verbindungsfehler",
            easy_close=False,
            # Kein Footer / kein OK-Button: Das Modal schliesst sich automatisch
            # sobald _health_poll eine erfolgreiche Verbindung meldet.
            # Ein OK-Button waere irrefuehrend, weil er das Modal nur client-
            # seitig schliessen wuerde und _health_poll es sofort wieder oeffnen.
            footer=None,
        )
    )


def _show_restart_modal(health_data: dict):
    """Zeigt das Neustart-Erkennungs-Modal einmalig an.

    Wird von _health_poll() aufgerufen wenn unclean_restart=True im ersten
    erfolgreichen /health-Response erkannt wird.
    Der Bediener bestätigt mit 'Verstanden' → _h_ack_restart() → ACK an Backend.
    """
    detected_at = health_data.get("restart_detected_at", "")
    ui.modal_show(
        ui.modal(
            ui.div(
                ui.tags.p(
                    "Noria wurde unerwartet neu gestartet "
                    "(z.\u00a0B. durch einen Stromausfall oder Systemabsturz). "
                    "Laufende Bewaesserungen wurden dabei unterbrochen.",
                ),
                ui.tags.p(
                    "Alle Ventile wurden beim Neustart automatisch geschlossen. "
                    "Bitte pruefen Sie, ob alle Zonen korrekt bewaessert wurden "
                    "und starten Sie ausstehende Bewässerungen ggf. manuell.",
                    class_="text-muted small",
                ),
                *(
                    [ui.tags.p(
                        ui.tags.b("Erkannt: "),
                        detected_at.replace("T", " ")[:19],
                        class_="text-muted small font-monospace",
                    )]
                    if detected_at else []
                ),
            ),
            title=ui.div(
                ui.tags.i(class_="bi bi-lightning-fill me-2", style="color:#f59e0b;"),
                "Unerwarteter Neustart erkannt",
            ),
            footer=ui.div(
                ui.input_action_button(
                    "btn_ack_restart",
                    ui.div(
                        ui.tags.i(class_="bi bi-check2 me-1"),
                        "Verstanden",
                    ),
                    class_="btn btn-primary",
                ),
            ),
            easy_close=False,
            size="m",
        )
    )


# =============================================================================
# CSS (DESIGN-ONLY)
# =============================================================================

ui.tags.head(
    ui.tags.link(rel="preconnect", href="https://fonts.googleapis.com"),
    ui.tags.link(rel="preconnect", href="https://fonts.gstatic.com", crossorigin="anonymous"),
    ui.tags.link(
        rel="stylesheet",
        href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap"),

    # Bootstrap Icons (für Zahnrad-Icon im Navbar-Tab)
    ui.tags.link(
        rel="stylesheet",
        href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css",
    ),
)

# Initiale Defaults – werden nach dem ersten _settings_data()-Poll durch
# die reaktiven Renderer überschrieben.
ACCENT_COLOR_DEFAULT = "#b8902a"
NAVBAR_TITLE_DEFAULT = "Noria - Irrigation Control"
# Hardcoded Prefix der immer in der Navbar und im Browser-Tab erscheint.
# Der User konfiguriert nur den Teil dahinter (den "Suffix").
NAVBAR_PREFIX = "Noria - "

# Initiales CSS mit Fallback-Farbe (sofort beim Laden aktiv)
ui.tags.style(f":root {{ --accent: {ACCENT_COLOR_DEFAULT}; }}")
ui.include_css(Path(__file__).parent / "www" / "app.css")

# =============================================================================
# SEITE
# =============================================================================

_ = ui.page_opts(title="", window_title="Bewaesserung", lang="de")

# -------------------------------------------------------------------------
# GETEILTE REACTIVE CALCS - Seitenebene
# -------------------------------------------------------------------------

@reactive.calc
def _status_data() -> dict:
    reactive.invalidate_later(POLL_STATUS_S)
    _status_trigger.get()
    r = _get("/status")
    if r is None or not r.ok:
        return {}
    return r.json()

@reactive.calc
def _automation_data() -> dict:
    reactive.invalidate_later(POLL_SLOW_S)
    _status_trigger.get()
    return _json_or_none(_get("/automation")) or {}

@reactive.calc
def _parallel_data() -> dict:
    reactive.invalidate_later(POLL_SLOW_S)
    _status_trigger.get()
    return _json_or_none(_get("/parallel")) or {}

@reactive.calc
def _settings_data() -> dict:
    """Gecachter /settings-Fetch. Langsamer Poll genuegt."""
    reactive.invalidate_later(POLL_SLOW_S)
    _status_trigger.get()
    return _json_or_none(_get("/settings")) or {}

@reactive.calc
def _sysinfo_data() -> dict:
    """Gecachter /system/info-Fetch. Langsamer Poll genuegt.

    Liefert OS-Metriken (Disk, RAM, Uptime, Netzwerk) vom Backend.
    Bei Verbindungsfehler: leeres Dict – Anzeige zeigt '–' als Fallback.
    """
    reactive.invalidate_later(POLL_SLOW_S)
    return _json_or_none(_get("/system/info")) or {}

@reactive.calc
def _sensor_readings_data() -> dict:
    """Gecachter /sensors/readings-Fetch. Langsamer Poll genuegt.

    Liefert Bodenfeuchte-Status aller konfigurierten Sensor-Zonen.
    Bei Verbindungsfehler: leeres Dict – Karten zeigen 'Unbekannt'.
    """
    reactive.invalidate_later(POLL_SLOW_S)
    _sensor_trigger.get()
    return _json_or_none(_get("/sensors/readings")) or {}

@reactive.calc
def _sensor_config_data() -> dict:
    """Gecachter /sensors/config-Fetch. Langsamer Poll genuegt.

    Liefert Sensor-Konfiguration (Treiber, Pins, Intervall, Cooldown).
    Aenderungen erfordern Backend-Neustart – seltener Poll genuegt.
    """
    reactive.invalidate_later(POLL_SLOW_S)
    _sensor_trigger.get()
    return _json_or_none(_get("/sensors/config")) or {}

@reactive.calc
def _sensor_assignments_data() -> dict:
    """Gecachter /sensors/assignments-Fetch."""
    reactive.invalidate_later(POLL_SLOW_S)
    _sensor_trigger.get()
    return _json_or_none(_get("/sensors/assignments")) or {}

# Letzter angewendeter dur/unit-Wert – verhindert Reset der Slider bei jedem Poll
_last_applied_dur_unit: reactive.Value = reactive.Value({})
# Zustandsvariable für das Hardware-Fault-Modal.
# Verhindert dass modal_show() bei jedem Poll-Zyklus erneut aufgerufen
# wird und das Modal flackert. Übergang False→True öffnet, True→False schließt.
_fault_modal_open: reactive.Value = reactive.Value(False)
# Zustandsvariable für das Neustart-Erkennungs-Modal.
# Verhindert wiederholtes Öffnen bei jedem Poll-Zyklus nach erkanntem unclean Restart.
# Übergang False→True öffnet das Modal (in _health_poll), True bleibt bis ACK.
_restart_modal_open: reactive.Value = reactive.Value(False)
# Wird auf True gesetzt sobald Settings einmalig aus dem Backend geladen wurden.
# Verhindert dass txt_navbar_title und clr_accent_color bei jedem Poll
# den User-Input ueberschreiben.
_settings_initialized: reactive.Value = reactive.Value(False)

@reactive.effect
def _health_poll():
    reactive.invalidate_later(POLL_STATUS_S)
    ok, health_data = _ping_health()
    if ok:
        _backend_fail_streak.set(0)
        _backend_ok.set(True)
        if _backend_modal_open.get():
            _backend_modal_open.set(False)
            ui.modal_remove()
        # Neustart-Erkennung: Modal nur beim Übergang False→True öffnen.
        # Kein Öffnen wenn das Backend-Modal noch aktiv ist (Modalkonflikt vermeiden).
        if (
            health_data.get("unclean_restart")
            and not _restart_modal_open.get()
            and not _backend_modal_open.get()
        ):
            _restart_modal_open.set(True)
            _show_restart_modal(health_data)
    else:
        streak = _backend_fail_streak.get() + 1
        _backend_fail_streak.set(streak)
        if streak >= BACKEND_FAIL_THRESHOLD:
            _backend_ok.set(False)
            _show_backend_modal()


@reactive.effect
@reactive.event(input.btn_reload_key)
def _h_reload_key():
    key = _load_api_key_from_disk()
    _api_key.set(key)
    _apply_api_key_to_session(key)

    if not key:
        _auth_fail("Key-Datei ist nicht lesbar/leer. Backend sollte sie erzeugen, bitte prüfen.")
        return

    r = _get("/status")
    if r is not None and r.ok:
        ui.notification_show("API-Key neu geladen. Auth OK.", type="message", duration=3)
        _auth_recover()
        _bump_status()
    elif r is not None and r.status_code == 401:
        ui.notification_show("API-Key scheint weiterhin falsch.", type="error", duration=4)
    else:
        ui.notification_show("Backend nicht erreichbar oder anderer Fehler.", type="warning", duration=4)


@reactive.effect
@reactive.event(input.btn_ack_restart)
def _h_ack_restart():
    """Quittiert den Neustart-Hinweis: sendet ACK an Backend und schliesst Modal."""
    rv = _post("/system/ack-restart")
    if rv and rv.ok:
        _restart_modal_open.set(False)
        ui.modal_remove()
    else:
        ui.notification_show(
            "Quittierung fehlgeschlagen – bitte erneut versuchen.",
            type="error",
            duration=4,
        )

# =============================================================================
# NAVBAR
# =============================================================================

# Dynamischer Accent-Style: überschreibt den statischen Block sobald
# _settings_data() den Backend-Wert liefert.
@render.ui
def _dynamic_accent_style():
    d = _settings_data()
    color = d.get("accent_color", ACCENT_COLOR_DEFAULT) if d else ACCENT_COLOR_DEFAULT
    return ui.tags.style(f":root {{ --accent: {color}; }}")

# Dynamischer Navbar-Titel: injiziert ein kleines Script das den
# .navbar-brand-Text im DOM aktualisiert sobald settings geladen sind.
@render.ui
def _dynamic_navbar_title_js():
    d = _settings_data()
    # navbar_title enthält nur den User-konfigurierten Suffix.
    # NAVBAR_PREFIX wird hier unveränderlich vorangestellt.
    suffix = d.get("navbar_title", NAVBAR_TITLE_DEFAULT) if d else NAVBAR_TITLE_DEFAULT
    full_title = NAVBAR_PREFIX + suffix
    title_js = _json_mod.dumps(full_title)
    return ui.tags.script(f"""
        (function() {{
            // Zielt auf den Text-Span, nicht auf das Root-Element – so wird ein
            // eventuell vorhandenes Logo-<img> nicht als childNodes[0] überschrieben.
            var titleEl = document.getElementById('navbar-title-text');
            if (titleEl) titleEl.textContent = {title_js};
            document.title = {title_js};
        }})();
    """)

def _build_navbar_brand() -> ui.Tag:
    """Erzeugt den Navbar-Brand-Inhalt: optional Logo + Titel-Span.

    Das <span id='navbar-title-text'> wird von _dynamic_navbar_title_js()
    nach dem ersten Settings-Poll per getElementById aktualisiert.
    Das Logo-<img> bleibt davon unberührt.
    """
    children = []
    if NAVBAR_LOGO_PATH:
        children.append(
            ui.tags.img(
                src=NAVBAR_LOGO_PATH,
                class_="navbar-brand-logo",
                alt="Logo",
            )
        )
    children.append(
        ui.tags.span(NAVBAR_TITLE_DEFAULT, id="navbar-title-text")
    )
    return ui.div(*children, class_="navbar-brand-inner")

with ui.navset_bar(title=_build_navbar_brand(), id="main_nav", fluid=True):

    ui.nav_spacer()
    # =========================================================================
    # TAB 1 - DASHBOARD
    # =========================================================================
    with ui.nav_panel("Dashboard", value="dashboard"):

        # Hardware-Fault Modal – Zustandsmaschine:
        # _fault_modal_open merkt sich ob das Modal gerade offen ist.
        # modal_show() wird NUR bei Übergang False→True aufgerufen,
        # modal_remove() NUR bei Übergang True→False.
        # Dadurch kein Flackern bei jedem Poll-Zyklus.
        @reactive.effect
        def _fault_modal_controller():
            d = _status_data()
            faulted = bool(d.get("hw_faulted", False)) if d else False
            currently_open = _fault_modal_open.get()

            if faulted and not currently_open:
                # Fault neu aufgetreten → Modal öffnen
                reason = d.get("hw_fault_reason", "") if d else ""
                zone   = d.get("hw_fault_zone", "?") if d else "?"
                since  = d.get("hw_fault_since", "") if d else ""
                _fault_modal_open.set(True)
                ui.modal_show(
                    ui.modal(
                        ui.div(
                            ui.tags.p(
                                ui.tags.b("Zone: "), str(zone),
                                style="margin-bottom:0.35rem;",
                            ),
                            ui.tags.p(
                                ui.tags.b("Ursache: "), reason or "Unbekannt",
                                style="margin-bottom:0.35rem;",
                            ),
                            *(
                                [ui.tags.p(
                                    ui.tags.b("Seit: "), since,
                                    class_="text-muted small",
                                )]
                                if since else []
                            ),
                            ui.tags.hr(),
                            ui.tags.p(
                                "Bitte Ventil und Hardware prüfen, bevor Sie den Fehler quittieren.",
                                class_="text-muted small",
                            ),
                            class_="fault-modal-body",
                        ),
                        title=ui.div(
                            ui.tags.i(class_="bi bi-exclamation-triangle-fill me-2",
                                      style="color:#dc2626;"),
                            "Hardware-Fehler erkannt",
                        ),
                        footer=ui.div(
                            ui.input_action_button(
                                "btn_fault_clear",
                                ui.div(
                                    ui.tags.i(class_="bi bi-check2-circle me-1"),
                                    "Fehler quittieren",
                                ),
                                class_="btn btn-warning",
                            ),
                        ),
                        easy_close=False,
                        size="m",
                    )
                )
            elif not faulted and currently_open:
                # Fault behoben (z.B. extern) → Modal schliessen
                _fault_modal_open.set(False)
                ui.modal_remove()
            # faulted + currently_open → nichts tun, Modal bleibt offen

        # ===== Overview Tiles Row (NEU) =====
        @render.ui
        def _overview_tiles():
            d = _status_data()
            auto = _automation_data().get("automation_enabled", False)
            para = _parallel_data().get("parallel_enabled", False)
            max_conc = _parallel_data().get("max_concurrent_valves", 1)

            if not _auth_ok.get():
                return ui.div()  # bei locked UI keine Kacheln

            active_runs = d.get("active_runs", {}) if d else {}
            running_cnt = len(active_runs) if isinstance(active_runs, dict) else 0

            q_len = d.get("queue_length", 0) if d else 0

            paused = bool(d.get("paused", False)) if d else False
            fault  = bool(d.get("hw_faulted", False)) if d else False

            # Overall state pill
            if fault:
                st = ui.span("FAULT", class_="ov-pill bad")
                st_sub = "Hardware-Fault aktiv"
            elif paused:
                st = ui.span("PAUSE", class_="ov-pill warn")
                st_sub = "Bewässerung pausiert"
            elif running_cnt > 0:
                st = ui.span("RUN", class_="ov-pill ok")
                st_sub = "Bewässerung läuft"
            else:
                st = ui.span("READY", class_="ov-pill")
                st_sub = "System bereit"

            # Queue Status Pill
            q_state = d.get("queue_state", "bereit") if d else "bereit"

            if q_state.lower().startswith("läuf"):
                q_pill = ui.span("RUNNING", class_="ov-pill ok")
            elif q_state.lower().startswith("paus"):
                q_pill = ui.span("PAUSED", class_="ov-pill warn")
            elif q_state.lower().startswith("fert"):
                q_pill = ui.span("DONE", class_="ov-pill")
            else:
                q_pill = ui.span("READY", class_="ov-pill")


            def tile(label: str, value: str, sub: str, ic: str, pill: ui.Tag | None = None):
                return ui.div(
                    ui.div(
                        ui.div(
                            ui.div(label, class_="ov-label"),
                            ui.div(value, class_="ov-value"),
                            ui.div(sub, class_="ov-sub"),
                        ),
                        ui.div(
                            ui.div(icon(ic), class_="ov-icon"),
                            style="display:flex; align-items:center; gap:0.55rem;",
                        ),
                        class_="ov-top",
                    ),
                    (ui.div(pill, style="margin-top:0.55rem;") if pill else ui.div()),
                    class_="ov-tile",
                )

            return ui.div(
                tile(
                    "Aktive Zonen",
                    str(running_cnt),
                    "parallel möglich" if para else "einzeln / seriell",
                    "droplet",
                ),
                tile(
                    "Queue",
                    str(q_len),
                    q_state.capitalize(),
                    "list-check",
                    q_pill,
                ),
                tile(
                    "Automatik",
                    "EIN" if auto else "AUS",
                    "Zeitpläne aktiv" if auto else "manuell",
                    "clock",
                    ui.span("ENABLED" if auto else "DISABLED", class_=("ov-pill ok" if auto else "ov-pill")),
                ),
                tile(
                    "Parallelmodus",
                    "EIN" if para else "AUS",
                    f"{max_conc} Ventile" if para else "1 Ventil",
                    "diagram-project",
                    ui.span("ON" if para else "OFF", class_=("ov-pill ok" if para else "ov-pill")),
                ),
                tile(
                    "System",
                    "OK" if not fault else "FAULT",
                    st_sub,
                    "shield-halved",
                    st,
                ),
                class_="overview-grid",
            )

        with ui.layout_columns(col_widths=[8, 4]):

            with ui.card():
                ui.card_header("Systemstatus")

                @render.ui
                def _status_display():
                    d = _status_data()
                    if not _auth_ok.get():
                        return ui.div(
                            ui.p("UI gesperrt: API-Key fehlt oder ist ungueltig.", class_="text-danger"),
                            ui.div(
                                ui.input_action_button(
                                    "btn_reload_key",
                                    "Key neu laden",
                                    class_="btn btn-sm btn-primary",
                                ),
                                ui.tags.span("Key-Datei:", class_="text-muted ms-3 small"),
                                ui.tags.span(str(_API_KEY_PATH), class_="kbd-like ms-1"),
                                style="display:flex; align-items:center; gap:0.35rem; flex-wrap:wrap;",
                            ),
                        )

                    if not d:
                        return ui.p("Warte auf Backend ...", class_="text-muted")

                    paused      = d.get("paused", False)
                    hw_faulted  = d.get("hw_faulted", False)
                    active_runs = d.get("active_runs", {})
                    q_state     = d.get("queue_state", "bereit")
                    q_len       = d.get("queue_length", 0)
                    auto        = _automation_data().get("automation_enabled", False)
                    parallel    = _parallel_data().get("parallel_enabled", False)

                    if hw_faulted:
                        badge = ui.span("Hardware-Fault", class_="badge text-bg-danger app-badge")
                    elif paused:
                        badge = ui.span("Pausiert", class_="badge text-bg-warning text-dark app-badge")
                    elif active_runs:
                        n = len(active_runs)
                        badge = ui.span(
                            f"Laeuft ({n} Zone{'n' if n > 1 else ''})",
                            class_="badge text-bg-success app-badge",
                        )
                    else:
                        badge = ui.span("Bereit", class_="badge text-bg-secondary app-badge")

                    zone_divs = []
                    for zk, ar in sorted(active_runs.items(), key=lambda x: int(x[0])):
                        rem = int(ar.get("remaining_s", 0) or 0)
                        src = ar.get("started_source", "manuell")

                        planned = (
                            ar.get("planned_s")
                            or ar.get("duration_s")
                            or ar.get("planned")
                            or ar.get("duration")
                            or 0
                        )

                        try:
                            planned = int(planned)
                        except Exception:
                            planned = 0

                        progress_style = ""
                        if planned and planned > 0:
                            pct = max(0.0, min(1.0, 1.0 - (rem / planned))) * 100.0
                            progress_style = f"--progress:{pct:.1f}%;"

                        zone_divs.append(
                            ui.div(
                                ui.tags.b(f"Zone {zk}"),
                                f"  -  {fmt_mmss(rem)} verbleibend  (Quelle: {src})",
                                class_="zone-running",
                                style=progress_style,
                            )
                        )
                    if not zone_divs:
                        rz  = d.get("running_zone")
                        rem = d.get("remaining_time", 0)
                        if rz:
                            zone_divs.append(
                                ui.div(
                                    ui.tags.b(f"Zone {rz}"),
                                    f"  -  {fmt_mmss(rem)} verbleibend",
                                    class_="zone-running",
                                )
                            )

                    return ui.div(
                        ui.div(badge, style="margin-bottom:0.85rem;"),
                        *zone_divs,
                        ui.tags.hr(),
                        ui.div(
                            ui.tags.small("Warteschlange: ", class_="text-muted"),
                            state_badge(q_state),
                            f" ({q_len} Item{'s' if q_len != 1 else ''})",
                        ),
                        ui.div(
                            ui.tags.small("Automatik: ", class_="text-muted"),
                            ui.span("EIN", class_="badge text-bg-success app-badge") if auto
                                else ui.span("AUS", class_="badge text-bg-secondary app-badge"),
                            ui.tags.small("   Parallel: ", class_="text-muted"),
                            ui.span("EIN", class_="badge text-bg-info app-badge") if parallel
                                else ui.span("AUS", class_="badge text-bg-secondary app-badge"),
                            style="margin-top:0.35rem;",
                        ),
                    )

            with ui.card():
                ui.card_header("Schnellaktionen")

                ui.input_task_button(
                    "btn_stop_all", "Alle Ventile STOP",
                    class_="btn-danger w-100 mb-2",
                )
                ui.input_action_button(
                    "btn_pause_all", "Pause",
                    class_="btn btn-warning w-100 mb-2",
                )
                ui.input_action_button(
                    "btn_resume_all", "Fortsetzen",
                    class_="btn btn-success w-100 mb-3",
                )

                ui.tags.hr()
                ui.p("Automatik", class_="form-section-title")
                ui.input_action_button(
                    "btn_auto_on",  "EIN",
                    class_="btn btn-sm btn-success me-1",
                )
                ui.input_action_button(
                    "btn_auto_off", "AUS",
                    class_="btn btn-sm btn-secondary",
                )

                ui.p("Parallelmodus", class_="form-section-title")
                ui.input_action_button(
                    "btn_para_on",  "EIN",
                    class_="btn btn-sm btn-info me-1",
                )
                ui.input_action_button(
                    "btn_para_off", "AUS",
                    class_="btn btn-sm btn-secondary",
                )

        # Handler
        @reactive.effect
        @reactive.event(input.btn_fault_clear)
        def _h_fault_clear():
            r = _post("/fault/clear")
            if r and r.ok:
                # Flag vor modal_remove() zurücksetzen damit der Controller
                # beim nächsten Poll nicht erneut öffnet
                _fault_modal_open.set(False)
                ui.modal_remove()
                ui.notification_show("Hardware-Fault quittiert.", type="message", duration=3)
            else:
                ui.notification_show("Quittierung fehlgeschlagen.", type="error", duration=4)
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_stop_all)
        def _h_stop_all():
            r = _post("/stop")
            ui.update_task_button("btn_stop_all", state="ready")
            if r and r.ok:
                ui.notification_show("Alle Ventile gestoppt.", type="message", duration=3)
            else:
                detail = _json_or_none(r) or {}
                ui.notification_show(
                    detail.get("detail", "Fehler beim Stoppen."), type="error", duration=5,
                )
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_pause_all)
        def _h_pause_all():
            r = _post("/pause")
            if r and r.ok:
                ui.notification_show("Ventile pausiert.", type="message", duration=3)
            elif r and r.status_code == 409:
                ui.notification_show("Bereits pausiert.", type="warning", duration=3)
            else:
                ui.notification_show("Fehler beim Pausieren.", type="error", duration=4)
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_resume_all)
        def _h_resume_all():
            r = _post("/resume")
            if r and r.ok:
                ui.notification_show("Ventile fortgesetzt.", type="message", duration=3)
            else:
                detail = _json_or_none(r) or {}
                ui.notification_show(
                    detail.get("detail", "Fehler beim Fortsetzen."), type="error", duration=4,
                )
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_auto_on)
        def _h_auto_on():
            r = _post("/automation/enable")
            if r and r.ok:
                ui.notification_show("Automatik aktiviert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler.", type="error", duration=4)
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_auto_off)
        def _h_auto_off():
            r = _post("/automation/disable")
            if r and r.ok:
                ui.notification_show("Automatik deaktiviert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler.", type="error", duration=4)
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_para_on)
        def _h_para_on():
            r = _post("/parallel", json={"enabled": True})
            if r and r.ok:
                ui.notification_show("Parallelmodus aktiviert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler.", type="error", duration=4)
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_para_off)
        def _h_para_off():
            r = _post("/parallel", json={"enabled": False})
            if r and r.ok:
                ui.notification_show("Parallelmodus deaktiviert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler.", type="error", duration=4)
            _bump_status()

    # =========================================================================
    # TAB 2 - VENTILE
    # =========================================================================
    with ui.nav_panel("Ventile", value="ventile"):

        with ui.div(class_="valve-grid"):
            for _vi in range(1, ANZAHL_VENTILE + 1):
                with ui.card(id=f"valve_card_{_vi}"):

                    with ui.card_header():
                        with ui.div(style="display:flex; align-items:center; width:100%;"):
                            ui.tags.b(f"Zone {_vi}")

                            @output(id=f"valve_dot_{_vi}")
                            @render.ui
                            def _vdot(_z=_vi):
                                d = _status_data()
                                is_running = str(_z) in d.get("active_runs", {})
                                # margin-left:auto am span selbst schiebt es
                                # im Flex-Container zuverlässig nach rechts.
                                return ui.span(
                                    "",
                                    class_=f"valve-dot {'on' if is_running else 'off'}",
                                    title="Laeuft" if is_running else "Bereit",
                                    style="margin-left:auto; display:block; flex-shrink:0;",
                                )

                    with ui.div(class_="valve-status-area px-3 pt-2"):
                        @output(id=f"valve_status_{_vi}")
                        @render.ui
                        def _vstatus(_z=_vi):
                            d  = _status_data()
                            ar = d.get("active_runs", {}).get(str(_z), {})
                            if ar:
                                rem = ar.get("remaining_s", 0)
                                src = ar.get("started_source", "manuell")
                                return ui.div(
                                    ui.tags.b("Laeuft - "),
                                    fmt_mmss(rem),
                                    f" verbleibend ({src})",
                                    class_="text-success small",
                                )
                            return ui.span("Bereit", class_="text-muted small")

                    with ui.div(class_="px-3 pb-3"):
                        ui.input_slider(
                            f"sld_{_vi}", "Dauer:",
                            min=1, max=60, value=5, step=1,
                        )
                        ui.input_radio_buttons(
                            f"rb_{_vi}", None,
                            choices={"Minuten": "Minuten", "Sekunden": "Sekunden"},
                            selected="Minuten", inline=True,
                        )
                        with ui.div(style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.6rem;"):
                            ui.input_task_button(
                                f"btn_start_{_vi}",
                                "Start",
                                label_busy="Laeuft ...",
                                auto_reset=False,
                            )
                            ui.input_action_button(
                                f"btn_queue_{_vi}",
                                "Queue",
                                icon=icon("list"),
                                class_="btn btn-outline-secondary btn-sm",
                            )

        def _make_start_handler(zone: int):
            @reactive.effect
            @reactive.event(input[f"btn_start_{zone}"])
            def _h(_z=zone):
                dur_raw = input[f"sld_{_z}"]()
                unit    = input[f"rb_{_z}"]()
                dur_s   = dur_raw * 60 if unit == "Minuten" else dur_raw
                r = _post("/start", json={"zone": _z, "duration": dur_s, "time_unit": unit})
                ui.update_task_button(f"btn_start_{_z}", state="ready")
                if r and r.ok:
                    ui.notification_show(
                        f"Zone {_z} gestartet ({dur_raw} {unit}).",
                        type="message", duration=3,
                    )
                else:
                    detail = _json_or_none(r) or {}
                    ui.notification_show(
                        detail.get("detail", f"Fehler beim Starten von Zone {_z}."),
                        type="error", duration=5,
                    )
                _bump_status()

        def _make_queue_handler(zone: int):
            @reactive.effect
            @reactive.event(input[f"btn_queue_{zone}"])
            def _h(_z=zone):
                dur_raw = input[f"sld_{_z}"]()
                unit    = input[f"rb_{_z}"]()
                dur_s   = dur_raw * 60 if unit == "Minuten" else dur_raw
                r = _post("/queue/add", json={"zone": _z, "duration": dur_s, "time_unit": unit})
                if r and r.ok:
                    ui.notification_show(
                        f"Zone {_z} ({dur_raw} {unit}) zur Warteschlange hinzugefuegt.",
                        type="message", duration=3,
                    )
                else:
                    detail = _json_or_none(r) or {}
                    ui.notification_show(
                        detail.get("detail", f"Fehler bei Zone {_z}."),
                        type="error", duration=5,
                    )
                _bump_queue()

        for _vi in range(1, ANZAHL_VENTILE + 1):
            _make_start_handler(_vi)
            _make_queue_handler(_vi)

    # =========================================================================
    # TAB 3 - WARTESCHLANGE
    # =========================================================================
    with ui.nav_panel("Warteschlange", value="queue"):

        with ui.layout_columns(col_widths=[8, 4]):

            with ui.card():
                ui.card_header("Aktuelle Warteschlange")

                @render.ui
                def _queue_display():
                    reactive.invalidate_later(POLL_SLOW_S)
                    _queue_trigger.get()
                    d = _json_or_none(_get("/queue"))
                    if d is None:
                        return ui.p("Keine Verbindung zum Backend.", class_="text-danger")

                    q_state = d.get("queue_state", "bereit")
                    items   = d.get("items", [])
                    q_len   = d.get("queue_length", 0)

                    header = ui.div(
                        state_badge(q_state),
                        f"  {q_len} Item{'s' if q_len != 1 else ''} in der Warteschlange",
                        style="margin-bottom:0.85rem;",
                    )
                    if not items:
                        return ui.div(header, ui.p("Warteschlange ist leer.", class_="text-muted"))

                    rows = [
                        ui.tags.tr(
                            ui.tags.td(str(idx), class_="text-muted"),
                            ui.tags.td(f"Zone {item.get('zone', '?')}"),
                            ui.tags.td(fmt_duration(
                                item.get("duration", 0), item.get("time_unit", "Sekunden")
                            )),
                            ui.tags.td(item.get("time_unit", "")),
                        )
                        for idx, item in enumerate(items, 1)
                    ]
                    return ui.div(
                        header,
                        ui.tags.table(
                            ui.tags.thead(
                                ui.tags.tr(
                                    ui.tags.th("#"),
                                    ui.tags.th("Zone"),
                                    ui.tags.th("Dauer"),
                                    ui.tags.th("Einheit"),
                                )
                            ),
                            ui.tags.tbody(*rows),
                            class_="table table-sm table-hover table-striped",
                        ),
                    )

                with ui.div(style="margin-top:0.9rem; display:flex; gap:0.5rem; flex-wrap:wrap;"):
                    ui.input_task_button(
                        "btn_q_start", "Starten",
                        label_busy="Laeuft ...",
                        auto_reset=False,
                    )
                    ui.input_action_button(
                        "btn_q_pause", "Pause",
                        class_="btn btn-warning",
                    )
                    ui.input_action_button(
                        "btn_q_clear", "Leeren",
                        class_="btn btn-outline-danger",
                    )

            with ui.card():
                ui.card_header("Item hinzufuegen")

                ui.input_selectize(
                    "q_add_zone", "Zone:",
                    choices={
                        "0": f"Alle Zonen ({ANZAHL_VENTILE})",
                        **{str(i): f"Zone {i}" for i in range(1, ANZAHL_VENTILE + 1)},
                    },
                    selected="1",
                )
                ui.input_slider("q_add_dur", "Dauer:", min=1, max=60, value=10)
                ui.input_radio_buttons(
                    "q_add_unit", None,
                    choices={"Minuten": "Minuten", "Sekunden": "Sekunden"},
                    selected="Minuten", inline=True,
                )
                ui.input_action_button(
                    "btn_q_add", "Hinzufuegen",
                    class_="btn btn-primary w-100 mt-2",
                )

        @reactive.effect
        @reactive.event(input.btn_q_start)
        def _h_q_start():
            r = _post("/queue/start")
            ui.update_task_button("btn_q_start", state="ready")
            if r and r.ok:
                started = r.json().get("started_count", 0)
                ui.notification_show(
                    f"Warteschlange gestartet - {started} Zone(n) aktiv.",
                    type="message", duration=3,
                )
            elif r and r.status_code == 400:
                ui.notification_show(
                    (_json_or_none(r) or {}).get("detail", "Queue leer."),
                    type="warning", duration=4,
                )
            else:
                ui.notification_show("Fehler beim Starten.", type="error", duration=4)
            _bump_queue()
            _bump_status()

        @reactive.effect
        @reactive.event(input.btn_q_pause)
        def _h_q_pause():
            r = _post("/queue/pause")
            if r and r.ok:
                ui.notification_show("Warteschlange pausiert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler beim Pausieren.", type="error", duration=4)
            _bump_queue()

        @reactive.effect
        @reactive.event(input.btn_q_clear)
        def _h_q_clear():
            r = _post("/queue/clear")
            if r and r.ok:
                ui.notification_show("Warteschlange geleert.", type="message", duration=3)
            else:
                ui.notification_show("Fehler beim Leeren.", type="error", duration=4)
            _bump_queue()

        @reactive.effect
        @reactive.event(input.btn_q_add)
        def _h_q_add():
            zone  = int(input.q_add_zone())
            dur   = input.q_add_dur()
            unit  = input.q_add_unit()
            dur_s = dur * 60 if unit == "Minuten" else dur
            r = _post("/queue/add", json={"zone": zone, "duration": dur_s, "time_unit": unit})
            if r and r.ok:
                data = _json_or_none(r) or {}
                if zone == 0:
                    n = data.get("zones_added", ANZAHL_VENTILE)
                    msg = f"Alle {n} Zonen ({dur} {unit}) zur Warteschlange hinzugefuegt."
                else:
                    msg = f"Zone {zone} ({dur} {unit}) hinzugefuegt."
                ui.notification_show(msg, type="message", duration=3)
            else:
                detail = _json_or_none(r) or {}
                ui.notification_show(
                    detail.get("detail", "Fehler beim Hinzufuegen."), type="error", duration=5,
                )
            _bump_queue()

    # =========================================================================
    # TAB 4 - SENSOREN
    # =========================================================================
    with ui.nav_panel("Sensoren", value="sensoren"):

        # ----- Konfigurations-Banner -----------------------------------------
        # Zeigt Treiber, Intervall, Cooldown und eventuelle Warnungen.
        # Render-Funktion statt statischem UI: damit Warnbanner (ungueltige Pins,
        # Sim-Fallback) reaktiv auftauchen ohne Seiten-Reload.
        @render.ui
        def _sensor_config_banner():
            cfg = _sensor_config_data()
            if not cfg:
                return ui.div()

            drv         = cfg.get("sensor_driver", "sim")
            mode        = cfg.get("configured_driver_mode", "sim")
            interval_s  = int(cfg.get("polling_interval_s", 30))
            cooldown_s  = int(cfg.get("cooldown_s", 600))
            duration_s  = int(cfg.get("default_duration_s", 300))
            gpio_valid  = bool(cfg.get("gpio_config_valid", True))
            zones       = cfg.get("zones_configured", [])

            # Dauer-Formatierung: Minuten wenn >= 60s, sonst Sekunden
            def _fmt_dur(s: int) -> str:
                return f"{s // 60} min" if s >= 60 else f"{s} s"

            config_bar = ui.div(
                ui.tags.span(ui.tags.b("Treiber: "), drv,      class_="sensor-cfg-item"),
                ui.tags.span(ui.tags.b("Polling: "), f"{interval_s} s", class_="sensor-cfg-item"),
                ui.tags.span(ui.tags.b("Cooldown: "), _fmt_dur(cooldown_s), class_="sensor-cfg-item"),
                ui.tags.span(ui.tags.b("Laufzeit: "), _fmt_dur(duration_s), class_="sensor-cfg-item"),
                ui.tags.span(
                    ui.tags.b("Zonen: "),
                    str(len(zones)) if zones else "–",
                    class_="sensor-cfg-item",
                ),
                class_="sensor-config-bar",
            )

            # Warnbanner aufbauen (mehrere Warnungen moeglich)
            warn_items = []

            # rpi_switch konfiguriert, aber Treiber ist sim → Init-Fehler beim Start
            if mode == "rpi_switch" and drv == "sim":
                warn_items.append(
                    ui.div(
                        ui.tags.i(class_="bi bi-exclamation-triangle-fill me-2",
                                  style="color:#d97706;"),
                        "Sensor-Treiber laeuft im Sim-Modus obwohl 'rpi_switch' "
                        "konfiguriert ist – GPIO-Fehler beim Start. "
                        "Verkabelung und lgpio-Installation pruefen.",
                        class_="sensor-warn-bar",
                    )
                )

            # Ungueltige oder doppelte GPIO-Pins
            if not gpio_valid:
                inv = cfg.get("invalid_pins", [])
                dup = cfg.get("duplicate_pins", [])
                details = []
                if inv:
                    details.append(f"ungueltige Pins: {[e['pin'] for e in inv]}")
                if dup:
                    details.append(f"Duplikate: {[e['pin'] for e in dup]}")
                warn_items.append(
                    ui.div(
                        ui.tags.i(class_="bi bi-exclamation-triangle-fill me-2",
                                  style="color:#d97706;"),
                        "Ungueltige Sensor-Pin-Konfiguration",
                        (f" ({', '.join(details)})" if details else ""),
                        ". Bitte device_config.json pruefen.",
                        class_="sensor-warn-bar",
                    )
                )

            return ui.div(config_bar, *warn_items)

        # ----- Sensor-Karten (eine Card pro Sensor) -------------------------
        @render.ui
        def _sensor_zone_cards():
            d    = _sensor_readings_data()
            cfg  = _sensor_config_data()
            asgn = _sensor_assignments_data()

            sensors        = cfg.get("sensors_configured", [])  if cfg  else []
            readings       = d.get("readings", {})               if d    else {}
            last_triggered = d.get("last_triggered", {})         if d    else {}
            cooldown_s     = int(d.get("cooldown_s", 600))       if d    else 600
            assignments    = asgn.get("assignments", {})         if asgn else {}

            if not sensors:
                return ui.div(
                    ui.tags.i(class_="bi bi-moisture me-2",
                              style="font-size:1.6rem; color:rgba(15,23,42,0.22);"),
                    ui.tags.p("Keine Sensoren konfiguriert.", class_="text-muted",
                              style="margin-top:0.5rem;"),
                    ui.tags.p(
                        "In device_config.json unter 'sensors' → "
                        "'IRRIGATION_SENSOR_PINS' eintragen "
                        '(z.\u00a0B. {"1": 24, "2": 25}).',
                        class_="text-muted small",
                    ),
                    style="text-align:center; padding:2rem 1rem;",
                )

            def _fmt_elapsed(elapsed):
                if elapsed is None:   return "Noch kein Trigger"
                if elapsed < 60:      return f"vor {int(elapsed)} Sek."
                if elapsed < 3600:    return f"vor {int(elapsed / 60)} Min."
                h = int(elapsed / 3600); m = int((elapsed % 3600) / 60)
                return f"vor {h} Std. {m} Min."

            cards = []
            for sid in sensors:
                s_key    = str(sid)
                moisture = readings.get(s_key)
                elapsed  = last_triggered.get(s_key)
                zones    = assignments.get(s_key, [])

                if moisture is None:
                    dot_class, status_text, status_cls = (
                        "sensor-dot unknown", "Unbekannt", "text-muted small"
                    )
                elif moisture:
                    dot_class, status_text, status_cls = (
                        "sensor-dot dry",
                        "Trocken – Bewaesserung noetig",
                        "sensor-status-dry small fw-bold",
                    )
                else:
                    dot_class, status_text, status_cls = (
                        "sensor-dot moist", "Feucht", "sensor-status-moist small"
                    )

                cooldown_row = ui.div()
                if elapsed is not None and elapsed < cooldown_s:
                    rem = int(cooldown_s - elapsed)
                    rem_text = f"{rem // 60} min" if rem >= 60 else f"{rem} s"
                    cooldown_row = ui.div(
                        ui.tags.small(
                            ui.tags.i(class_="bi bi-hourglass-split me-1",
                                      style="color:#6b7280;"),
                            f"Cooldown: noch {rem_text}",
                            class_="text-muted",
                        ),
                        style="margin-top:0.3rem;",
                    )

                if zones:
                    zones_text = ", ".join(f"Zone {z}" for z in sorted(zones))
                    zones_row = ui.div(
                        ui.tags.small(
                            ui.tags.i(class_="bi bi-diagram-3 me-1",
                                      style="color:#6b7280;"),
                            zones_text, class_="text-muted",
                        ),
                        style="margin-top:0.3rem;",
                    )
                else:
                    zones_row = ui.div(
                        ui.tags.small(
                            ui.tags.i(class_="bi bi-exclamation-circle me-1",
                                      style="color:#d97706;"),
                            "Keine Zonen zugeordnet", class_="text-muted",
                        ),
                        style="margin-top:0.3rem;",
                    )

                cards.append(
                    ui.div(
                        ui.div(
                            ui.div(
                                ui.tags.b(f"Sensor {sid}"),
                                ui.span("", class_=dot_class, title=status_text,
                                        style="margin-left:auto; display:block; flex-shrink:0;"),
                                style="display:flex; align-items:center; width:100%;",
                            ),
                            class_="card-header",
                        ),
                        ui.div(
                            ui.div(ui.span(status_text, class_=status_cls),
                                   class_="sensor-status-area px-3 pt-2"),
                            ui.div(
                                ui.tags.small(
                                    ui.tags.i(class_="bi bi-clock me-1",
                                              style="color:#6b7280;"),
                                    _fmt_elapsed(elapsed), class_="text-muted",
                                ),
                                zones_row,
                                cooldown_row,
                                class_="px-3 pb-3 pt-1",
                            ),
                        ),
                        class_="card bslib-card",
                    )
                )

            return ui.div(*cards, class_="sensor-grid")

        # ----- Sim-Steuerung (nur im Sim-Modus sichtbar) ---------------------
        #
        # Exakt dasselbe Pattern wie _settings_initialized im Settings-Tab:
        #
        #   1. STATISCHER Wrapper-div mit id="sim_panel_wrap" – nie in @render.ui.
        #      Die Checkbox-Inputs darin werden vom Poll NICHT neu initialisiert,
        #      weil sie nicht Teil eines @render.ui-Outputs sind.
        #
        #   2. @render.ui _sensor_sim_visibility gibt ausschliesslich ein <style>-Tag
        #      zurueck (display:block / display:none) – reine Render-Funktion,
        #      kein Side-Effect, kein ui.update_*-Aufruf.
        #
        #   3. @reactive.effect _sync_sim_zones: einziger Ort der ui.update_*-Aufrufe.
        #      Guard-Flag _sim_zones_initialized verhindert Reset bei jedem Poll,
        #      analog zu _last_applied_dur_unit im Settings-Tab.

        _sim_zones_initialized: reactive.Value = reactive.Value([])

        # Statischer Wrapper: immer im DOM, Sichtbarkeit via CSS gesteuert.
        # Inputs hier sind nie Teil eines @render.ui – kein Poll-Reset moeglich.
        with ui.div(id="sim_panel_wrap", style="margin-top:1rem;"):
            with ui.div(class_="card bslib-card"):
                ui.div(
                    ui.tags.i(class_="bi bi-bug me-2", style="color:#6b7280;"),
                    "Sim-Steuerung",
                    ui.tags.span(
                        "nur im Sim-Modus",
                        class_="badge text-bg-secondary app-badge ms-2",
                        style="font-size:0.72rem; vertical-align:middle;",
                    ),
                    class_="card-header",
                    style="font-weight:700;",
                )
                with ui.div(class_="card-body", style="padding:1rem;"):
                    ui.tags.p(
                        "Zonen manuell auf trocken oder feucht setzen. "
                        "Der naechste Polling-Zyklus wertet den gesetzten "
                        "Zustand aus und loest ggf. einen Bewaesserungslauf aus.",
                        class_="text-muted small",
                        style="margin-bottom:1rem;",
                    )
                    with ui.div(style="display:flex; gap:2rem; flex-wrap:wrap; margin-bottom:1rem;"):
                        with ui.div(style="flex:1; min-width:160px;"):
                            ui.tags.p("Sensor trocken setzen", class_="form-section-title")
                            # Leere choices – _sync_sim_zones fuellt sie beim ersten Poll
                            ui.input_checkbox_group(
                                "sim_dry_zones", label=None, choices={}, inline=True,
                            )
                        with ui.div(style="flex:1; min-width:160px;"):
                            ui.tags.p("Sensor feucht setzen", class_="form-section-title")
                            ui.input_checkbox_group(
                                "sim_moist_zones", label=None, choices={}, inline=True,
                            )
                    ui.input_action_button(
                        "btn_sim_set",
                        ui.div(ui.tags.i(class_="bi bi-send me-1"), "Zustand setzen"),
                        class_="btn btn-sm btn-outline-secondary",
                    )

        @render.ui
        def _sensor_sim_visibility():
            """Steuert Sichtbarkeit von #sim_panel_wrap via injiziertem <style>-Tag.

            Pure Render-Funktion: gibt ausschliesslich ein <style>-Tag zurueck,
            kein ui.update_*-Aufruf (Shiny-Regel: Side-Effects nur in @reactive.effect).
            Analog zu _dynamic_accent_style im Navbar-Bereich.
            """
            cfg = _sensor_config_data()
            is_sim = (
                bool(cfg)
                and cfg.get("sensor_driver", "") == "sim"
                and bool(cfg.get("sensors_configured", []))
            )
            display = "block" if is_sim else "none"
            return ui.tags.style(f"#sim_panel_wrap {{ display: {display} !important; }}")

        @reactive.effect
        def _sync_sim_zones():
            """Aktualisiert Checkbox-Choices wenn sich die Zone-Liste aendert.

            Einziger Ort fuer ui.update_checkbox_group()-Aufrufe (Side-Effect).
            Guard-Flag _sim_zones_initialized verhindert Reset bei unveraenderter
            Zone-Liste – identisches Muster wie _last_applied_dur_unit im Settings-Tab.
            """
            cfg = _sensor_config_data()
            sensors = cfg.get("sensors_configured", []) if cfg else []

            # Keine Aenderung → nichts tun, Auswahl des Nutzers bleibt erhalten
            if _sim_zones_initialized.get() == sensors:
                return

            _sim_zones_initialized.set(sensors)
            sensor_choices = {str(s): f"Sensor {s}" for s in sensors}
            ui.update_checkbox_group("sim_dry_zones",   choices=sensor_choices, selected=[])
            ui.update_checkbox_group("sim_moist_zones", choices=sensor_choices, selected=[])

        @reactive.effect
        @reactive.event(input.btn_sim_set)
        def _h_sim_set():
            """Sendet ausgewaehlte Zonen-Zustaende an POST /sensors/sim/set."""
            try:
                dry_raw   = input.sim_dry_zones()   or []
                moist_raw = input.sim_moist_zones() or []
            except Exception:
                ui.notification_show(
                    "Fehler beim Lesen der Auswahl.", type="error", duration=4,
                )
                return

            dry_sensors   = [int(z) for z in dry_raw]
            moist_sensors = [int(z) for z in moist_raw]

            overlap = set(dry_sensors) & set(moist_sensors)
            if overlap:
                ui.notification_show(
                    f"Zonen {sorted(overlap)} sind in beiden Listen. "
                    "Bitte Auswahl korrigieren.",
                    type="warning",
                    duration=5,
                )
                return

            if not dry_sensors and not moist_sensors:
                ui.notification_show(
                    "Keine Zonen ausgewaehlt.", type="warning", duration=3,
                )
                return

            rv = _post("/sensors/sim/set", json={
                "dry_sensors":   dry_sensors,
                "moist_sensors": moist_sensors,
            })

            if rv and rv.ok:
                data = rv.json()
                dry_now   = data.get("dry_sensors", [])
                moist_now = []
                parts = []
                if dry_now:
                    parts.append(f"Trocken: {dry_now}")
                if moist_now:
                    parts.append(f"Feucht: {moist_now}")
                ui.notification_show(
                    "Sim-Zustand gesetzt. " + (" | ".join(parts)),
                    type="message",
                    duration=4,
                )
                # Auswahl nach erfolgreichem Setzen zuruecksetzen –
                # _sim_zones_initialized bleibt unveraendert (Zonen-Liste
                # hat sich nicht geaendert, nur die Auswahl wird geleert).
                cfg     = _sensor_config_data()
                sensors = cfg.get("sensors_configured", []) if cfg else []
                sensor_choices = {str(s): f"Sensor {s}" for s in sensors}
                ui.update_checkbox_group("sim_dry_zones",   choices=sensor_choices, selected=[])
                ui.update_checkbox_group("sim_moist_zones", choices=sensor_choices, selected=[])
                _bump_sensor()
            elif rv and rv.status_code == 404:
                ui.notification_show(
                    "Backend ist nicht im Sim-Modus.", type="error", duration=4,
                )
            elif rv and rv.status_code == 422:
                detail = _json_or_none(rv) or {}
                ui.notification_show(
                    detail.get("detail", "Ungueltige Eingabe."),
                    type="error", duration=5,
                )
            else:
                ui.notification_show(
                    "Sim-Set fehlgeschlagen.", type="error", duration=4,
                )

    # =========================================================================
    # TAB 5 - ZEITPLAENE
    # =========================================================================
    with ui.nav_panel("Zeitplaene", value="schedule"):

        _schedule_cache = reactive.Value([])
        # Selektionszustand wird NICHT über Shiny-Reaktivität verwaltet,
        # sondern über window.schSelectedIds im Browser (JS-Set).
        # Grund: @render.ui re-initialisiert alle enthaltenen Inputs bei
        # jedem Poll – Shiny-basiertes State-Tracking ist zirkulär und
        # nicht zuverlässig. window.schSelectedIds überlebt Re-Renders.

        # Statischer JS-Block: wird einmalig beim Laden der Seite initialisiert.
        #
        # Warum MutationObserver statt Inline-Script in @render.ui?
        # @render.ui führt Inline-Scripts WÄHREND Shinys bindAll()-Durchlauf aus.
        # Shinys Input-Initialisierung kann danach Checkbox-Zustände überschreiben.
        # Der MutationObserver feuert NACH abgeschlossenem DOM-Update, unabhängig
        # vom Shiny-Render-Zyklus – keine Race Conditions möglich.
        ui.tags.script("""
            window.schSelectedIds = window.schSelectedIds || new Set();

            window.schCbChange = function(el) {
                var id = el.getAttribute('data-sch-id');
                if (el.checked) {
                    window.schSelectedIds.add(id);
                } else {
                    window.schSelectedIds.delete(id);
                }
                Shiny.setInputValue(
                    'sch_checked_ids',
                    Array.from(window.schSelectedIds),
                    {priority: 'event'}
                );
            };

            window.schClearSelection = function() {
                window.schSelectedIds.clear();
                Shiny.setInputValue('sch_checked_ids', [], {priority: 'event'});
            };

            // MutationObserver: überwacht den _schedule_table-Output-Container.
            // Sobald Shiny die Tabelle neu rendert (childList-Änderung), werden
            // alle Checkboxen sofort auf den Zustand aus window.schSelectedIds gesetzt.
            // Startet sobald das Element im DOM vorhanden ist.
            function schSetupObserver() {
                var target = document.getElementById('_schedule_table');
                if (!target) {
                    setTimeout(schSetupObserver, 150);
                    return;
                }
                var observer = new MutationObserver(function() {
                    document.querySelectorAll('[data-sch-id]').forEach(function(el) {
                        el.checked = window.schSelectedIds.has(
                            el.getAttribute('data-sch-id')
                        );
                    });
                });
                observer.observe(target, { childList: true, subtree: true });
            }

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', schSetupObserver);
            } else {
                schSetupObserver();
            }
        """)

        with ui.layout_columns(col_widths=[8, 4]):

            with ui.card():
                ui.card_header("Zeitplaene")

                @render.ui
                def _schedule_table():
                    reactive.invalidate_later(POLL_SLOW_S)
                    _schedule_trigger.get()
                    d = _json_or_none(_get("/schedule"))
                    if d is None:
                        return ui.p("Keine Verbindung zum Backend.", class_="text-danger")

                    items = d.get("items", [])
                    _schedule_cache.set(items)

                    if not items:
                        return ui.p("Keine Zeitplaene vorhanden.", class_="text-muted")

                    rows = []
                    for idx, item in enumerate(items):
                        zone     = item.get("zone", 0)
                        weekdays = item.get("weekdays", [])
                        times    = item.get("start_times", [])
                        dur_s    = item.get("duration_s", 0)
                        unit     = item.get("time_unit", "Sekunden")
                        repeat   = item.get("repeat", False)
                        enabled  = item.get("enabled", True)

                        zone_label = f"Alle ({ANZAHL_VENTILE})" if zone == 0 else f"Zone {zone}"
                        status_span = (
                            ui.span("EIN", class_="badge text-bg-success app-badge") if enabled
                            else ui.span("AUS", class_="badge text-bg-secondary app-badge")
                        )
                        rows.append(
                            ui.tags.tr(
                                ui.tags.td(
                                    # Natives HTML-Checkbox statt ui.input_checkbox:
                                    # Shiny würde dynamische Inputs bei jedem Re-Render
                                    # zurücksetzen. Der checked-Zustand wird via
                                    # window.schSelectedIds in JS gehalten und durch
                                    # den restore-Script nach jedem Re-Render gesetzt.
                                    ui.tags.input(
                                        type="checkbox",
                                        id=f"cb_sch_{idx}",
                                        **{"data-sch-id": item.get("id", "")},
                                        onchange="window.schCbChange(this)",
                                        style="cursor:pointer;width:1.1rem;height:1.1rem;",
                                    ),
                                    style="width:1%;text-align:center;vertical-align:middle;",
                                ),
                                ui.tags.td(zone_label),
                                ui.tags.td(fmt_weekdays(weekdays)),
                                ui.tags.td(", ".join(times)),
                                ui.tags.td(fmt_duration(dur_s, unit)),
                                ui.tags.td("woechtl." if repeat else "einmalig"),
                                ui.tags.td(status_span),
                                # Klick auf die gesamte Zeile togglet die Checkbox.
                                # Guard: event.target.type === 'checkbox' verhindert
                                # Doppel-Fire wenn die Checkbox direkt angeklickt wird
                                # (dann feuert der native onchange bereits).
                                onclick=(
                                    "if(event.target.type!=='checkbox'){"
                                    f"var cb=document.getElementById('cb_sch_{idx}');"
                                    "cb.checked=!cb.checked;"
                                    "window.schCbChange(cb);"
                                    "}"
                                ),
                            )
                        )

                    return ui.div(
                        ui.tags.table(
                            ui.tags.thead(
                                ui.tags.tr(
                                    ui.tags.th(""),
                                    ui.tags.th("Zone"),
                                    ui.tags.th("Tage"),
                                    ui.tags.th("Uhrzeit"),
                                    ui.tags.th("Dauer"),
                                    ui.tags.th("Typ"),
                                    ui.tags.th("Status"),
                                )
                            ),
                            ui.tags.tbody(*rows),
                            class_="table sch-table table-hover table-striped",
                        ),
                        ui.div(
                            ui.input_action_button(
                                "btn_sch_enable_sel",  "Aktivieren",
                                class_="btn btn-sm btn-success me-1",
                            ),
                            ui.input_action_button(
                                "btn_sch_disable_sel", "Deaktivieren",
                                class_="btn btn-sm btn-secondary me-1",
                            ),
                            ui.input_action_button(
                                "btn_sch_delete_sel",  "Loeschen",
                                class_="btn btn-sm btn-outline-danger",
                            ),
                            style="margin-top:0.8rem; display:flex; gap:0.45rem; flex-wrap:wrap;",
                        ),
                    )

                def _selected_ids() -> list[str]:
                    """Liest selektierte IDs aus dem Shiny-Input sch_checked_ids.

                    Wird von window.schCbChange via Shiny.setInputValue befüllt.
                    """
                    try:
                        val = input.sch_checked_ids()
                        return list(val) if val else []
                    except Exception:
                        return []

                def _clear_selection():
                    """Leert window.schSelectedIds im Browser und den Shiny-Input."""
                    ui.insert_ui(
                        selector="body",
                        where="beforeEnd",
                        ui=ui.tags.script("window.schClearSelection();"),
                        immediate=True,
                    )

                @reactive.effect
                @reactive.event(input.btn_sch_enable_sel)
                def _h_sch_enable():
                    ids = _selected_ids()
                    if not ids:
                        ui.notification_show("Bitte Zeitplaene auswaehlen.", type="warning", duration=3)
                        return
                    ok_count = sum(
                        1 for sid in ids
                        if (rv := _post(f"/schedule/enable/{sid}")) and rv.ok
                    )
                    _clear_selection()
                    ui.notification_show(f"{ok_count}/{len(ids)} aktiviert.", type="message", duration=3)
                    _bump_schedule()

                @reactive.effect
                @reactive.event(input.btn_sch_disable_sel)
                def _h_sch_disable():
                    ids = _selected_ids()
                    if not ids:
                        ui.notification_show("Bitte Zeitplaene auswaehlen.", type="warning", duration=3)
                        return
                    ok_count = sum(
                        1 for sid in ids
                        if (rv := _post(f"/schedule/disable/{sid}")) and rv.ok
                    )
                    _clear_selection()
                    ui.notification_show(f"{ok_count}/{len(ids)} deaktiviert.", type="message", duration=3)
                    _bump_schedule()

                @reactive.effect
                @reactive.event(input.btn_sch_delete_sel)
                def _h_sch_delete():
                    ids = _selected_ids()
                    if not ids:
                        ui.notification_show("Bitte Zeitplaene auswaehlen.", type="warning", duration=3)
                        return
                    rv = _delete("/schedule", json=ids)
                    if rv and rv.ok:
                        _clear_selection()
                        ui.notification_show(f"{len(ids)} geloescht.", type="message", duration=3)
                    else:
                        ui.notification_show("Fehler beim Loeschen.", type="error", duration=4)
                    _bump_schedule()

            with ui.card():
                ui.card_header("Zeitplan hinzufuegen")

                ui.input_selectize(
                    "sch_zone", "Zone:",
                    choices={
                        "0": f"Alle Zonen ({ANZAHL_VENTILE})",
                        **{str(i): f"Zone {i}" for i in range(1, ANZAHL_VENTILE + 1)},
                    },
                    selected="0",
                )
                ui.input_slider("sch_dur", "Dauer:", min=1, max=120, value=10)
                ui.input_radio_buttons(
                    "sch_unit", None,
                    choices={"Minuten": "Minuten", "Sekunden": "Sekunden"},
                    selected="Minuten", inline=True,
                )

                ui.p("Wochentage", class_="form-section-title")
                ui.input_checkbox_group(
                    "sch_days", None,
                    choices=WEEKDAY_CHOICES,
                    inline=True,
                )
                ui.input_checkbox("sch_all_days", "Alle Tage", value=False)

                ui.p("Startzeit (HH:MM)", class_="form-section-title")
                ui.input_text(
                    "sch_time", None, value="07:00",
                    placeholder="z.B. 07:00 oder 07:00, 12:00",
                )

                ui.p("Wiederholung", class_="form-section-title")
                ui.input_radio_buttons(
                    "sch_repeat", None,
                    choices={"true": "Woechentlich", "false": "Einmalig"},
                    selected="true", inline=True,
                )

                ui.input_action_button(
                    "btn_sch_add", "Zeitplan speichern",
                    class_="btn btn-primary w-100 mt-3",
                )

        @reactive.effect
        @reactive.event(input.sch_all_days)
        def _h_all_days():
            if input.sch_all_days():
                ui.update_checkbox_group("sch_days", selected=list(WEEKDAY_CHOICES.keys()))
            else:
                ui.update_checkbox_group("sch_days", selected=[])

        @reactive.effect
        @reactive.event(input.btn_sch_add)
        def _h_sch_add():
            zone      = int(input.sch_zone())
            dur       = input.sch_dur()
            unit      = input.sch_unit()
            dur_s     = dur * 60 if unit == "Minuten" else dur
            days      = [int(d) for d in (input.sch_days() or [])]
            times_raw = input.sch_time()
            repeat    = input.sch_repeat() == "true"

            if not days:
                ui.notification_show("Bitte mindestens einen Tag auswaehlen.", type="warning", duration=4)
                return

            time_list = [t.strip() for t in times_raw.split(",") if t.strip()]
            if not time_list:
                ui.notification_show("Bitte Startzeit angeben.", type="warning", duration=4)
                return
            for t in time_list:
                if not re.match(r"^\d{1,2}:\d{2}$", t):
                    ui.notification_show(
                        f"Ungueltiges Format: '{t}' - erwartet HH:MM.", type="error", duration=5,
                    )
                    return

            rv = _post("/schedule/add", json={
                "zone": zone,
                "duration_s": dur_s,
                "time_unit": unit,
                "weekdays": days,
                "start_times": time_list,
                "repeat": repeat,
            })
            if rv and rv.ok:
                ui.notification_show("Zeitplan gespeichert.", type="message", duration=3)
            elif rv and rv.status_code == 400:
                ui.notification_show(
                    (_json_or_none(rv) or {}).get("detail", "Fehler."), type="error", duration=5,
                )
            else:
                ui.notification_show("Fehler beim Speichern.", type="error", duration=5)
            _bump_schedule()

    # =========================================================================
    # TAB 6 - VERLAUF
    # =========================================================================
    with ui.nav_panel("Verlauf", value="history"):

        with ui.card():
            with ui.card_header():
                with ui.div(style="display:flex; justify-content:space-between; align-items:center; width:100%;"):
                    ui.span("Bewaesserungsverlauf")
                    ui.input_action_button(
                        "btn_history_refresh", "Aktualisieren",
                        class_="btn btn-sm btn-outline-secondary",
                    )

            @render.ui
            def _history_display():
                reactive.invalidate_later(POLL_SLOW_S)
                _history_trigger.get()
                d = _json_or_none(_get("/history"))
                if d is None:
                    return ui.p("Keine Verbindung zum Backend.", class_="text-danger")

                items = d.get("items", [])
                count = d.get("count", 0)

                if not items:
                    return ui.p("Noch keine Eintraege.", class_="text-muted")

                source_labels = {
                    "manual":   "Manuell",
                    "schedule": "Zeitplan",
                    "queue":    "Warteschlange",
                }

                rows = []
                for item in (items):
                    ts   = item.get("ts_end", "")
                    zone = item.get("zone", "?")
                    dur  = item.get("duration_s", 0)
                    src  = item.get("source", "")
                    unit = item.get("time_unit", "Sekunden")
                    ts_fmt = ts[:16].replace("T", " ") if isinstance(ts, str) else str(ts)
                    rows.append(
                        ui.tags.tr(
                            ui.tags.td(ts_fmt, class_="text-muted small"),
                            ui.tags.td(f"Zone {zone}"),
                            ui.tags.td(fmt_duration(dur, unit)),
                            ui.tags.td(source_labels.get(src, src)),
                        )
                    )

                return ui.div(
                    ui.p(f"{count} Eintraege gesamt.", class_="text-muted small mb-2"),
                    ui.div(
                        ui.tags.table(
                            ui.tags.thead(
                                ui.tags.tr(
                                    ui.tags.th("Zeitpunkt"),
                                    ui.tags.th("Zone"),
                                    ui.tags.th("Dauer"),
                                    ui.tags.th("Quelle"),
                                )
                            ),
                            ui.tags.tbody(*rows),
                            class_="table table-sm table-hover table-striped history-table",
                        ),
                        style="overflow-x:auto;",
                    ),
                )

        @reactive.effect
        @reactive.event(input.btn_history_refresh)
        def _h_history_refresh():
            _bump_history()

    # =========================================================================
    # TAB 7 - EINSTELLUNGEN
    # =========================================================================
    ui.nav_spacer()

    with ui.nav_control():

        @render.ui
        def _nav_clock2():
            reactive.invalidate_later(1)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            return ui.span(now, class_="badge nav-clock-badge")

    with ui.nav_panel(
        ui.tags.span(
            ui.tags.i(class_="bi bi-gear-fill"),
            title="Einstellungen",
            style="font-size:1.15rem;",
        ),
        value="settings",
    ):

        with ui.layout_columns(col_widths=[12]):

            # ----- Benutzereinstellungen (via Backend-API) -------------------
            with ui.card():
                ui.card_header("Benutzereinstellungen")

                # --- Darstellung ---------------------------------------------
                with ui.div(class_="settings-section"):
                    ui.p("Darstellung", class_="settings-section-title")

                    ui.p("Navbar-Titel", class_="fw-semibold mb-1")
                    ui.div(
                        # ui.tags.span(
                        #     NAVBAR_PREFIX,
                        #     class_="input-group-text",
                        #     style="font-size:0.9rem;",
                        # ),
                        ui.input_text(
                            "txt_navbar_title", None,
                            value="Bewaesserungscomputer",
                            placeholder="z.B. Hof Muster",
                        ),
                        class_="input-group",
                    )

                    ui.p("Akzentfarbe", class_="fw-semibold mt-3 mb-1")
                    with ui.div(class_="d-flex align-items-center gap-3"):
                        ui.tags.input(
                            id="clr_accent_color",
                            type="color",
                            value="#82372a",
                            title="Akzentfarbe waehlen",
                            style=(
                                "width:52px;height:40px;padding:3px;"
                                "border-radius:8px;cursor:pointer;"
                                "border:1px solid var(--bs-border-color);"
                            ),
                            oninput=(
                                "Shiny.setInputValue('clr_accent_color', this.value, "
                                "{priority: 'event'});"
                            ),
                            onchange=(
                                "Shiny.setInputValue('clr_accent_color', this.value, "
                                "{priority: 'event'});"
                            ),
                        )
                        @render.ui
                        def _accent_hex_label():
                            val = input.clr_accent_color() if hasattr(input, 'clr_accent_color') else None
                            if not val:
                                d = _settings_data()
                                val = d.get("accent_color", ACCENT_COLOR_DEFAULT) if d else ACCENT_COLOR_DEFAULT
                            return ui.span(val, class_="text-muted small font-monospace")

                        ui.input_action_button(
                            "btn_reset_accent", "Zurücksetzen",
                            class_="btn btn-sm btn-outline-secondary",
                            title=f"Akzentfarbe auf {ACCENT_COLOR_DEFAULT} zurücksetzen",
                        )

                # --- Ventilsteuerung -----------------------------------------
                with ui.div(class_="settings-section"):
                    ui.p("Ventilsteuerung – Standardwerte", class_="settings-section-title")

                    # Label zeigt die gültige Bereichsangabe dynamisch nach dem
                    # ersten Settings-Poll. Nur Text – kein Input-Reset-Risiko.
                    @render.ui
                    def _slider_max_label():
                        d = _settings_data()
                        hard_max_s   = d.get("hard_max_runtime_s", 3600) if d else 3600
                        hard_max_min = max(1, hard_max_s // 60)
                        return ui.p(
                            f"Max. Laufzeit-Slider in Minuten (1\u2013{hard_max_min})",
                            class_="fw-semibold mb-1",
                        )

                    # Statisches Widget mit sicheren Fallback-Werten.
                    # max= und value= werden einmalig in _sync_settings_to_ui (Block A)
                    # via ui.update_slider() auf die Backend-Werte gesetzt.
                    # Kein render.ui → kein Poll-Reset.
                    ui.input_slider("sld_slider_max_minutes", None, min=1, max=60, value=60, step=1)

                    @render.ui
                    def _slider_default_label():
                        d = _settings_data()
                        slider_max = max(1, d.get("slider_max_minutes", 60) if d else 60)
                        return ui.p(
                            f"Standard-Laufzeit (1\u2013{slider_max})",
                            class_="fw-semibold mt-3 mb-1",
                        )

                    # Statisches Widget – max= und value= werden in Block A gesetzt,
                    # value= zusätzlich in Block B bei Wertänderung (ohne max= anzutasten).
                    ui.input_slider("sld_default_duration", None, min=1, max=60, value=5, step=1)

                    ui.p("Zeiteinheit", class_="fw-semibold mt-2 mb-1")
                    ui.input_radio_buttons(
                        "rb_default_time_unit", None,
                        choices={"Minuten": "Minuten", "Sekunden": "Sekunden"},
                        selected="Minuten", inline=True,
                    )

                # --- Verlauf -------------------------------------------------
                with ui.div(class_="settings-section"):
                    ui.p("Verlauf", class_="settings-section-title")
                    ui.p("Max. Verlaufseintraege (1–500)", class_="fw-semibold")
                    ui.input_slider("sld_max_history", None, min=1, max=500, value=20, step=1)

                ui.input_action_button(
                    "btn_save_settings", "Einstellungen speichern",
                    class_="btn btn-primary w-100 mt-3",
                )

                @render.ui
                def _settings_mismatch_warn():
                    """Warnung wenn ANZAHL_VENTILE != Backend-max_valves."""
                    d = _settings_data()
                    backend_max = d.get("max_valves") if d else None
                    if backend_max is not None and backend_max != ANZAHL_VENTILE:
                        return ui.div(
                            ui.tags.b("Hinweis: "),
                            f"Backend max_valves={backend_max}, "
                            f"Frontend zeigt {ANZAHL_VENTILE} Ventile. "
                            "Bitte device_config.json und frontend_config.json pruefen "
                            "und das Frontend neu starten.",
                            class_="fault-banner mt-3",
                        )
                    return ui.div()

        # ----- Systeminfo (ausserhalb layout_columns, volle Breite) -----------
        with ui.card(class_="mt-3"):
            ui.card_header("Systeminfo")

            @render.ui
            def _settings_sysinfo():
                d  = _settings_data()
                si = _sysinfo_data()

                backend_max  = d.get("max_valves",   "?") if d else "?"
                valve_driver = d.get("valve_driver", "?") if d else "?"

                # --- OS-Metriken aus /system/info ----------------------------
                disk   = si.get("disk",   {}) or {}
                mem    = si.get("memory", {}) or {}
                net    = si.get("network", []) or []

                disk_str   = fmt_disk(
                    disk.get("free_gb"), disk.get("total_gb"), disk.get("used_pct")
                )
                mem_str    = fmt_memory(
                    mem.get("used_mb"), mem.get("total_mb"), mem.get("used_pct")
                )
                uptime_raw = si.get("uptime_s")
                uptime_str = fmt_uptime(uptime_raw) if uptime_raw is not None else "–"

                # --- Netzwerk-Zeilen aufbauen --------------------------------
                net_rows: list[tuple[str, str]] = []
                if not net:
                    net_rows.append(("Netzwerk", "–"))
                for iface in net:
                    label    = iface.get("type", "Netzwerk")
                    name     = iface.get("name", "")
                    is_up    = iface.get("is_up", False)
                    ip       = iface.get("ip") or ""
                    status   = "verbunden" if is_up else "getrennt"
                    ip_part  = f" · {ip}" if ip and is_up else ""
                    value    = f"{name}: {status}{ip_part}"
                    net_rows.append((label, value))

                    if iface.get("type") == "WLAN" and is_up:
                        ssid   = iface.get("ssid") or "–"
                        signal = fmt_signal(iface.get("signal_pct"))
                        net_rows.append(("WLAN SSID",   ssid))
                        net_rows.append(("WLAN Signal", signal))

                # --- Alle Zeilen zusammenbauen --------------------------------
                config_rows = [
                    ("Backend-URL",          BASE_URL),
                    ("Ventile (Frontend)",   str(ANZAHL_VENTILE)),
                    ("Ventile (Backend)",    str(backend_max)),
                    ("Treiber",              valve_driver),
                    ("Status-Poll",          f"{POLL_STATUS_S} s"),
                    ("Slow-Poll",            f"{POLL_SLOW_S} s"),
                    ("Backend-Fail-Schwelle", f"{BACKEND_FAIL_THRESHOLD} Fehlschlaege"),
                ]
                sys_rows = [
                    ("Uptime",       uptime_str),
                    ("RAM",          mem_str),
                    ("Speicherplatz", disk_str),
                    *net_rows,
                ]

                def _make_rows(rows: list[tuple[str, str]]):
                    return [
                        ui.tags.tr(
                            ui.tags.td(lbl, class_="text-muted small pe-3",
                                       style="white-space:nowrap;width:1%;"),
                            ui.tags.td(val, class_="small fw-semibold"),
                        )
                        for lbl, val in rows
                    ]

                def _section_header(title: str):
                    return ui.tags.tr(
                        ui.tags.td(
                            title,
                            colspan="2",
                            class_="text-uppercase small fw-bold pt-3 pb-1",
                            style="color:var(--bs-secondary);letter-spacing:.05em;",
                        )
                    )

                return ui.div(
                    ui.tags.img(
                        src="noria-logo-animated-light.svg",
                        alt="Noria",
                        style="height:56px; display:block; margin-bottom:0.5rem;",
                    ),
                    ui.tags.p(
                        __version__,
                        class_="text-muted small mb-3",
                        style="margin-top:0;",
                    ),
                    ui.tags.table(
                        ui.tags.tbody(
                            _section_header("Konfiguration"),
                            *_make_rows(config_rows),
                            _section_header("System"),
                            *_make_rows(sys_rows),
                        ),
                        class_="table table-sm",
                    ),
                )

        # ----- Log-Download --------------------------------------------------
        with ui.card(class_="mt-3"):
            ui.card_header("Diagnose-Logs")
            ui.p(
                "Lädt alle vorhandenen Log-Dateien als ZIP-Archiv herunter "
                "(aktuell + rotierte Backups, max. ~110 MB).",
                class_="text-muted small mb-3",
            )

            @render.download(
                label="Logs herunterladen",
                filename=lambda: (
                    f"noria-logs-{datetime.datetime.now().strftime('%Y-%m-%d')}.zip"
                ),
                media_type="application/zip",
            )
            def _download_logs():
                """Ruft GET /system/logs/download ab und reicht die ZIP durch.

                Das Backend zippt alle Log-Dateien in-memory und liefert sie
                als StreamingResponse. Shiny empfängt den Response-Body und
                reicht ihn 1:1 an den Browser weiter. So bleibt die Auth
                beim Backend (X-API-Key) und der Download wird geloggt.

                Bei Fehler (Backend nicht erreichbar, kein Key) wird eine
                leere Datei zurückgegeben und eine Fehlermeldung angezeigt.
                """
                try:
                    r = _session.get(
                        BASE_URL + "/system/logs/download",
                        timeout=30.0,   # ZIP-Erstellung kann einen Moment dauern
                        stream=True,
                    )
                    r = _wrap_auth(r)
                    if r is None or not r.ok:
                        ui.notification_show(
                            "Log-Download fehlgeschlagen – Backend nicht erreichbar"
                            " oder kein API-Key.",
                            type="error",
                            duration=5,
                        )
                        yield b""
                        return
                    yield r.content
                except Exception:
                    ui.notification_show(
                        "Log-Download fehlgeschlagen – Verbindungsfehler.",
                        type="error",
                        duration=5,
                    )
                    yield b""

        # ----- Speichern -----------------------------------------------------
        @reactive.effect
        @reactive.event(input.btn_save_settings)
        def _h_save_settings():
            hist_val  = input.sld_max_history()
            # txt_navbar_title enthält nur den Suffix (ohne NAVBAR_PREFIX).
            # NAVBAR_PREFIX wird in _dynamic_navbar_title_js angehängt und
            # niemals in der Datenbank gespeichert.
            title_val = input.txt_navbar_title() or NAVBAR_TITLE_DEFAULT
            # clr_accent_color kommt via Shiny.setInputValue; Fallback auf Default
            try:
                color_val = input.clr_accent_color()
            except Exception:
                color_val = None
            if not color_val or not re.match(r'^#[0-9a-fA-F]{6}$', color_val):
                d = _settings_data()
                color_val = d.get("accent_color", ACCENT_COLOR_DEFAULT) if d else ACCENT_COLOR_DEFAULT

            dur_val      = input.sld_default_duration()
            unit_val     = input.rb_default_time_unit()
            slider_max_val = input.sld_slider_max_minutes()

            rv = _post("/settings", json={
                "max_history_items":  hist_val,
                "navbar_title":       title_val.strip(),
                "accent_color":       color_val.lower(),
                "default_duration":   dur_val,
                "default_time_unit":  unit_val,
                "slider_max_minutes": slider_max_val,
            })
            if rv and rv.ok:
                ui.notification_show(
                    "Einstellungen gespeichert.", type="message", duration=3,
                )
                # Beide Flags zurücksetzen, damit der nächste Poll-Zyklus
                # Block A (Settings-Tab-Slider) UND Block B (alle anderen Tabs)
                # garantiert durchläuft – unabhängig davon ob sich der gespeicherte
                # Wert numerisch verändert hat.
                _settings_initialized.set(False)
                _last_applied_dur_unit.set({})
                _bump_status()
            elif rv and rv.status_code == 422:
                ui.notification_show(
                    "Ungueltige Eingabe – bitte Werte pruefen.", type="error", duration=4,
                )
            else:
                ui.notification_show("Fehler beim Speichern.", type="error", duration=4)

        # ----- Sync: Settings → UI-Elemente ----------------------------------
        @reactive.effect
        def _sync_settings_to_ui():
            """Synct Settings-Inputs aus dem Backend.

            Zwei Kategorien mit unterschiedlichem Sync-Verhalten:

            A) Nur einmalig beim ersten Load + nach erfolgreichem Save:
               txt_navbar_title, clr_accent_color, sld_max_history,
               sld_slider_max_minutes (inkl. max=), sld_default_duration (inkl. max=),
               rb_default_time_unit
               → _settings_initialized verhindert, dass der User-Input
                 bei jedem 5s-Poll überschrieben wird.
               → Beide Slider sind statische Widgets; max= und value= werden hier
                 genau einmal gesetzt. Kein render.ui = kein Poll-Reset.

            B) Andere Tabs (Ventile/Queue/Schedule-Slider+Radio):
               Nur bei tatsächlicher Wertänderung (_last_applied_dur_unit).
               Aktualisiert nur value= von sld_default_duration, nicht max=.
            """
            d = _settings_data()
            if not d:
                return

            # A) Nur beim ersten Load ────────────────────────────────────────
            if not _settings_initialized.get():
                _settings_initialized.set(True)

                hist = d.get("max_history_items")
                if hist is not None:
                    ui.update_slider("sld_max_history", value=int(hist))

                # Gespeicherter Wert ist reiner Suffix – kein Prefix abschneiden nötig.
                # Sicherheitshalber: falls ein alter Wert mit Prefix gespeichert wurde,
                # diesen entfernen damit das Textfeld nicht "Noria - Hof Muster" zeigt.
                title_raw = d.get("navbar_title", NAVBAR_TITLE_DEFAULT)
                if title_raw.startswith(NAVBAR_PREFIX):
                    title_raw = title_raw[len(NAVBAR_PREFIX):] or NAVBAR_TITLE_DEFAULT
                ui.update_text("txt_navbar_title", value=title_raw)

                color = d.get("accent_color", ACCENT_COLOR_DEFAULT)
                _apply_color_picker(color)

                # sld_slider_max_minutes: max= auf Hardware-Limit, value= auf
                # gespeicherten Wert setzen. Einmalig – kein Poll-Reset.
                hard_max_s   = d.get("hard_max_runtime_s", 3600)
                hard_max_min = max(1, hard_max_s // 60)
                slider_max   = min(max(1, int(d.get("slider_max_minutes", hard_max_min))), hard_max_min)
                ui.update_slider("sld_slider_max_minutes", min=1, max=hard_max_min, value=slider_max)

                # sld_default_duration: max= auf slider_max_minutes, value= auf
                # gespeicherten Wert (bereits serverseitig gegen slider_max gekappt).
                default_dur = min(max(1, int(d.get("default_duration", 5))), slider_max)
                ui.update_slider("sld_default_duration", min=1, max=slider_max, value=default_dur)

            # B) Slider/Radios in allen Tabs: nur bei Wertänderung ───────────
            dur        = int(d.get("default_duration", 5))
            unit       = d.get("default_time_unit", "Minuten")
            slider_max = max(1, int(d.get("slider_max_minutes", 60)))
            last = _last_applied_dur_unit.get()
            # slider_max in die Änderungs-Erkennung einschließen: eine Änderung
            # von slider_max_minutes allein (dur/unit unverändert) muss ebenfalls
            # alle Tabs aktualisieren, da sonst max= auf dem alten Wert stehen bleibt.
            if (last.get("dur") == dur
                    and last.get("unit") == unit
                    and last.get("slider_max") == slider_max):
                return
            _last_applied_dur_unit.set({"dur": dur, "unit": unit, "slider_max": slider_max})

            # value= auf neuen Default, max= auf konfigurierten Slider-Maximalwert.
            # Ohne max= würde Shiny nur den Wert aktualisieren, das Widget-Maximum
            # aber am hartcodierten Initialwert (max=60 / max=120) belassen.
            ui.update_slider("sld_default_duration", min=1, max=slider_max, value=dur)
            ui.update_radio_buttons("rb_default_time_unit", selected=unit)

            # Ventile-Tab: alle Zone-Slider + Radiobuttons
            for i in range(1, ANZAHL_VENTILE + 1):
                ui.update_slider(f"sld_{i}", min=1, max=slider_max, value=dur)
                ui.update_radio_buttons(f"rb_{i}", selected=unit)

            # Warteschlange-Tab
            ui.update_slider("q_add_dur", min=1, max=slider_max, value=dur)
            ui.update_radio_buttons("q_add_unit", selected=unit)

            # Zeitplaene-Tab
            ui.update_slider("sch_dur", min=1, max=slider_max, value=dur)
            ui.update_radio_buttons("sch_unit", selected=unit)

        # Helper: Color-Picker DOM-Wert via JS setzen
        def _apply_color_picker(color: str):
            """Setzt Color-Picker DOM-Wert UND Shinys internen Input-State.

            Nur den DOM-Wert per JS zu setzen reicht nicht: input.clr_accent_color()
            liest den Shiny-State, nicht den DOM-Wert. Shiny.setInputValue synct beide.
            """
            color_js = _json_mod.dumps(color)
            ui.insert_ui(
                selector="body",
                where="beforeEnd",
                ui=ui.tags.script(
                    f"(function(){{"
                    f"  var el=document.getElementById('clr_accent_color');"
                    f"  if(el) el.value={color_js};"
                    f"  Shiny.setInputValue('clr_accent_color', {color_js}, {{priority: 'event'}});"
                    f"}})();"
                ),
                immediate=True,
            )

        # Reset-Button: Farbe auf Code-Default zurücksetzen
        @reactive.effect
        @reactive.event(input.btn_reset_accent)
        def _h_reset_accent():
            _apply_color_picker(ACCENT_COLOR_DEFAULT)

        # Nach erfolgreichem Save: txt + color neu laden (einmalige Ausnahme)
        # Wird durch _bump_status() ausgelöst den _h_save_settings aufruft.
        # Da _settings_initialized True ist, würde B) nicht mehr greifen.
        # Lösung: Save setzt Flag zurück → nächster Poll lädt Werte neu.
        # (Implementierung: _h_save_settings setzt _settings_initialized.set(False))
        # ----- Sensor-Zonen-Zuordnung ------------------------------------
        #
        # Korrekte Loesung fuer das Checkbox-Poll-Problem:
        #
        # _sensor_asgn_state speichert {sensors, assignments} als reactive.Value.
        # @render.ui liest direkt aus _sensor_asgn_state und setzt selected=
        # mit den echten Werten – kein ui.update_* noetig.
        #
        # _sensor_asgn_state aendert sich nur zweimal im Normalfall:
        #   - Beim ersten Load (None → Daten): @render.ui rendert mit richtigen Werten.
        #   - Nach jedem Save (Daten → None → Daten): bestaetigt gespeicherte Werte.
        #
        # Beim regulaeren 5s-Poll aendert sich _sensor_asgn_state NICHT →
        # kein Re-Render, Nutzer-Checkboxen bleiben unveraendert.

        # None = noch nicht geladen; {} = geladen (auch wenn leer)
        _sensor_asgn_state: reactive.Value = reactive.Value(None)

        @reactive.effect
        def _load_asgn_state():
            """Laedt Zuordnungs-Daten einmalig in _sensor_asgn_state.

            Laeuft bei jedem Poll, tut aber nichts solange State gesetzt ist.
            Reset auf None erfolgt nur nach erfolgreichem Save – dann laedt
            dieser Effect beim naechsten Poll die bestaetigt gespeicherten Werte.
            """
            if _sensor_asgn_state.get() is not None:
                return  # bereits geladen – Nutzereingaben nicht ueberschreiben

            cfg  = _sensor_config_data()
            asgn = _sensor_assignments_data()

            # Warten bis beide API-Calls geantwortet haben
            if not cfg or not asgn:
                return

            sensors     = cfg.get("sensors_configured", [])
            assignments = asgn.get("assignments", {})

            _sensor_asgn_state.set({
                "sensors":     sensors,
                "assignments": assignments,
            })

        with ui.card(class_="mt-3"):
            ui.card_header("Sensor-Zonen-Zuordnung")

            ui.tags.p(
                "Legt fest, welche Ventil-Zonen ein Sensor bewaessert. "
                "Ein Sensor kann mehrere Zonen steuern.",
                class_="text-muted small",
                style="margin-bottom:1rem;",
            )

            @render.ui
            def _sensor_assignment_rows():
                """Rendert Zeilen mit KORREKTEN selected-Werten.

                Haengt ausschliesslich an _sensor_asgn_state – NICHT am
                5s-Poll-Calc. Re-rendert nur wenn _sensor_asgn_state sich
                aendert (Erstladen + nach Save). Kein Poll-Reset moeglich.

                selected= wird hier direkt aus den gespeicherten Daten
                gesetzt – kein ui.update_* noetig oder erlaubt.
                """
                state = _sensor_asgn_state.get()

                if state is None:
                    # Noch nicht geladen – neutrales Platzhalter-Element
                    return ui.p("Lade...", class_="text-muted small")

                sensors     = state.get("sensors", [])
                assignments = state.get("assignments", {})

                if not sensors:
                    return ui.p("Keine Sensoren konfiguriert.", class_="text-muted small")

                zone_choices = {str(i): f"Zone {i}" for i in range(1, ANZAHL_VENTILE + 1)}
                rows = []
                for sid in sensors:
                    selected = [str(z) for z in assignments.get(str(sid), [])]
                    rows.append(
                        ui.div(
                            ui.tags.b(f"Sensor {sid}",
                                      style="display:block; margin-bottom:0.35rem;"),
                            ui.input_checkbox_group(
                                f"asgn_sensor_{sid}",
                                label=None,
                                choices=zone_choices,
                                selected=selected,  # echte Werte direkt hier
                                inline=True,
                            ),
                            class_="settings-section",
                            style="margin-bottom:0.5rem;",
                        )
                    )
                return ui.div(*rows)

            ui.input_action_button(
                "btn_save_sensor_assignments",
                "Zuordnung speichern",
                class_="btn btn-outline-secondary w-100 mt-2",
            )

            @reactive.effect
            @reactive.event(input.btn_save_sensor_assignments)
            def _h_save_sensor_assignments():
                cfg     = _sensor_config_data()
                sensors = cfg.get("sensors_configured", []) if cfg else []
                assignments = {}
                for sid in sensors:
                    try:
                        raw = input[f"asgn_sensor_{sid}"]() or []
                        assignments[str(sid)] = [int(z) for z in raw]
                    except Exception:
                        assignments[str(sid)] = []
                rv = _post("/sensors/assignments", json={"assignments": assignments})
                if rv and rv.ok:
                    ui.notification_show(
                        "Sensor-Zuordnung gespeichert.", type="message", duration=3,
                    )
                    # State auf None → _load_asgn_state laedt beim naechsten
                    # Poll die gespeicherten Werte → @render.ui zeigt Bestaetigung.
                    _sensor_asgn_state.set(None)
                    _bump_sensor()
                else:
                    ui.notification_show(
                        "Fehler beim Speichern.", type="error", duration=4,
                    )
