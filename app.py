
# app.py - Bewaesserungscomputer Frontend
# Shiny Express | FastAPI Backend: http://127.0.0.1:8000

# Rate-Limit-Strategie:
#   - Alle shared @reactive.calc (status, automation, parallel) werden
#     auf Seitenebene EINMAL pro Poll-Tick definiert.
#   - Shiny cached @reactive.calc-Ergebnisse innerhalb eines reaktiven
#     Ticks: Egal wie viele Renders darauf zugreifen, es entsteht nur
#     EIN HTTP-Request pro Intervall.
#   - Kein Render darf _get("/status") direkt aufrufen - immer _status_data().


from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

import requests
from shiny import reactive
from shiny.express import input, output, render, ui
from faicons import icon_svg as icon

# --- Konfiguration -----------------------------------------------------------
BASE_URL               = "http://127.0.0.1:8000"
ANZAHL_VENTILE         = 6
POLL_STATUS_S          = 1      # Status-Polling: 1s -> max 60 Req/min (weit unter Limit)
POLL_SLOW_S            = 5      # Queue / Zeitplaene / Verlauf
BACKEND_FAIL_THRESHOLD = 3
HEALTH_TIMEOUT_S       = 0.8

WEEKDAY_CHOICES = {
    "0": "Mo", "1": "Di", "2": "Mi",
    "3": "Do", "4": "Fr", "5": "Sa", "6": "So",
}

# --- API-Key -----------------------------------------------------------------
try:
    _API_KEY = Path("./data/api_key.txt").read_text(encoding="utf-8").strip()
except OSError:
    _API_KEY = ""
    print("WARNUNG: ./data/api_key.txt nicht lesbar - Requests laufen ohne Key.")

_session = requests.Session()
_session.headers.update({"X-API-Key": _API_KEY})

# --- HTTP-Hilfsfunktionen ----------------------------------------------------

def _get(path: str, timeout: float = 2.0) -> requests.Response | None:
    try:
        return _session.get(BASE_URL + path, timeout=timeout)
    except Exception:
        return None

def _post(path: str, json: Any = None, timeout: float = 3.0) -> requests.Response | None:
    try:
        return _session.post(BASE_URL + path, json=json, timeout=timeout)
    except Exception:
        return None

def _delete(path: str, json: Any = None, timeout: float = 3.0) -> requests.Response | None:
    try:
        return _session.delete(BASE_URL + path, json=json, timeout=timeout)
    except Exception:
        return None

def _json_or_none(r: requests.Response | None) -> dict | None:
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        return None

# --- Formatierungshilfsfunktionen --------------------------------------------

def fmt_mmss(total_s: int) -> str:
    m, s = divmod(max(0, int(total_s)), 60)
    return f"{m}:{s:02d}"

def fmt_duration(duration_s: int, time_unit: str = "Sekunden") -> str:
    if time_unit == "Minuten" or duration_s % 60 == 0:
        return f"{duration_s // 60} Min"
    return f"{duration_s} Sek"

def fmt_weekdays(weekdays: list[int]) -> str:
    return ", ".join(WEEKDAY_CHOICES.get(str(w), str(w)) for w in sorted(weekdays))

def state_badge(state_str: str) -> ui.Tag:
    label_map = {
        "laeuft":   ("success",   "Laeuft"),
        "pausiert": ("warning",   "Pausiert"),
        "bereit":   ("secondary", "Bereit"),
        "fertig":   ("info",      "Fertig"),
    }
    # Umlaut-tolerant: "laeuft" matcht auch "lauft" falls Backend ohne Umlaut
    key = (state_str
           .replace("\u00e4", "ae")
           .replace("\u00f6", "oe")
           .replace("\u00fc", "ue"))
    color, label = label_map.get(key, ("secondary", state_str))
    return ui.span(label, class_=f"badge text-bg-{color}")

# --- Reaktive Werte (modul-global, session-scoped durch Shiny Express) -------

_backend_fail_streak = reactive.Value(0)
_backend_ok          = reactive.Value(True)
_backend_modal_open  = reactive.Value(False)

# Manuelle Trigger fuer sofortige UI-Aktualisierung nach Button-Klicks
_status_trigger   = reactive.Value(0)
_queue_trigger    = reactive.Value(0)
_schedule_trigger = reactive.Value(0)
_history_trigger  = reactive.Value(0)

def _bump_status():   _status_trigger.set(_status_trigger.get() + 1)
def _bump_queue():    _queue_trigger.set(_queue_trigger.get() + 1)
def _bump_schedule(): _schedule_trigger.set(_schedule_trigger.get() + 1)
def _bump_history():  _history_trigger.set(_history_trigger.get() + 1)

# --- Backend-Health ----------------------------------------------------------

def _ping_health() -> bool:
    try:
        r = _session.get(BASE_URL + "/health", timeout=HEALTH_TIMEOUT_S)
        return r.status_code == 200 and bool(r.json().get("ok", False))
    except Exception:
        return False

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
            footer=ui.modal_button("OK", class_="btn btn-secondary"),
        )
    )

# --- CSS ---------------------------------------------------------------------

ACCENT = "#82372a"

ui.tags.style(f"""
:root {{ --accent: {ACCENT}; }}

button.bslib-task-button {{
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #fff !important;
}}
button.bslib-task-button:hover {{ filter: brightness(1.15); }}

.btn.btn-primary {{
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
}}
.btn.btn-primary:hover {{ filter: brightness(1.15); }}

.irs--shiny .irs-bar,
.irs--shiny .irs-handle,
.irs--shiny .irs-handle.state-hover {{ background-color: var(--accent); }}
.irs--shiny .irs-single          {{ background-color: var(--accent); color: #fff; }}
.irs--shiny .irs-single:after    {{ border-top-color: var(--accent); }}
.irs--shiny .irs-min,
.irs--shiny .irs-max             {{ color: var(--accent); }}

.shiny-input-radiogroup input[type="radio"]:checked,
.shiny-input-checkboxgroup input[type="checkbox"]:checked {{
    accent-color: var(--accent) !important;
}}

.navbar {{ background-color: {ACCENT} !important; }}
.navbar .navbar-brand, .navbar .nav-link {{ color: #fff !important; }}
.navbar .nav-link.active,
.navbar .nav-link:hover {{
    background-color: rgba(255,255,255,0.15) !important;
    border-radius: 4px;
}}

#nav-clock {{
    color: #fff;
    font-weight: 600;
    font-size: 1.05rem;
    padding: 0.4rem 0.75rem;
    letter-spacing: 0.05em;
}}
.nav-backend-ok  {{color: #a8ffb0 !important; font-size: 0.8rem; padding: 0.4rem 0.75rem;}}
.nav-backend-err {{color: #ffcccb !important; font-size: 0.8rem; padding: 0.4rem 0.75rem; animation: blink 1s step-end infinite;}}
@keyframes blink {{ 50% {{ opacity: 0.4; }} }}

.valve-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
    gap: 1rem;
    padding: 1rem;
}}

.zone-running {{
    background-color: #d4edda;
    border-left: 4px solid #28a745;
    border-radius: 4px;
    padding: 0.35rem 0.6rem;
    margin-bottom: 0.5rem;
    font-size: 0.92rem;
}}

.form-section-title {{
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #666;
    margin: 0.9rem 0 0.3rem;
}}

.fault-banner {{
    background: #f8d7da;
    border: 1px solid #f5c2c7;
    border-radius: 6px;
    padding: 0.7rem 1rem;
    margin-bottom: 1rem;
}}

table.history-table {{ font-size: 0.88rem; }}
table.history-table th {{ background-color: #f5f5f5; }}

.valve-status-area {{
    min-height: 1.8rem;
    padding: 0.2rem 0;
}}
""")

# =============================================================================
# SEITE
# =============================================================================

ui.page_opts(title="", window_title="Bewaesserung", lang="de")

# -----------------------------------------------------------------------------
# GETEILTE REACTIVE CALCS - Seitenebene
#
# Diese Calcs werden EINMAL pro Poll-Intervall ausgefuehrt, egal wie viele
# Renders darauf zugreifen. Shiny cached @reactive.calc innerhalb eines
# reaktiven Ticks. Kein Render darf _get() direkt aufrufen!
# -----------------------------------------------------------------------------

@reactive.calc
def _status_data() -> dict:
    """Einzige Quelle fuer /status - gecached pro reaktivem Tick."""
    reactive.invalidate_later(POLL_STATUS_S)
    _status_trigger.get()   # manuelle Invalidierung nach Button-Klicks
    r = _get("/status")
    if r is None or not r.ok:
        return {}
    return r.json()

@reactive.calc
def _automation_data() -> dict:
    """Gecachter /automation-Fetch."""
    reactive.invalidate_later(POLL_SLOW_S)
    _status_trigger.get()
    return _json_or_none(_get("/automation")) or {}

@reactive.calc
def _parallel_data() -> dict:
    """Gecachter /parallel-Fetch."""
    reactive.invalidate_later(POLL_SLOW_S)
    _status_trigger.get()
    return _json_or_none(_get("/parallel")) or {}

# Globaler Health-Poller (unabhaengig von Status-Calls)
@reactive.effect
def _health_poll():
    reactive.invalidate_later(POLL_STATUS_S)
    ok = _ping_health()
    if ok:
        _backend_fail_streak.set(0)
        _backend_ok.set(True)
        if _backend_modal_open.get():
            _backend_modal_open.set(False)
            ui.modal_remove()
    else:
        streak = _backend_fail_streak.get() + 1
        _backend_fail_streak.set(streak)
        if streak >= BACKEND_FAIL_THRESHOLD:
            _backend_ok.set(False)
            _show_backend_modal()

# =============================================================================
# NAVBAR
# =============================================================================

with ui.navset_bar(title="Bewaesserungscomputer", id="main_nav"):

    # Uhr + Backend-Status im Navbar
    with ui.nav_control():

        @render.ui
        def _nav_clock():
            reactive.invalidate_later(1)
            ok  = _backend_ok.get()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            return ui.div(
                ui.span(now, id="nav-clock"),
                ui.tags.br(),
                ui.span(
                    "Backend OK" if ok else "Backend OFFLINE",
                    class_="nav-backend-ok" if ok else "nav-backend-err",
                ),
                style="text-align:right; line-height:1.3;",
            )

    # =========================================================================
    # TAB 1 - DASHBOARD
    # =========================================================================
    with ui.nav_panel("Dashboard", value="dashboard"):

        # Hardware-Fault-Banner (nur sichtbar wenn hw_faulted)
        @render.ui
        def _fault_banner():
            d = _status_data()
            if not d.get("hw_faulted", False):
                return ui.div()
            reason = d.get("hw_fault_reason", "")
            zone   = d.get("hw_fault_zone", "?")
            return ui.div(
                ui.tags.b("Hardware-Fault"),
                f" - Zone {zone}",
                (f": {reason}" if reason else ""),
                class_="fault-banner",
            )

        # Quittier-Button (nur sichtbar bei Fault)
        @render.ui
        def _fault_clear_btn():
            if not _status_data().get("hw_faulted", False):
                return ui.div()
            return ui.div(
                ui.input_action_button(
                    "btn_fault_clear", "Hardware-Fault quittieren",
                    class_="btn btn-warning mb-3",
                ),
            )

        with ui.layout_columns(col_widths=[8, 4]):

            with ui.card():
                ui.card_header("Systemstatus")

                @render.ui
                def _status_display():
                    # Nutzt den gemeinsamen Calc - kein eigener HTTP-Call!
                    d = _status_data()
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
                        badge = ui.span("Hardware-Fault", class_="badge text-bg-danger")
                    elif paused:
                        badge = ui.span("Pausiert", class_="badge text-bg-warning text-dark")
                    elif active_runs:
                        n = len(active_runs)
                        badge = ui.span(
                            f"Laeuft ({n} Zone{'n' if n > 1 else ''})",
                            class_="badge text-bg-success",
                        )
                    else:
                        badge = ui.span("Bereit", class_="badge text-bg-secondary")

                    zone_divs = []
                    for zk, ar in sorted(active_runs.items(), key=lambda x: int(x[0])):
                        rem = ar.get("remaining_s", 0)
                        src = ar.get("started_source", "manuell")
                        zone_divs.append(
                            ui.div(
                                ui.tags.b(f"Zone {zk}"),
                                f"  -  {fmt_mmss(rem)} verbleibend  (Quelle: {src})",
                                class_="zone-running",
                            )
                        )
                    # Fallback auf Legacy-Felder
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
                        ui.div(badge, style="margin-bottom:0.8rem;"),
                        *zone_divs,
                        ui.tags.hr(),
                        ui.div(
                            ui.tags.small("Warteschlange: ", class_="text-muted"),
                            state_badge(q_state),
                            f" ({q_len} Item{'s' if q_len != 1 else ''})",
                        ),
                        ui.div(
                            ui.tags.small("Automatik: ", class_="text-muted"),
                            ui.span("EIN", class_="badge text-bg-success") if auto
                                else ui.span("AUS", class_="badge text-bg-secondary"),
                            ui.tags.small("   Parallel: ", class_="text-muted"),
                            ui.span("EIN", class_="badge text-bg-info") if parallel
                                else ui.span("AUS", class_="badge text-bg-secondary"),
                            style="margin-top:0.3rem;",
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
    #
    # Korrekte Rate-Limit-Strategie:
    #   - Jeder Ventil-Render ruft _status_data() auf (gecachter Calc)
    #   - KEIN direkter _get("/status") Aufruf in Renders!
    #   - Einen kombinierten Render pro Zone (statt 2x dot + status)
    #   - Statische Karten mit "with ui.card():" auf Modulebene
    # =========================================================================
    with ui.nav_panel("Ventile", value="ventile"):

        with ui.div(class_="valve-grid"):
            for _vi in range(1, ANZAHL_VENTILE + 1):
                with ui.card(id=f"valve_card_{_vi}"):

                    with ui.card_header():
                        with ui.div(
                            style="display:flex; justify-content:space-between; align-items:center;"
                        ):
                            ui.tags.b(f"Zone {_vi}")

                            # Live-Statuspunkt (nutzt gemeinsamen Calc)
                            @output(id=f"valve_dot_{_vi}")
                            @render.ui
                            def _vdot(_z=_vi):
                                d = _status_data()   # gecachter Calc - kein HTTP!
                                is_running = str(_z) in d.get("active_runs", {})
                                color = "#28a745" if is_running else "#ccc"
                                return ui.span(
                                    "●",
                                    style=f"color:{color}; font-size:1.2rem;",
                                )

                    # Kombinierter Live-Status (Zeit + Quelle, ein Render pro Zone)
                    with ui.div(class_="valve-status-area px-3 pt-2"):
                        @output(id=f"valve_status_{_vi}")
                        @render.ui
                        def _vstatus(_z=_vi):
                            d  = _status_data()   # gecachter Calc - kein HTTP!
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

                    # Steuerelemente (Inputs auf Modulebene - Shiny-Pflicht)
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
                        with ui.div(
                            style="display:flex; gap:0.4rem; flex-wrap:wrap; margin-top:0.5rem;"
                        ):
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

        # Handler-Factories (Closure-Bug-Vermeidung)
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
                        style="margin-bottom:0.8rem;",
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
                            class_="table table-sm table-hover",
                        ),
                    )

                with ui.div(style="margin-top:0.8rem; display:flex; gap:0.4rem; flex-wrap:wrap;"):
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

                ui.input_select(
                    "q_add_zone", "Zone:",
                    choices={str(i): f"Zone {i}" for i in range(1, ANZAHL_VENTILE + 1)},
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

        # Handler

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
                ui.notification_show(
                    f"Zone {zone} ({dur} {unit}) hinzugefuegt.",
                    type="message", duration=3,
                )
            else:
                detail = _json_or_none(r) or {}
                ui.notification_show(
                    detail.get("detail", "Fehler beim Hinzufuegen."), type="error", duration=5,
                )
            _bump_queue()

    # =========================================================================
    # TAB 4 - ZEITPLAENE
    # =========================================================================
    with ui.nav_panel("Zeitplaene", value="schedule"):

        _schedule_cache = reactive.Value([])

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
                            ui.span("EIN", class_="badge text-bg-success") if enabled
                            else ui.span("AUS", class_="badge text-bg-secondary")
                        )
                        rows.append(
                            ui.tags.tr(
                                ui.tags.td(
                                    ui.input_checkbox(f"cb_sch_{idx}", None, value=False),
                                    style="width:1%;",
                                ),
                                ui.tags.td(zone_label),
                                ui.tags.td(fmt_weekdays(weekdays)),
                                ui.tags.td(", ".join(times)),
                                ui.tags.td(fmt_duration(dur_s, unit)),
                                ui.tags.td("woechtl." if repeat else "einmalig"),
                                ui.tags.td(status_span),
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
                            class_="table table-sm table-hover",
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
                            style="margin-top:0.7rem; display:flex; gap:0.3rem; flex-wrap:wrap;",
                        ),
                    )

                def _selected_ids() -> list[str]:
                    items = _schedule_cache.get()
                    selected = []
                    for idx in range(len(items)):
                        try:
                            if input[f"cb_sch_{idx}"]():
                                selected.append(items[idx]["id"])
                        except Exception:
                            pass
                    return selected

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
                        ui.notification_show(f"{len(ids)} geloescht.", type="message", duration=3)
                    else:
                        ui.notification_show("Fehler beim Loeschen.", type="error", duration=4)
                    _bump_schedule()

            with ui.card():
                ui.card_header("Zeitplan hinzufuegen")

                ui.input_select(
                    "sch_zone", "Zone:",
                    choices={
                        "0": f"Alle Zonen ({ANZAHL_VENTILE})",
                        **{str(i): f"Zone {i}" for i in range(1, ANZAHL_VENTILE + 1)},
                    },
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

        # Handler

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
    # TAB 5 - VERLAUF
    # =========================================================================
    with ui.nav_panel("Verlauf", value="history"):

        with ui.card():
            with ui.card_header():
                with ui.div(
                    style="display:flex; justify-content:space-between; align-items:center; width:100%;"
                ):
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
                for item in reversed(items):
                    ts   = item.get("ts_end", "")
                    zone = item.get("zone", "?")
                    dur  = item.get("duration_s", 0)
                    src  = item.get("source", "")
                    unit = item.get("time_unit", "Sekunden")
                    try:
                        ts_fmt = ts[:16].replace("T", " ")
                    except Exception:
                        ts_fmt = ts
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
