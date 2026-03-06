# API_REFERENCE.md – API-Referenz

Vollständige Dokumentation aller REST-Endpunkte des Noria-Backends.

---

## Grundlagen

### Basisadresse

```
http://<HOST>:8000
```

Standard: `http://127.0.0.1:8000` (lokaler Pi-Betrieb)

### Authentifizierung

Alle Endpunkte außer `GET /health` erfordern einen API-Key:

```
X-API-Key: <64-stelliger-Hex-Key>
```

Den Key auslesen:
```bash
cat data/api_key.txt
```

### Content-Type

Alle Request-Bodies sind JSON: `Content-Type: application/json`  
Alle Responses sind JSON.

### Fehlerformat

Standard-Fehler:
```json
{"detail": "Fehlermeldung als Text"}
```

Teilfehler (z.B. bei `/stop` wenn nicht alle Ventile schließen):
```json
{
  "detail": {
    "message": "Beschreibung",
    "stopped": [1, 2],
    "failed": [{"zone": 3, "error": "GPIO error"}]
  }
}
```

### Rate-Limiting

| Tier | Limit | Gilt für |
|---|---|---|
| Global | 120/Minute | Alle Endpunkte |
| Mutations | 30/Minute | Alle POST und DELETE |

Limits gelten pro Client-IP. Bei Überschreitung: HTTP 429.

```json
{"detail": "Rate limit exceeded: 30 per 1 minute"}
```

### HTTP-Status-Codes

| Code | Bedeutung |
|---|---|
| 200 | Erfolg |
| 400 | Ungültige Eingabe (z.B. Zone außerhalb Bereich, Laufzeit = 0) |
| 401 | Kein oder ungültiger API-Key |
| 404 | Ressource nicht gefunden (z.B. Schedule-ID unbekannt) |
| 409 | Konflikt mit aktuellem Zustand (z.B. schon pausiert, nix läuft) |
| 422 | Pydantic-Validierungsfehler (strukturell falsche Anfrage) |
| 423 | Gesperrt durch Hardware-Fault |
| 429 | Rate-Limit überschritten |
| 503 | Hardware-Fehler beim Ausführen der Operation |

---

## Endpunkte

### GET /health

Systemzustand für Monitoring. **Kein API-Key erforderlich.**

HTTP-Status ist immer `200`. Das `ok`-Feld ist das eigentliche Gesundheitssignal.

**Response:**
```json
{
  "ok": true,
  "service": "irrigation",
  "version": 1,
  "ts": "2025-03-05T09:00:00+01:00",
  "running_zones": [1, 3],
  "queue_length": 2,
  "parallel_enabled": false,
  "max_concurrent_valves": 1,
  "hw_faulted": false,
  "hw_fault_reason": "",
  "hw_fault_zone": null,
  "hw_fault_since": "",
  "valves": {
    "valve_driver": "rpi",
    "configured_driver_mode": "rpi",
    "relay_active_low": true,
    "max_valves": 6,
    "configured_zones": [1, 2, 3, 4, 5, 6],
    "missing_zones": [],
    "gpio_config_valid": true,
    "invalid_pins": [],
    "duplicate_pins": []
  }
}
```

| Feld | Beschreibung |
|---|---|
| `ok` | `false` wenn `hw_faulted=true`, sonst `true` |
| `hw_faulted` | `true` = Hardware-Fault aktiv, Start gesperrt |
| `hw_fault_reason` | Ursache des Faults (z.B. GPIO-Fehlermeldung) |
| `hw_fault_zone` | Zone die den Fault ausgelöst hat |
| `hw_fault_since` | Zeitstempel des Fault-Beginns |
| `missing_zones` | Zonen ohne GPIO-Pin-Konfiguration (nur bei `driver=rpi`) |
| `gpio_config_valid` | `false` wenn Pins fehlen oder ungültig sind |

**curl:**
```bash
curl http://localhost:8000/health
```

---

### GET /status

Vollständiger Systemzustand. **API-Key erforderlich.**

**Response:**
```json
{
  "running_zone": 1,
  "running_zones": [1],
  "paused": false,
  "queue_length": 2,
  "queue_state": "bereit",
  "hw_faulted": false,
  "hw_fault_reason": "",
  "hw_fault_zone": null,
  "hw_fault_since": "",
  "automation_enabled": true,
  "parallel_enabled": false,
  "max_concurrent_valves": 1,
  "active_runs": {
    "1": {
      "remaining_s": 45,
      "time_unit": "Sekunden",
      "started_source": "manual",
      "planned_s": 60
    }
  }
}
```

| Feld | Beschreibung |
|---|---|
| `running_zone` | Erste laufende Zone (Legacy, für Kompatibilität) oder `null` |
| `running_zones` | Alle laufenden Zonen als sortierte Liste |
| `paused` | `true` = alle Ventile pausiert (Restzeit gespeichert) |
| `queue_state` | `"bereit"` \| `"läuft"` \| `"pausiert"` \| `"fertig"` |
| `active_runs` | Map von Zone → Lauf-Detail (verbleibende Zeit etc.) |
| `active_runs[zone].remaining_s` | Verbleibende Sekunden (live berechnet) |
| `active_runs[zone].started_source` | `"manual"` \| `"queue"` \| `"schedule"` |

**curl:**
```bash
curl http://localhost:8000/status \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

---

### POST /start

Startet eine einzelne Zone manuell.

**Request-Body:**
```json
{
  "zone": 1,
  "duration": 60,
  "time_unit": "Sekunden"
}
```

| Feld | Typ | Beschreibung |
|---|---|---|
| `zone` | int, 1..MAX_VALVES | Zone starten |
| `duration` | int, ≥ 1 | Laufzeit (in `time_unit`) |
| `time_unit` | `"Sekunden"` \| `"Minuten"` | Zeiteinheit |

`duration` in Sekunden: max. `MAX_RUNTIME_S` (Standard: 3600 = 1 Stunde).  
`duration` in Minuten: wird intern zu Sekunden umgerechnet, gleiche Maximalgrenze.

**Response (200):**
```json
{
  "ok": true,
  "running_zone": 1,
  "duration": 60,
  "time_unit": "Sekunden",
  "parallel_enabled": false,
  "max_concurrent_valves": 1
}
```

**Fehler:**
- 400: Zone außerhalb Bereich, Laufzeit ≤ 0 oder > Max
- 409: Zone läuft bereits, oder Kapazitätsgrenze erreicht (Parallel-Modus)
- 423: Hardware-Fault aktiv
- 503: GPIO-Fehler beim Öffnen

**curl:**
```bash
curl -X POST http://localhost:8000/start \
  -H "X-API-Key: $(cat data/api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"zone": 1, "duration": 60, "time_unit": "Sekunden"}'
```

---

### POST /stop

Stoppt sofort alle aktiven Ventile.

**Kein Request-Body.**

**Teilfehler-Semantik (sicherheitskritisch):** Nur Zonen, die hardware-seitig erfolgreich geschlossen wurden, werden aus `active_runs` entfernt. Fehlgeschlagene Zonen bleiben mit `end_time = jetzt - 1s` im State und werden vom Timer mit exponentiellem Backoff automatisch nachgeschlossen. Das garantiert: logisch "gestoppt" ↔ Hardware physisch geschlossen.

**Response (200, alles OK):**
```json
{"ok": true, "stopped_zones": [1, 3]}
```

**Response (503, Teilfehler):**
```json
{
  "detail": {
    "message": "Nicht alle Ventile konnten gestoppt werden. Fehlgeschlagene Zonen werden automatisch nachgeschlossen.",
    "stopped": [1],
    "failed": [{"zone": 3, "error": "GPIO error message"}]
  }
}
```

Wenn keine Ventile laufen: `{"ok": true, "stopped_zones": []}` (kein Fehler).

**curl:**
```bash
curl -X POST http://localhost:8000/stop \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

---

### POST /pause

Pausiert alle aktiven Ventile (Restzeit wird gespeichert).

Rollback-Semantik: Nur wenn **alle** Ventile erfolgreich geschlossen wurden, wird der State auf `paused=true` gesetzt. Bei Hardware-Fehler: kein State-Update.

**Kein Request-Body.**

**Response (200):**
```json
{"ok": true, "paused_zones": [1, 3]}
```

**Fehler:**
- 409: Kein Ventil läuft / schon pausiert
- 503: Hardware-Fehler → Rollback, State unverändert

**curl:**
```bash
curl -X POST http://localhost:8000/pause \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

---

### POST /resume

Setzt pausierte Ventile mit der gespeicherten Restzeit fort.

Rollback-Semantik: Nur wenn **alle** Ventile erfolgreich geöffnet wurden, wird `paused=false` gesetzt.

**Kein Request-Body.**

**Response (200):**
```json
{"ok": true, "resumed_zones": [1, 3]}
```

**Fehler:**
- 409: Nicht pausiert
- 423: Hardware-Fault aktiv
- 503: Hardware-Fehler → Rollback, State bleibt `paused=true`

**curl:**
```bash
curl -X POST http://localhost:8000/resume \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

---

### POST /fault/clear

Quittiert den Hardware-Fault nach Operator-Prüfung.

**Voraussetzungen:**
- ≥ 60 Sekunden seit Fault-Auslösung (`HW_FAULT_COOLDOWN_S`)
- Keine Ventile laufen gerade

**Kein Request-Body.**

**Response (200):**
```json
{"ok": true}
```

**Fehler:**
- 409: Kein Fault aktiv / Ventile laufen noch
- 423: Cooldown läuft noch (mit `retry_after_s`)

**curl:**
```bash
curl -X POST http://localhost:8000/fault/clear \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

---

### GET /automation

Automatikmodus (Zeitpläne aktiv/inaktiv) abfragen.

**Response (200):**
```json
{"automation_enabled": true}
```

---

### POST /automation/enable

Automatikmodus aktivieren (Zeitpläne werden ausgeführt).

**Kein Request-Body. Response:** `{"ok": true, "automation_enabled": true}`

---

### POST /automation/disable

Automatikmodus deaktivieren (Zeitpläne werden ignoriert).

**Kein Request-Body. Response:** `{"ok": true, "automation_enabled": false}`

---

### POST /automation/toggle

Automatikmodus umschalten.

**Kein Request-Body. Response:** `{"ok": true, "automation_enabled": <neuer_wert>}`

---

### GET /parallel

Parallelmodus-Konfiguration abfragen.

**Response (200):**
```json
{
  "parallel_enabled": false,
  "max_concurrent_valves": 2
}
```

---

### POST /parallel

Parallelmodus konfigurieren.

**Request-Body:**
```json
{
  "parallel_enabled": true,
  "max_concurrent_valves": 2
}
```

| Feld | Typ | Beschreibung |
|---|---|---|
| `parallel_enabled` | bool | Parallelbetrieb aktivieren |
| `max_concurrent_valves` | int, ≥ 1 | Max. gleichzeitige Ventile (wird durch `hard_limits.MAX_CONCURRENT_VALVES` gedeckelt) |

**Response (200):** `{"ok": true, "parallel_enabled": true, "max_concurrent_valves": 2}`

---

### GET /queue

Queue-Inhalt und -Zustand abfragen.

**Response (200):**
```json
{
  "queue_state": "bereit",
  "queue_length": 3,
  "items": [
    {"zone": 1, "duration": 60, "time_unit": "Sekunden"},
    {"zone": 2, "duration": 120, "time_unit": "Sekunden"},
    {"zone": 3, "duration": 90, "time_unit": "Sekunden"}
  ]
}
```

Queue-Zustände: `"bereit"` → `"läuft"` → `"fertig"` (oder `"pausiert"`)

---

### POST /queue/add

Item(s) zur Queue hinzufügen.

`zone=0` = alle Ventile 1..MAX_VALVES werden als separate Items eingereiht.

**Request-Body:**
```json
{
  "zone": 1,
  "duration": 60,
  "time_unit": "Sekunden"
}
```

**Response (200):** `{"ok": true, "queue_length": 4}`

**Fehler:**
- 400: Zone außerhalb Bereich, Laufzeit ≤ 0, Queue voll (max. 50 Items)

**curl (alle Ventile à 5 Minuten):**
```bash
curl -X POST http://localhost:8000/queue/add \
  -H "X-API-Key: $(cat data/api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"zone": 0, "duration": 5, "time_unit": "Minuten"}'
```

---

### POST /queue/start

Queue-Abarbeitung starten.

**Kein Request-Body.**

**Response (200):** `{"ok": true, "queue_state": "läuft", "started_zones": [1]}`

**Fehler:**
- 400: Queue leer
- 409: Queue läuft bereits
- 423: Hardware-Fault aktiv

---

### POST /queue/pause

Queue-Abarbeitung pausieren. Laufende Ventile werden **nicht** gestoppt.

**Kein Request-Body.**

**Response (200):** `{"ok": true, "queue_state": "pausiert"}`

---

### POST /queue/clear

Queue vollständig leeren und auf `"bereit"` zurücksetzen.

**Kein Request-Body.**

**Response (200):** `{"ok": true, "cleared": <anzahl>}`

---

### GET /schedule

Alle Zeitpläne abrufen.

**Response (200):**
```json
{
  "schedules": [
    {
      "id": "a1b2c3d4",
      "zone": 1,
      "weekdays": [0, 2, 4],
      "start_times": ["06:00", "18:00"],
      "duration_s": 300,
      "time_unit": "Sekunden",
      "repeat": true,
      "enabled": true,
      "last_run_on": "2025-03-05 06:00",
      "once_pending": null
    }
  ]
}
```

| Feld | Beschreibung |
|---|---|
| `weekdays` | Liste von Wochentagen: 0=Montag, 1=Dienstag, ..., 6=Sonntag |
| `start_times` | Uhrzeiten im Format `"HH:MM"` |
| `repeat` | `true` = wöchentlich wiederholen; `false` = Einmalregel |
| `once_pending` | Nur bei `repeat=false`: noch ausstehende Auslösungen |

---

### POST /schedule/add

Neuen Zeitplan anlegen.

**Request-Body:**
```json
{
  "zone": 1,
  "weekdays": [0, 2, 4],
  "start_times": ["06:00"],
  "duration_s": 300,
  "time_unit": "Sekunden",
  "repeat": true
}
```

**Response (200):** `{"ok": true, "id": "a1b2c3d4"}`

**Fehler:**
- 400: Zone ungültig, max. 20 Zeitpläne erreicht, ungültige Zeitangaben
- 422: Strukturell ungültige Anfrage (Pydantic)

---

### POST /schedule/enable/{schedule_id}

Zeitplan aktivieren.

**Response (200):** `{"ok": true, "enabled": true}`  
**Fehler:** 404 wenn Schedule-ID nicht existiert.

---

### POST /schedule/disable/{schedule_id}

Zeitplan deaktivieren.

**Response (200):** `{"ok": true, "enabled": false}`  
**Fehler:** 404 wenn Schedule-ID nicht existiert.

---

### DELETE /schedule

Einen oder mehrere Zeitpläne löschen.

**Request-Body:** Liste von IDs
```json
["a1b2c3d4", "e5f6g7h8"]
```

**Response (200):** `{"deleted": ["a1b2c3d4", "e5f6g7h8"]}`  
**Fehler:** 404 wenn keine der IDs gefunden wurde.

**curl:**
```bash
curl -X DELETE http://localhost:8000/schedule \
  -H "X-API-Key: $(cat data/api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '["a1b2c3d4"]'
```

---

### GET /history

Verlauf abgeschlossener Bewässerungsläufe (neueste zuerst).

**Response (200):**
```json
{
  "count": 2,
  "items": [
    {
      "ts_end": "2025-03-05T09:01:00+01:00",
      "zone": 1,
      "duration_s": 60,
      "source": "manual",
      "time_unit": "Sekunden"
    }
  ]
}
```

| Feld | Beschreibung |
|---|---|
| `ts_end` | ISO-8601-Zeitstempel Laufende |
| `duration_s` | Tatsächlich gelaufene Sekunden |
| `source` | `"manual"` \| `"queue"` \| `"schedule"` |

---

### GET /settings

Benutzereinstellungen abrufen.

**Response (200):**
```json
{
  "max_history_items": 20,
  "navbar_title": "Noria",
  "accent_color": "#82372a",
  "default_duration": 5,
  "default_time_unit": "Minuten",
  "max_valves": 6
}
```

`max_valves` ist readonly (kommt aus `device_config.json`).

---

### POST /settings

Benutzereinstellungen aktualisieren. Wird sofort atomar auf Disk geschrieben.

**Request-Body:**
```json
{
  "max_history_items": 50,
  "navbar_title": "Hof Müller Bewässerung",
  "accent_color": "#1a7a4a",
  "default_duration": 10,
  "default_time_unit": "Minuten"
}
```

**Response (200):** `{"ok": true}`

---

## Polling-Empfehlung

Das System bietet keine WebSocket-Verbindung. Empfohlene Polling-Strategie:

| Daten | Endpunkt | Empfehlung |
|---|---|---|
| Aktiver Zustand (Zone, Pause, Fault) | `/status` | Alle 1 Sekunde |
| Zeitpläne, Verlauf, Einstellungen | `/schedule`, `/history`, `/settings` | Alle 5 Sekunden |
| Verbindungsprüfung | `/health` | Alle 1-5 Sekunden |

Das Shiny-Frontend verwendet exakt diese Strategie (`poll_status_s=1`, `poll_slow_s=5`).
