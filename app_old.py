from dataclasses import dataclass
from pathlib import Path
from shiny import *
from shiny.express import render, ui, input, expressify, output
from faicons import icon_svg as icon
import time as time
import datetime as dt
import locale
import requests


# ---------------------------
# ToDos:
# - Check ob API-Server läuft, sonst Fehlermeldung
# - UI-Verbesserungen
# - Responsives Design testen
# - Check ob alle Meldungen eingebaut und sinnvoll sind
# - Evtl. WebSocket für Statusupdates statt Polling
# - POST /fault/clear -> Quittiert Hardware-Fault (Operator-Ack) integrieren, damit nach Fehlern wieder Bewässerung möglich ist (derzeit muss main.py neu gestartet werden)
# ---------------------------

# ---------------------------
# Konfiguration
# ---------------------------

BASE_URL = "http://127.0.0.1:8000"

anzahl_ventile = 6

# ---------------------------
# API-Key Authentifizierung
# ---------------------------

try:
    _API_KEY = Path("./data/api_key.txt").read_text(encoding="utf-8").strip()
except OSError:
    _API_KEY = ""
    print("WARNUNG: ./data/api_key.txt nicht lesbar – Requests werden mit leerem Key gesendet.")

_session = requests.Session()
_session.headers.update({"X-API-Key": _API_KEY})


@dataclass
class ApiResponse:
    status_code: int
    data: dict | list | None = None
    error: str | None = None

    def json(self) -> dict | list:
        if self.data is None:
            return {}
        return self.data


REQUEST_TIMEOUT_S = 2.0


def api_request(method: str, path: str, *, json: dict | list | None = None, timeout: float = REQUEST_TIMEOUT_S) -> ApiResponse:
    try:
        response = _session.request(method, BASE_URL + path, json=json, timeout=timeout)
    except requests.RequestException as exc:
        return ApiResponse(status_code=0, error=str(exc))

    try:
        data = response.json()
    except ValueError:
        data = None

    return ApiResponse(status_code=response.status_code, data=data)


def api_get(path: str, *, timeout: float = REQUEST_TIMEOUT_S) -> ApiResponse:
    return api_request("GET", path, timeout=timeout)


def api_post(path: str, *, json: dict | list | None = None, timeout: float = REQUEST_TIMEOUT_S) -> ApiResponse:
    return api_request("POST", path, json=json, timeout=timeout)


def api_delete(path: str, *, json: dict | list | None = None, timeout: float = REQUEST_TIMEOUT_S) -> ApiResponse:
    return api_request("DELETE", path, json=json, timeout=timeout)

# ---------------------------
# CSS-Anpassungen
# ---------------------------

ui.tags.style("""
/* Task Button */
button.bslib-task-button {
    background-color: #82372a !important;
    border-color: #82372a !important;
    color: white;
}             

/* Radio: Punkt + Rahmen */
.shiny-input-radiogroup input[type="radio"]:checked {
    accent-color: #82372a !important;
    background-color: #82372a !important;
    border-color: #82372a !important;
}
              
/* Slider-Leiste (Hintergrund) */
.irs--shiny .irs-line {
    background-color: #e6e6e6;
}

/* Aktiver Bereich (gefüllter Teil) */
.irs--shiny .irs-bar {
    background-color: #82372a;
}

/* Griff (Handle) */
.irs--shiny .irs-handle {
    background-color: #82372a;
}

/* Griff (Handle) hover */
.irs--shiny .irs-handle:hover {
    background-color: #82372a;
}              

/* Griff (Handle) state-hover */
.irs--shiny .irs-handle.state-hover {
    background-color: #82372a;
}               

/* Griff (Handle) active */
.irs--shiny .irs-handle:active {
    background-color: #82372a;              
}
              
/* Aktueller Wert (Zahl über dem Slider) */
.irs--shiny .irs-single {
    background-color: #82372a;
    color: white;
}

/* Pfeil unter der Zahl */
.irs--shiny .irs-single:after {
    border-top-color: #82372a;
}

/* Min / Max Werte */
.irs--shiny .irs-min,
.irs--shiny .irs-max {
    color: #82372a;
}
"""
)

ui.tags.style("""
.shiny-input-checkboxgroup .checkbox label {
    white-space: nowrap;
}
""")

ui.tags.style("""
/* NUR Tabellen mit Checkbox-Spalte */
table.table-with-checkbox td:first-child {
  width: 1%;
  white-space: nowrap;
  padding-right: .4rem !important;
}

/* Shiny Input-Container nur dort überschreiben */
table.table-with-checkbox td:first-child
.shiny-input-container:not(.shiny-input-container-inline) {
  width: auto !important;
}

/* Wrapper-Abstände nur dort entfernen */
table.table-with-checkbox td:first-child
.form-group.shiny-input-container {
  margin: 0 !important;
  padding: 0 !important;
}

table.table-with-checkbox td:first-child .checkbox {
  margin: 0 !important;
}

table.table-with-checkbox td:first-child .checkbox label {
  margin: 0 !important;
  padding: 0 !important;
}
""")

ui.tags.style("""
.nowrap {
    white-space: nowrap !important;
}
""")


# ---------------------------
# Hilfsfunktionen
# ---------------------------

def format_mmss(total_seconds: int) -> str:
    """
    Wandelt eine Anzahl Sekunden in 'M:SS' um (z.B. 321 -> '5:21').
    """
    if total_seconds < 0:
        raise ValueError("total_seconds muss >= 0 sein")

    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def render_active_runs_table(active_runs: dict, paused: bool) -> str:
    if not active_runs:
        return "<i>Keine Ventile aktiv</i>"

    def fmt_s(sec: int) -> str:
        sec = int(sec)
        if sec >= 60:
            return f"{sec//60}m {sec%60:02d}s" if sec % 60 else f"{sec//60}m"
        return f"{sec}s"

    def src_label(src: str) -> str:
        return {"manual": "Manuell", "queue": "Queue", "schedule": "Automatik"}.get(src, src)

    rows = []
    for zone, r in sorted(active_runs.items()):
        pause_icon = "⏸️" if paused else ""
        rows.append(f"""
        <tr>
          <td><b>{zone}</b></td>
          <td>{pause_icon} {fmt_s(r.get("remaining_s", 0))}</td>
          <td>{fmt_s(r.get("planned_s", 0))}</td>
          <td>{src_label(r.get("started_source", ""))}</td>
        </tr>
        """)

    return f"""
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
      <thead>
        <tr style="border-bottom:2px solid #ddd;">
          <th>Ventil</th>
          <th>Noch</th>
          <th>Geplant</th>
          <th>Start</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def render_queue_table(queue_items: list, queue_state: str) -> str:
    if not queue_items:
        return "<i>Keine Einträge in der Warteschlange</i>"

    def fmt_s(sec: int) -> str:
        sec = int(sec)
        if sec >= 60:
            return f"{sec//60}m {sec%60:02d}s" if sec % 60 else f"{sec//60}m"
        return f"{sec}s"

    def src_label(src: str) -> str:
        return {"manual": "Manuell", "queue": "Queue", "schedule": "Automatik"}.get(src, src)

    paused_icon = "⏸️" if queue_state == "pausiert" else ""

    rows = []
    for i, q in enumerate(queue_items, start=1):
        rows.append(f"""
        <tr>
          <td>{i}</td>
          <td><b>{q["zone"]}</b></td>
          <td>{fmt_s(q["duration"])}</td>
          <td>{src_label(q.get("source", ""))}</td>
        </tr>
        """)

    return f"""
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
      <thead>
        <tr style="border-bottom:2px solid #ddd;">
          <th>#</th>
          <th>Ventil</th>
          <th>Dauer</th>
          <th>Quelle {paused_icon}</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


WEEKDAY_NAMES = {
    0: "Montag",
    1: "Dienstag",
    2: "Mittwoch",
    3: "Donnerstag",
    4: "Freitag",
    5: "Samstag",
    6: "Sonntag",
}


automation_status = reactive.Value(False)


# ---------------------------
# Backend-Health / Debounce
# ---------------------------

backend_ok = reactive.Value(True)
_backend_fail_streak = reactive.Value(0)
_backend_modal_open = reactive.Value(False)

BACKEND_FAIL_THRESHOLD = 3   # erst nach 3 Fehlschlägen "down" melden (Debounce)
HEALTH_TIMEOUT_S = 0.8       # kurz halten, damit UI nicht hängt


def ping_health() -> bool:
    """
    True, wenn Backend erreichbar (HTTP 200 + ok==True).
    Niemals Exceptions nach außen werfen.
    """
    try:
        r = api_get("/health", timeout=HEALTH_TIMEOUT_S)
        if r.status_code != 200:
            return False
        data = r.json()
        return bool(data.get("ok", False))
    except Exception:
        return False


def show_backend_down_modal():
    # Wichtig: nicht mehrfach öffnen
    if _backend_modal_open.get():
        return

    m = ui.modal(
        ui.tags.div(
            ui.tags.p("Das Backend (main.py) ist aktuell nicht erreichbar."),
            ui.tags.p("Bitte Backend starten oder Netzwerk/URL prüfen."),
            ui.tags.hr(),
            ui.tags.p(ui.tags.b("Tipp:"), " Prüfe: uvicorn main:app --host 0.0.0.0 --port 8000"),
        ),
        title="ERROR: Backend nicht erreichbar",
        easy_close=False,
        footer=None,
        size="m",
    )
    ui.modal_show(m)
    _backend_modal_open.set(True)


def close_backend_modal_if_open():
    if _backend_modal_open.get():
        ui.modal_remove()
        _backend_modal_open.set(False)


@reactive.Effect
def backend_health_watcher():
    reactive.invalidate_later(0.5)

    ok = ping_health()

    if ok:
        _backend_fail_streak.set(0)
        if not backend_ok.get():
            # Reconnect: Modal schließen
            backend_ok.set(True)
            close_backend_modal_if_open()
    else:
        _backend_fail_streak.set(_backend_fail_streak.get() + 1)
        if backend_ok.get() and _backend_fail_streak.get() >= BACKEND_FAIL_THRESHOLD:
            backend_ok.set(False)
            show_backend_down_modal()


# ---------------------------
# Shiny App
# ---------------------------

#ui.page_opts(title="Bewässerungssteuerung")

with ui.layout_columns(col_widths=[12,12,6,6,4,4,4,4,4,4,4,8,12]):

    @render.ui
    def date():
        locale.setlocale(locale.LC_TIME, "de_DE.utf8")
        reactive.invalidate_later(0.25)
        return ui.HTML(f"<b>{dt.datetime.now().strftime('%A, %d.%m.%y<br>%H:%M:%S')}</b>")
    
    with ui.card():     
       
        @render.ui
        def show_automation_status():
            reactive.invalidate_later(1)
            response = api_get("/status")
            if response.status_code == 200:
                data = response.json()
                if data['parallel_enabled']:
                    ui.update_switch("switch_concurrent", label="Parallelbetrieb aktiviert", value=True)
                elif data['parallel_enabled'] == False:
                    ui.update_switch("switch_concurrent", label="Parallelbetrieb deaktiviert", value=False)
                if data['automation_enabled']:
                    automation_status.set(True)
                    ui.update_action_button("btn_enable_automation", disabled=True)
                    ui.update_action_button("btn_disable_automation", disabled=False)
                    return ui.HTML("<span><b>Automatik: </b>""<span style='color: #28a745; font-weight: 600;'>aktiviert</span>""</span>")
                else:
                    ui.update_action_button("btn_enable_automation", disabled=False)
                    ui.update_action_button("btn_disable_automation", disabled=True)
                    automation_status.set(False)
                    return ui.HTML(f"<span><b>Automatik: </b>""<span style='color: #dc3545; font-weight: 600;'>deaktiviert</span>""</span>")   
            else:
                return "Fehler bei der Anfrage an den Server!"

        ui.input_switch("switch_concurrent", "Parallelbetrieb aktivieren", value=False)
        @reactive.Effect
        @reactive.event(input["switch_concurrent"])
        def toggle_parallel():
            new_value = input.switch_concurrent()
            response = api_post("/parallel", json={"enabled": new_value})
            if response.status_code == 200:
                if new_value:
                    ui.notification_show("Parallelbetrieb aktiviert!", duration=3, close_button=False, type="message")
                else:
                    ui.notification_show("Parallelbetrieb deaktiviert!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Ändern des Parallelbetriebs.", duration=3, close_button=False, type="error")
  

    with ui.card(max_height="350px"):
        ui.card_header("Status:")
        
        @render.ui
        def status():
            reactive.invalidate_later(0.5)
            response = api_get("/status")
            if response.status_code == 200:
                data = response.json()
                if data['state'] == "bereit":
                    ui.update_action_button("btn_cancel_active", disabled=True)
                    ui.update_action_button("btn_stop_active", disabled=True)
                    ui.update_action_button("btn_resume_active", disabled=True)
                    for i in range(1,anzahl_ventile + 1):
                        ui.update_task_button(f"start{i}", state = "ready")
                    return ui.HTML("<span><b>Keine Bewässerung aktiv</b></span>")
                else:
                    ui.update_action_button("btn_cancel_active", disabled=False)
                    ui.update_action_button("btn_stop_active", disabled=False)
                    if data['paused'] == True:
                        ui.update_action_button("btn_resume_active", disabled=False)
                        ui.update_action_button("btn_stop_active", disabled=True)
                    else:
                        ui.update_action_button("btn_stop_active", disabled=False)
                        ui.update_action_button("btn_resume_active", disabled=True)
                    return ui.HTML(render_active_runs_table(data.get('active_runs', {}), data.get('paused', False)))
            else:  
                return "Fehler bei der Anfrage an den Server!"

        ui.card_footer(    
            ui.input_action_button(id="btn_resume_active", label=None, icon=icon("play"), disabled=True, title="Bewässerung fortsetzen"),
            ui.input_action_button(id="btn_stop_active", label=None, icon=icon("pause"), disabled=True, title="Bewässerung pausieren"),
            ui.input_action_button(id="btn_cancel_active", label="Bewässerung stoppen", disabled=True, icon=icon("stop"), title="Aktive Bewässerung stoppen")
        )
        
        @reactive.Effect
        @reactive.event(input["btn_resume_active"])
        def button_resume_active():
            response = api_post("/resume")
            if response.status_code == 200:
                ui.notification_show("Bewässerung fortgesetzt!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Fortsetzen der Bewässerung.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_stop_active"])
        def button_stop_active():
            response = api_post("/pause")
            if response.status_code == 200:
                ui.notification_show("Bewässerung pausiert!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Pausieren der Bewässerung.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_cancel_active"])
        def button_cancel_active():
            response = api_post("/stop")
            if response.status_code == 200:
                ui.notification_show("Bewässerung gestoppt!", duration=3, close_button=False, type="message")
                #ui.update_action_button("btn_cancel_active", disabled=True)
            else:
                ui.notification_show("Fehler beim Stoppen der Bewässerung.", duration=3, close_button=False, type="error")

    
    with ui.card(full_screen=True, max_height="350px"):
        ui.card_header("Warteschlange:")
            
        @render.ui
        def queue_status():
            reactive.invalidate_later(0.5)
            response = api_get("/queue")
            if response.status_code == 200:
                data = response.json()
                if data['queue_length'] == 0:
                    ui.update_task_button("btn_q_start", state = "ready")
                    ui.update_action_button("btn_q_stop", disabled=True)
                    ui.update_action_button("btn_q_clear", disabled=True)
                else:
                    ui.update_task_button("btn_q_start", state = "busy" if data['queue_state'] == "läuft" else "ready")
                    ui.update_action_button("btn_q_stop", disabled=False)
                    ui.update_action_button("btn_q_clear", disabled=False)
                return ui.HTML(render_queue_table(data.get('items', []), data.get('queue_state', 'bereit')))
            else:  
                return "Fehler bei der Anfrage an den Server!"
        ui.card_footer(
            ui.input_task_button(id="btn_q_start", label="Warteschlange starten", label_busy="Warteschlange läuft...", auto_reset=False, icon=icon("play")),
            ui.input_action_button(id="btn_q_stop", label=None, icon=icon("pause"), disabled=True, title="Warteschlange pausieren"),
            ui.input_action_button(id="btn_q_clear", label=None, icon=icon("trash"), disabled=True, title="Warteschlange löschen")
        )

        @reactive.Effect
        @reactive.event(input["btn_q_start"])
        def button_q_start():
            response = api_post("/queue/start")
            if response.status_code == 200:
                ui.notification_show("Warteschlange gestartet!", duration=3, close_button=False, type="message")
            else:
                if response.status_code == 400:
                    data = response.json()
                    ui.notification_show(f"{data['detail']}", duration=3, close_button=False, type="warning")
                    ui.update_task_button("btn_q_start", state = "ready")
                else:
                    ui.notification_show("Fehler beim Starten der Warteschlange.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_q_stop"])
        def button_q_stop():
            response = api_post("/queue/pause")
            if response.status_code == 200:
                ui.notification_show("Warteschlange pausiert!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Pausieren der Warteschlange.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_q_clear"])
        def button_q_clear():
            response = api_post("/queue/clear")
            if response.status_code == 200:
                ui.notification_show("Warteschlange gelöscht!", duration=3, close_button=False, type="message")
                ui.update_action_button("btn_q_clear", disabled=True)
            else:
                ui.notification_show("Fehler beim Löschen der Warteschlange.", duration=3, close_button=False, type="error")

    for i in range(1,anzahl_ventile + 1):

        with ui.card():
            ui.card_header(f"Ventil {i}")
            ui.input_slider(id=f"sld_time_{i}", label="Dauer:", min=1, max=60, value=5)
            ui.input_radio_buttons(id=f"rb_secmin{i}", label=None, choices=["Minuten", "Sekunden"], inline=True)
            ui.input_task_button(id=f"start{i}", label="Bewässerung starten", label_busy="Bewässerung läuft...", auto_reset=False, icon=icon("play"))
            ui.input_action_button(id=f"queue{i}", label="In die Warteschlange...")

            @reactive.Effect
            @reactive.event(input[f"start{i}"])
            def button_start(i=i):
                start_payload = {"zone": i, "duration": input[f"sld_time_{i}"]() if input[f"rb_secmin{i}"]() == "Sekunden" else input[f"sld_time_{i}"]() * 60, "time_unit": input[f"rb_secmin{i}"]()}
                response = api_post("/start", json=start_payload)
                if response.status_code == 200:
                    ui.notification_show("Bewässerung gestartet!", duration=3, close_button=False, type="message")
                    #ui.update_action_button("btn_cancel_active", disabled=False)
                else:
                    if response.status_code == 409:
                        data = response.json()
                        print(data)
                        ui.notification_show(f"{data['detail']}", duration=3, close_button=False, type="warning")
                        ui.update_task_button(f"start{i}", state = "ready")
                    elif response.status_code == 400:
                        data = response.json()
                        ui.notification_show(f"{data['detail']}", duration=3, close_button=False, type="error")
                    elif response.status_code == 500:
                        data = response.json()
                        ui.notification_show(f"Serverfehler: {data['detail']}", duration=3, close_button=False, type="error")
                    else:
                        ui.notification_show(f"Fehler beim Starten von Ventil {i}.", duration=3, close_button=False, type="error")

            
            @reactive.Effect
            @reactive.event(input[f"queue{i}"])
            def button_queue(i=i):
                queue_payload = {"zone": i, "duration": input[f"sld_time_{i}"]() if input[f"rb_secmin{i}"]() == "Sekunden" else input[f"sld_time_{i}"]() * 60, "time_unit": input[f"rb_secmin{i}"]()}
                response = api_post("/queue/add", json=queue_payload)
                if response.status_code == 200:
                    ui.notification_show("Eintrag zur Warteschlange hinzugefügt!", duration=3, close_button=False, type="message")
                else:
                    if response.status_code == 400:
                        data = response.json()
                        ui.notification_show(f"{data['detail']}", duration=3, close_button=False, type="error")
                    else:
                        ui.notification_show(f"Fehler beim Hinzufügen zur Warteschlange von Ventil {i}.", duration=3, close_button=False, type="error")


    with ui.card():
        ui.card_header("Aufgaben")
        ui.input_select("select_schedule_valve", "Ventil auswählen:", choices={0: "Alle Ventile", **{f"{i}": f"Ventil {i}" for i in range(1, anzahl_ventile + 1)}})
        ui.input_slider("sld_schedule_duration", "Dauer:", min=1, max=60, value=10)
        ui.input_radio_buttons("rb_schedule_secmin", label=None, choices=["Minuten", "Sekunden"], inline=True)
        ui.input_checkbox_group("checkbox_schedule_days", "Tage auswählen:", {"0":"Montag", "1":"Dienstag", "2":"Mittwoch", "3":"Donnerstag", "4":"Freitag", "5":"Samstag", "6":"Sonntag"})
        ui.input_checkbox("chk_schedule_all_days", "Alle Tage auswählen")
        ui.input_text("txt_schedule_time", "Startzeit (HH:MM, 24h):", value="10:00")
        ui.input_radio_buttons("rb_schedule_repeat", "Wiederholung:", choices={"false":"Einmalig", "true":"Wöchentlich"}, inline=True)
        ui.input_action_button("btn_schedule_add", "Zeitplan hinzufügen", icon=icon("plus"))

        @reactive.Effect
        @reactive.event(input["chk_schedule_all_days"])
        def check_all_days():
            if input["chk_schedule_all_days"]():
                ui.update_checkbox_group("checkbox_schedule_days", selected=["0","1","2","3","4","5","6"])
            else:
                ui.update_checkbox_group("checkbox_schedule_days", selected=[])

        @reactive.Effect
        @reactive.event(input["btn_schedule_add"])
        def button_schedule_add():
            selected_zone = int(input["select_schedule_valve"]())
            selected_duration = input["sld_schedule_duration"]() if input["rb_schedule_secmin"]() == "Sekunden" else input["sld_schedule_duration"]() * 60
            selected_time_unit = input["rb_schedule_secmin"]()
            selected_days = [int(day) for day in input["checkbox_schedule_days"]()]
            selected_time = input["txt_schedule_time"]()
            selected_repeat = input["rb_schedule_repeat"]() == "true"

            if not selected_days:
                ui.notification_show("Bitte mindestens einen Tag auswählen!", duration=3, close_button=False, type="error")
                return

            schedule_payload = {
                "zone": selected_zone,
                "duration_s": selected_duration,
                "time_unit": selected_time_unit,
                "weekdays": selected_days,
                "start_times": [selected_time],
                "repeat": selected_repeat
            }
            print(schedule_payload)
            response = api_post("/schedule/add", json=schedule_payload)
            if response.status_code == 200:
                ui.notification_show("Zeitplan hinzugefügt!", duration=3, close_button=False, type="message")
            else:
                if response.status_code == 400:
                    data = response.json()
                    ui.notification_show(f"{data['detail']}", duration=3, close_button=False, type="error")
                else:
                    ui.notification_show("Fehler beim Hinzufügen des Zeitplans.", duration=3, close_button=False, type="error")


    with ui.card():
        ui.card_header("Zeitpläne:")
        
        @render.ui
        def shedule_header():
            reactive.invalidate_later(1)
            auto = automation_status.get()
            if auto == True:
                return ui.HTML("<span><b>Automatik: </b>""<span style='color: #28a745; font-weight: 600;'>aktiviert</span>""</span>")
            elif auto == False:
                return ui.HTML(f"<span><b>Automatik: </b>""<span style='color: #dc3545; font-weight: 600;'>deaktiviert</span>""</span>")
            else:
                return ui.HTML("Fehler bei der Anfrage an den Server!")

        @render.ui
        def schedule_status():
            reactive.invalidate_later(0.5)
            response = api_get("/schedule")

            if response.status_code != 200:
                return "Fehler bei der Anfrage an den Server!"

            data = response.json()
            schedules_count = data["count"]

            if schedules_count == 0:
                ui.update_action_button("btn_schedule_delete", disabled=True)
                return ui.tags.div("Keine Zeitpläne vorhanden.")

            ui.update_action_button("btn_schedule_delete", disabled=False)

            rows = []
            for item in data["items"]:
                days_str = ", ".join(WEEKDAY_NAMES[d] for d in item["weekdays"])
                times_str = " / ".join(sorted(f"{t} Uhr" for t in item["start_times"]))
                repeat_str = "Einmalig" if item["repeat"] is False else "Wöchentlich"

                if item["time_unit"] == "Minuten":
                    duration_str = f"{format_mmss(item['duration_s'])} {item['time_unit']}"
                else:
                    duration_str = f"{item['duration_s']} {item['time_unit']}"

                rows.append(
                    ui.tags.tr(
                        ui.tags.td("Alle Ventile" if item["zone"] == 0 else f"Ventil {item['zone']}", class_="nowrap"),
                        ui.tags.td("Jeden Tag" if days_str == ", ".join(WEEKDAY_NAMES[d] for d in range(7)) else days_str),
                        ui.tags.td(times_str, class_="nowrap"),
                        ui.tags.td(duration_str, class_="nowrap"),
                        ui.tags.td(repeat_str, class_="nowrap"),
                    )
                )

            return ui.tags.table(
                ui.tags.thead(
                    ui.tags.tr(
                        ui.tags.th("Ventil"),
                        ui.tags.th("Tage"),
                        ui.tags.th("Zeiten"),
                        ui.tags.th("Dauer"),
                        ui.tags.th("Wiederholung"),
                    )
                ),
                ui.tags.tbody(*rows),
                class_="table table-striped table-hover",
            )

        ui.card_footer(
        ui.input_action_button("btn_schedule_delete", "Zeitpläne löschen", icon=icon("trash"), disabled=True),
        ui.input_action_button("btn_enable_automation", "Automatik aktivieren", icon=icon("toggle-on")),
        ui.input_action_button("btn_disable_automation", "Automatik deaktivieren", icon=icon("toggle-off")),
        )
       
        @reactive.Effect
        @reactive.event(input["btn_enable_automation"])
        def button_enable_automation():
            response = api_post("/automation/enable")
            if response.status_code == 200:
                ui.notification_show("Automatik aktiviert!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Aktivieren der Automatik.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_disable_automation"])
        def button_disable_automation():
            response = api_post("/automation/disable")
            if response.status_code == 200:
                ui.notification_show("Automatik deaktiviert!", duration=3, close_button=False, type="message")
            else:
                ui.notification_show("Fehler beim Deaktivieren der Automatik.", duration=3, close_button=False, type="error")

        @reactive.Effect
        @reactive.event(input["btn_schedule_delete"])
        def button_schedule_delete():
            response = api_get("/schedule")
            if response.status_code == 200:
                data = response.json()
                schedules_count = data["count"]

                if schedules_count == 0:
                    body = ui.tags.div("Keine Zeitpläne vorhanden.")
                else:
                    rows = []

                    for item in data["items"]:
                        days_str = ", ".join(WEEKDAY_NAMES[d] for d in item["weekdays"])
                        times_str = " / ".join(f"{t} Uhr" for t in sorted(item["start_times"]))
                        repeat_str = "Einmalig" if item["repeat"] == False else "Wöchentlich"

                        if item["time_unit"] == "Minuten":
                            duration_str = f"{format_mmss(item['duration_s'])} {item['time_unit']}"
                        else:
                            duration_str = f"{item['duration_s']} {item['time_unit']}"

                        rows.append(
                            ui.tags.tr(
                                ui.tags.td(
                                    ui.input_checkbox(
                                        f"cb_del_{item['id']}",
                                        label=None
                                    )
                                ),
                                ui.tags.td(f"Ventil {item['zone']}", class_="nowrap"),
                                ui.tags.td(days_str),
                                ui.tags.td(times_str, class_="nowrap"),
                                ui.tags.td(duration_str, class_="nowrap"),
                                ui.tags.td(repeat_str, class_="nowrap"),
                            )
                        )

                    body = ui.tags.table(
                        ui.tags.thead(
                            ui.tags.tr(
                                ui.tags.th(""),
                                ui.tags.th("Ventil"),
                                ui.tags.th("Tage"),
                                ui.tags.th("Zeiten"),
                                ui.tags.th("Dauer"),
                                ui.tags.th("Wiederholung"),
                            )
                        ),
                        ui.tags.tbody(*rows),
                        class_="table table-striped table-hover table-with-checkbox"
                    )


                m = ui.modal(
                    body,
                    size="xl",
                    title="Zeitpläne löschen",
                    footer=(
                        ui.input_action_button(
                            "btn_confirm_delete_schedules",
                            "Ausgewählte löschen",
                            class_="btn-danger"
                        ),
                        ui.modal_button("Schließen"),
                    ),
                )
            else:
                m = ui.modal("Fehler bei der Anfrage an den Server!")

            ui.modal_show(m)

        
        @reactive.Effect
        @reactive.event(input["btn_confirm_delete_schedules"])
        def button_confirm_delete_schedules():
            response = api_get("/schedule")
            if response.status_code == 200:
                data = response.json()
                ids_to_delete = []

                for item in data["items"]:
                    checkbox_id = f"cb_del_{item['id']}"

                    if checkbox_id in input and input[checkbox_id]():
                        ids_to_delete.append(item["id"])

                if ids_to_delete:
                    print("Zu löschende IDs:", ids_to_delete)
                    delete_response = api_delete("/schedule", json=ids_to_delete)
                    if delete_response.status_code == 200:
                        ui.notification_show("Ausgewählte Zeitpläne gelöscht!", duration=3, close_button=False, type="message")
                    else:
                        ui.notification_show("Fehler beim Löschen der Zeitpläne.", duration=3, close_button=False, type="error")
                else:
                    ui.notification_show("Keine Zeitpläne ausgewählt.", duration=3, close_button=False, type="warning")

                ui.modal_remove()
            else:
                ui.notification_show("Fehler bei der Anfrage an den Server!", duration=3, close_button=False, type="error")


    with ui.card(max_height="400px"):
        ui.card_header("Bewässerungsverlauf:")
        
        @render.ui
        def history_status():
            reactive.invalidate_later(1)
            response = api_get("/history")
            if response.status_code == 200:
                data = response.json()
                history_count = data["count"]

                if history_count == 0:
                    return ui.tags.div("Keine Einträge im Bewässerungsverlauf.")

                rows = []
                for item in data["items"]:
                    timestamp_str = dt.datetime.fromisoformat(item["ts_end"]).strftime("%d.%m.%Y - %H:%M Uhr")
                    if item["time_unit"] == "Minuten":
                        duration_str = f"{format_mmss(item['duration_s'])} {item['time_unit']}"
                    else:
                        duration_str = f"{item['duration_s']} {item['time_unit']}"
                    rows.append(
                        ui.tags.tr(
                            ui.tags.td(timestamp_str, class_="nowrap"),
                            ui.tags.td(f"Ventil {item['zone']}", class_="nowrap"),
                            ui.tags.td(duration_str, class_="nowrap"),
                            ui.tags.td(item['source'], class_="nowrap"),
                        )
                    )

                return ui.tags.table(
                    ui.tags.thead(
                        ui.tags.tr(
                            ui.tags.th("Zeitpunkt"),
                            ui.tags.th("Ventil"),
                            ui.tags.th("Dauer"),
                            ui.tags.th("Quelle"),
                        )
                    ),
                    ui.tags.tbody(*rows),
                    class_="table table-striped table-hover",
                )
            else:  
                return "Fehler bei der Anfrage an den Server!"
