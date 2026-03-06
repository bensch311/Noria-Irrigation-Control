# ARCHITECTURE.md – Systemarchitektur

Technische Tiefenbeschreibung von Noria: Thread-Modell, Concurrency-Design, State-Machine, Persistenz und Sicherheitsschichten.

---

## 1. Systemübersicht

Das System besteht aus zwei unabhängigen Prozessen, die über HTTP kommunizieren:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Backend-Prozess                             │
│                                                                     │
│  uvicorn (ASGI)                                                     │
│  ├── FastAPI App (main.py)                                          │
│  │   ├── Middleware-Stack (Security → CORS → RateLimit)             │
│  │   └── Router (health, control, queue, schedule, history, settings│
│  │                                                                  │
│  ├── timer_loop          (Thread – schließt Ventile nach Ablauf)    │
│  ├── scheduler_loop      (Thread – prüft Zeitpläne, startet Queue)  │
│  ├── persistence_loop    (Thread – schreibt dirty state alle 2s)    │
│  ├── io_worker           (Thread – serialisiert alle GPIO-Ops)      │
│  └── watchdog_loop       (Thread – sendet WATCHDOG=1 an systemd)    │
│                                                                     │
│  Globaler Zustand: state (RunState) + state_lock (threading.Lock)  │
└─────────────────────────────────────────────────────────────────────┘
          ▲ HTTP (X-API-Key)
          │
┌─────────────────────────────────────────────────────────────────────┐
│                        Frontend-Prozess                             │
│                                                                     │
│  Shiny Express (app.py)                                             │
│  ├── HTTP-Polling /status alle 1s                                   │
│  └── HTTP-Polling /schedule, /history, /settings alle 5s           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Thread-Modell

Das Backend verwendet 5 + 1 Threads (zusätzlich zum uvicorn ASGI-Thread-Pool):

### 2.1 `timer_loop` (`services/timer.py`)

**Zweck:** Schließt Ventile nach Ablauf ihrer Laufzeit. Verarbeitet Hardware-Fehler mit exponentiellem Backoff. Füllt die Queue auf (startet nächste Items). Erkennt Queue-Ende.

**Takt:** Alle 100ms (`shutdown_event.wait(0.1)`)

**Ablauf pro Iteration (Prepare / Execute / Commit):**

```
Schritt 1 (unter Lock):   Queue-Fill: Items sammeln, die gestartet werden müssen
Schritt 2 (ohne Lock):    Queue-Items starten (start_queue_item → io_worker)
Schritt 3 (unter Lock):   Abgelaufene Zonen (finished_zones) ermitteln
Schritt 4 (ohne Lock):    io_worker.send_command("close", zone) pro Zone
Schritt 5 (unter Lock):   Ergebnisse committen: History schreiben, active_runs bereinigen,
                          Fault-Latch prüfen, Legacy-Felder syncen, Queue-fertig-Check
Schritt 6 (ohne Lock):    Notfall close_all() wenn Fault neu gelatcht (einmalig)
```

**Hardware-Fault-Logik:**  
Wenn `close()` fehlschlägt, wird `hw_close_failures` für die Zone inkrementiert und `hw_next_retry_at` auf `jetzt + backoff_s` gesetzt. Nach `HW_CLOSE_MAX_RETRIES = 5` Fehlversuchen: Fault-Latch (`hw_faulted = True`).

Backoff: `min(30s, 1s × 2^failures)` → 1s, 2s, 4s, 8s, 16s, 30s

### 2.2 `scheduler_loop` (`services/scheduler.py`)

**Zweck:** Prüft periodisch alle aktivierten Zeitpläne. Wenn ein Zeitplan zur aktuellen Zeit ausgelöst werden soll, werden die entsprechenden Zonen in die Queue eingereiht und die Queue gestartet.

**Takt:** Jede Minute (schlafende Schleife mit `shutdown_event.wait(60)`)

**Einmal-Regeln (`repeat=False`):**  
Beim ersten Erstellen werden die Auslösungen in `once_pending` gespeichert (als `"Wochentag HH:MM"`-Strings). Nach Auslösung wird der entsprechende Eintrag aus `once_pending` entfernt. Wenn `once_pending` leer ist, wird die Regel automatisch gelöscht.

**`automation_block_run_key`:**  
Verhindert, dass derselbe Zeitplan innerhalb einer Minute doppelt ausgelöst wird (bei mehrfacher Iteration aufgrund von Taktjitter).

### 2.3 `persistence_loop` (`services/persistence.py`)

**Zweck:** Schreibt alle dirty-markierten Zustände alle 2 Sekunden auf Disk.

**Takt:** Alle 2 Sekunden

**Dirty-Flags:**
- `state.schedules_dirty` → `schedules.json`
- `state.queue_dirty` → `queue.json`
- `state.history_dirty` → `history.json`

**Atomares Schreiben:** Alle Dateien werden über `.tmp` + `os.replace()` geschrieben, um Teildateien bei Absturz zu verhindern. Das OS garantiert die Atomarität von `rename()`/`os.replace()` auf demselben Dateisystem.

### 2.4 `io_worker` (`services/io_worker.py`)

**Zweck:** Serialisiert alle GPIO-Hardware-Operationen auf einen einzigen Thread. Verhindert Race-Conditions beim GPIO-Zugriff aus mehreren Threads.

**Design:** Producer-Consumer mit einer `queue.Queue`. API-Route und Timer senden `IOCommand`-Objekte, der IO-Worker führt sie sequenziell aus und gibt `IOResult` zurück.

**Timeout:** Jedes Kommando hat einen konfigurierbaren Timeout (Standard: 5 Sekunden). Bei Timeout: `IOResult(success=False, error="timeout")`.

**Kritische Invariante:** Der IO-Worker-Thread ist der **einzige** Thread, der `open()`, `close()`, `close_all()` auf dem Valve-Driver aufruft. Kein anderer Thread darf den Driver direkt verwenden.

### 2.5 `watchdog_loop`

**Zweck:** Sendet regelmäßig `WATCHDOG=1` an systemd, um den systemd-Watchdog am Leben zu halten.

**Takt:** Alle ~15 Sekunden (deutlich unter dem WatchdogSec=30-Limit).

**systemd-Integration:** Verwendet `sd_notify()` aus der `systemd`-Python-Bibliothek, wenn verfügbar. Auf Nicht-systemd-Systemen ist `_sd_notify()` ein No-Op.

---

## 3. Concurrency-Modell: Prepare / Execute / Commit

Alle schreibenden Operationen, die Hardware-Zugriff und State-Änderung verbinden, verwenden das dreiphasige Muster:

```python
# Phase 1: Prepare (unter Lock)
# - Zustand lesen, validieren
# - Alle nötigen Werte für Hardware-Op berechnen
# - Lock wieder freigeben

# Phase 2: Execute (ohne Lock)
# - Hardware-Operation via io_worker ausführen
# - Kein Lock während des Wartens auf IO-Worker
# (State kann sich in diesem Fenster durch andere Threads ändern)

# Phase 3: Commit (unter Lock)
# - Ergebnis der Hardware-Op in State übernehmen
# - Nur bei Erfolg: State-Änderung committen
# - Bei Fehler: Rollback oder Recovery-Semantik
```

**Warum kein Lock während Hardware-Op?**  
`io_worker.send_command()` blockiert bis zu 5 Sekunden. Ein gehaltener `state_lock` würde alle anderen Threads (Timer, Scheduler, API-Requests) blockieren und das System effektiv einfrieren.

**Unlock-Fenster:**  
Zwischen Phase 1 und Phase 3 kann sich der State durch andere Threads verändern (z.B. stoppt der Timer eine Zone). Die Commit-Phase prüft daher immer, ob der State noch konsistent ist (z.B. `if zone in state.active_runs`).

### 3.1 `state_lock`-Konvention

- Jeder Zugriff auf `state`-Felder **muss** unter `state_lock` erfolgen
- Funktionen mit `_locked` im Namen erwarten, dass der Lock **bereits gehalten** wird
- `state_lock` ist ein `threading.Lock()` (nicht re-entrant) → kein verschachteltes Acquiren
- `get_valve_driver()` kann intern `state_lock` anfordern → **niemals** innerhalb von `state_lock` aufrufen (Deadlock-Risiko)

---

## 4. State-Machine: Queue-Zustände

```
           ┌─────────────────────────────────────────────┐
           │                                             │
     queue/add                                      queue/clear
           │                                             │
           ▼                                             │
        "bereit" ◄──────────────────────────────────────┤
           │                                             │
      queue/start                                        │
           │                                             │
           ▼                                             │
        "läuft" ──────────────────────────────────►  "fertig"
           │         (alle Items abgearbeitet)           │
      queue/pause                                        │
           │                                             │
           ▼                                             │
       "pausiert" ─────────────────────────────────────►┘
           │
      queue/start (resume)
           │
           └──────────────────────────────────────────► "läuft"
```

- `"bereit"`: Queue ist befüllt oder leer, wartet auf `POST /queue/start`
- `"läuft"`: `timer_loop` startet automatisch nächste Items wenn Kapazität frei
- `"pausiert"`: Queue-Abarbeitung gestoppt; laufende Ventile **werden nicht gestoppt**
- `"fertig"`: Alle Items abgearbeitet, Queue ist leer

**Zustand-Persistenz beim Neustart:** `queue_state` wird beim Start immer auf `"bereit"` zurückgesetzt, unabhängig vom gespeicherten Wert. Die Queue-Inhalte bleiben erhalten.

---

## 5. Hardware-Safety-Design

### Warum verbleiben fehlgeschlagene Zonen in `active_runs`?

Wenn `close()` für eine Zone fehlschlägt, wird die Zone **nicht** aus `active_runs` entfernt, obwohl sie logisch gestoppt sein sollte.

**Begründung:** Die Invariante des Systems lautet: *Ein Eintrag in `active_runs` bedeutet, dass das Ventil physisch offen ist oder sein könnte.* Ein gescheitertes `close()` bedeutet: wir wissen nicht, ob das Ventil geschlossen ist. Der konservative Ansatz ist daher, es weiterhin als "offen" zu behandeln und es erneut zu versuchen.

**Alternative wäre gefährlich:** Das Ventil aus `active_runs` entfernen ohne Gewissheit würde bedeuten: logisch "geschlossen", physisch ggf. offen. Folge: Wasser läuft unkontrolliert.

**Retry-Mechanismus:**  
Die Zone bleibt in `active_runs` mit `end_time = jetzt - 1s`. Der `timer_loop` sieht die abgelaufene Zone im nächsten Durchlauf und versucht erneut `close()`, mit exponentiellem Backoff. Nach `HW_CLOSE_MAX_RETRIES = 5` Fehlversuchen wird der Hardware-Fault ausgelöst.

### Fail-Safe `close_all()` bei Startup und Shutdown

Beim Startup (Schritt 6 der Startup-Sequenz) und beim Shutdown wird `close_all()` via IO-Worker ausgeführt. Damit werden Ventile geschlossen, die durch einen vorherigen Absturz offen geblieben sein könnten.

---

## 6. Persistenz-Schicht

```
state (RAM)  ──dirty-flag──►  persistence_loop (alle 2s)  ──►  *.json (Disk)
                              │
                              └──► atomic_write: .tmp → os.replace()
```

**Atomares Schreiben:**
```python
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)  # atomar auf Linux (gleicher Mountpoint)
```

**Corrupt-File-Recovery:**  
Wenn eine JSON-Datei beim Lesen nicht geparst werden kann, wird sie mit Zeitstempel umbenannt (`*.corrupt-YYYYMMDD-HHMMSS`) und sichere Defaults werden verwendet. Pro Datei werden max. 3 Backup-Dateien behalten.

**Sofortiges Schreiben (ohne dirty-Flag):**  
User-Settings werden bei `POST /settings` sofort atomar geschrieben (nicht über den 2s-Persistence-Loop), da Settings-Änderungen nutzerkritisch und selten sind.

---

## 7. Security-Schichten

```
Internet/LAN
     │
     ▼
[Firewall: nur Port 8080 für LAN]
     │
     ▼
[nginx Reverse-Proxy – optional]
     │
     ▼
[SecurityHeadersMiddleware]   ← outermost: Header auf ALLE Responses
     │
[CORSMiddleware]              ← middle: Preflight vor Rate-Limit beantworten
     │
[SlowAPIMiddleware]           ← inner: Rate-Limiting (120/min global)
     │
[Route-Handler]
     │
[require_api_key Dependency]  ← alle Routen außer /health: X-API-Key prüfen
```

**Middleware-Reihenfolge (Starlette-Besonderheit):**  
In Starlette gilt: das **zuletzt** hinzugefügte Middleware ist das **äußerste** und verarbeitet Requests **zuerst**. Daher: `SlowAPIMiddleware` zuerst hinzufügen (innermost), `SecurityHeadersMiddleware` zuletzt (outermost).

**Warum CORS vor Rate-Limiting?**  
Browser senden OPTIONS-Preflight-Requests ohne Auth-Header. Die `CORSMiddleware` muss diese direkt beantworten, bevor der Rate-Limiter sie zählt (kein sinnloser 429 auf Preflight).

**CORS ist Browser-only:**  
CORS wird nur von Browsern erzwungen. Das Shiny-Frontend sendet Server-seitige Requests (Python `requests`-Bibliothek), die CORS komplett umgehen. Die CORS-Konfiguration ist daher nur relevant wenn das Backend direkt per Browser-JavaScript abgefragt wird.

**Security-Header (alle Responses):**

| Header | Wert | Zweck |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Kein MIME-Sniffing |
| `X-Frame-Options` | `DENY` | Kein Clickjacking via iFrame |
| `X-XSS-Protection` | `0` | Alten XSS-Filter deaktivieren (OWASP-Empfehlung) |
| `Referrer-Policy` | `no-referrer` | Keine URL in Referer-Header |
| `Content-Security-Policy` | `default-src 'none'` | Kein Content bei JSON-API ladbar |
| `Server` | `webserver` | uvicorn-Version verschleiert |

---

## 8. Globaler State (`core/state.py`)

```
RunState
│
├── Ventil-Zustand
│   ├── paused: bool
│   └── active_runs: Dict[int, ActiveRun]
│       └── ActiveRun: zone, end_time, started_at, paused_at, paused_total_s,
│                       remaining_s, hw_close_failures, hw_next_retry_at, ...
│
├── Queue
│   ├── queue: List[QueueItem]
│   │   └── QueueItem: zone, duration, time_unit, source
│   ├── queue_state: str ("bereit"|"läuft"|"pausiert"|"fertig")
│   └── queue_state_before_valve_pause: str
│
├── Zeitpläne
│   ├── schedules: List[ScheduleRule]
│   │   └── ScheduleRule: id, zone, weekdays, start_times, duration_s,
│   │                      repeat, enabled, last_run_on, once_pending
│   └── automation_enabled: bool
│
├── Hardware-Fault
│   ├── hw_faulted: bool
│   ├── hw_fault_reason: str
│   ├── hw_fault_zone: Optional[int]
│   └── hw_fault_since: str
│
├── Parallel-Modus
│   ├── parallel_enabled: bool
│   └── max_concurrent_valves: int
│
├── Device-Konfiguration (aus device_config.json)
│   ├── max_valves: int
│   ├── valve_driver_mode: str
│   ├── relay_active_low: bool
│   ├── gpio_pins_by_zone: Dict[int, int]
│   ├── hard_max_runtime_s: int
│   └── hard_max_concurrent_valves: int
│
├── User-Settings (aus user_settings.json)
│   ├── max_history_items: int
│   ├── navbar_title: str
│   ├── accent_color: str
│   ├── default_duration: int
│   └── default_time_unit: str
│
├── Dirty-Flags
│   ├── schedules_dirty: bool
│   ├── queue_dirty: bool
│   └── history_dirty: bool
│
└── Verlauf
    └── run_history: List[HistoryItem]
        └── HistoryItem: ts_end, zone, duration_s, source, time_unit
```

**Singleton-Invariante:** `active_runs` ist die **einzige** Quelle der Wahrheit dafür, ob ein Ventil gerade läuft. Kein anderes Flag darf diesen Zustand duplizieren oder widersprechen.
