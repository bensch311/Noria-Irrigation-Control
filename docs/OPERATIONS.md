# OPERATIONS.md – Betriebshandbuch

Referenz für den laufenden Betrieb: Log-Events, Hardware-Fault-Behandlung, Log-Analyse, Backup und Recovery.

---

## 1. Log-System

### 1.1 Format

Alle Events werden als JSONL (JSON Lines) geschrieben: eine JSON-Zeile pro Event.

Pfad: `logs/irrigation.jsonl`  
Rotation: Maximal 10 MB pro Datei, bis zu 10 Backup-Dateien → max. ~110 MB Gesamtgröße.

Jedes Event enthält immer diese Pflichtfelder:

```json
{
  "ts":       "2025-03-05T09:00:00+01:00",
  "event":    "valve_start",
  "level":    "info",
  "event_id": "a1b2c3d4"
}
```

| Feld | Beschreibung |
|---|---|
| `ts` | ISO-8601-Zeitstempel mit Timezone (Europe/Berlin) |
| `event` | Event-Name (snake_case) |
| `level` | `"info"` \| `"warning"` \| `"error"` |
| `event_id` | 8-stellige UUID-Kurzform zur Korrelation mehrerer Logs |

Bei `level="error"` wird zusätzlich ein `traceback`-Array mit den letzten 15 Zeilen des Python-Tracebacks angehängt (wenn ein Exception-Kontext aktiv ist).

---

### 1.2 Log-Events Referenz

#### Ventil-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `valve_start` | info | Ventil wurde geöffnet | `zone`, `duration_s`, `source`, `time_unit`, `parallel_enabled`, `active_runs_count` |
| `valve_stop` | info | Alle aktiven Ventile wurden gestoppt (manuell) | `zone="all"`, `stopped_count`, `failed_count`, `queue_state`, `queue_length` |
| `valve_stop_hw_error_retry_scheduled` | error | Stop fehlgeschlagen; Timer wird Retry versuchen | `zone`, `error`, `action="timer_will_retry"` |
| `valve_timeout` | info | Ventil wurde nach Ablauf der Laufzeit geschlossen | `zone`, `actual_s`, `planned_s`, `source` |
| `valve_pause` | info | Alle aktiven Ventile wurden pausiert | `zone="all"`, `remaining_s`, `queue_state`, `queue_length` |
| `valve_resume` | info | Pausierte Ventile wurden fortgesetzt | `zone="all"`, `resumed_zones` |
| `valve_hw_error` | error | Hardware-Fehler beim Öffnen oder Schließen | `zone`, `action` (`"open"` \| `"close"`), `reason`, `failed` |
| `valve_hw_close_retry` | warning | Timer versucht erneut, ein Ventil zu schließen | `zone`, `attempt`, `backoff_s`, `error` |
| `valve_hw_close_failed_final` | error | Alle Retry-Versuche erschöpft → Fault-Latch | `zone`, `attempts`, `error` |
| `valve_driver_init` | info | Valve-Driver wurde initialisiert | `driver`, `mode`, `env_override` |
| `valve_driver_init_fallback` | warning | Unbekannter Treiber → Fallback auf Simulation | `requested_mode`, `driver` |

#### Hardware-Fault-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `hw_fault_latched` | error | Hardware-Fault wurde aktiviert (Start gesperrt) | `zone`, `reason`, `attempts` |
| `hw_fault_cleared` | info | Fault wurde durch Operator quittiert | `cleared_by="manual"` |
| `hw_fault_clear_blocked_cooldown` | warning | Quittierung zu früh nach Fault (Cooldown läuft noch) | `retry_after_s` |
| `hw_fault_clear_blocked_running` | warning | Quittierung nicht möglich: Ventile laufen noch | `running_zones` |

#### Queue-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `queue_add` | info | Items wurden zur Queue hinzugefügt | `zone`, `duration_s`, `time_unit`, `queue_length` |
| `queue_start` | info | Queue-Abarbeitung gestartet | `queue_length` |
| `queue_pause` | info | Queue pausiert (laufende Ventile nicht berührt) | `queue_state`, `queue_length` |
| `queue_clear` | info | Queue vollständig geleert | `cleared_count` |
| `queue_item_start` | info | Einzelnes Queue-Item wurde gestartet | `zone`, `duration_s`, `remaining_queue` |
| `queue_done` | info | Queue vollständig abgearbeitet | `total_items` |
| `queue_corrupt` | error | `queue.json` war korrupt → Backup erstellt | – |

#### Zeitplan-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `schedule_add` | info | Neuer Zeitplan wurde gespeichert | `schedule_id`, `zone`, `weekdays`, `start_times`, `duration_s`, `repeat` |
| `schedule_enable` | info | Zeitplan aktiviert | `schedule_id`, `zone` |
| `schedule_disable` | info | Zeitplan deaktiviert | `schedule_id`, `zone` |
| `schedule_delete` | info | Zeitplan(e) gelöscht | `deleted_ids`, `remaining_count` |
| `schedule_trigger` | info | Zeitplan hat Bewässerung ausgelöst | `schedule_id`, `zone`, `run_key` |

#### Security-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `auth_failure` | warning | Ungültiger oder fehlender API-Key | `client_ip` |
| `rate_limit_exceeded` | warning | Rate-Limit überschritten | `client_ip`, `path` |
| `request_rejected` | warning | Request aus anderem Sicherheitsgrund abgelehnt | `client_ip`, `reason` |

#### System-Events

| Event | Level | Beschreibung | Typische Zusatzfelder |
|---|---|---|---|
| `service_start` | info | Backend vollständig gestartet | `driver`, `max_valves` |
| `service_stop` | info | Shutdown eingeleitet | – |
| `failsafe_close_all_startup` | info | Fail-Safe close_all beim Start | `driver` |
| `failsafe_close_all_shutdown` | info | Fail-Safe close_all beim Shutdown | `driver` |
| `io_worker_start` | info | IO-Worker-Thread gestartet | – |
| `io_worker_stop` | info | IO-Worker-Thread gestoppt | – |
| `io_worker_timeout` | warning | IO-Kommando hat Timeout überschritten | `action`, `zone`, `timeout_s` |
| `device_config_created_template` | warning | `device_config.json` fehlte → Vorlage erstellt | – |
| `device_config_corrupt` | error | `device_config.json` war korrupt → Backup erstellt, Defaults verwendet | – |
| `parallel_disabled_waiting_for_drain` | warning | Parallelmodus deaktiviert, aber mehrere Zonen laufen noch | `running_zones`, `queue_length` |

---

## 2. Hardware-Fault-Behandlung

### 2.1 Was ist ein Hardware-Fault?

Ein Hardware-Fault wird ausgelöst, wenn alle konfigurierten Retry-Versuche (`HW_CLOSE_MAX_RETRIES = 5`) zum Schließen eines Ventils fehlgeschlagen sind. Dies deutet auf ein ernstes Hardware-Problem hin (GPIO nicht erreichbar, Relay-Board defekt, Verkabelungsproblem).

**Auswirkungen eines aktiven Faults:**
- Manueller Ventilstart: gesperrt (HTTP 423)
- Queue-Start: gesperrt (HTTP 423)
- `GET /health` gibt `"ok": false` zurück
- Fault-Banner in der Frontend-Oberfläche sichtbar

Die betroffene Zone bleibt in `active_runs` bis sie erfolgreich geschlossen wurde oder der Fault quittiert wird.

### 2.2 Fault-Ursachen identifizieren

```bash
# Alle Fehler der letzten Stunde anzeigen
jq 'select(.level == "error")' logs/irrigation.jsonl | tail -50

# Hardware-Fault-Events anzeigen
jq 'select(.event | startswith("hw_fault") or startswith("valve_hw"))' logs/irrigation.jsonl | tail -20

# Fehlermeldung des letzten close()-Fehlers
jq 'select(.event == "valve_hw_close_failed_final")' logs/irrigation.jsonl | tail -5
```

Typische Ursachen:
- `RPi.GPIO` nicht verfügbar oder nicht korrekt installiert
- GPIO-Pin nicht als OUTPUT konfiguriert (falsche `device_config.json`)
- Relay-Board-Spannungsversorgung unterbrochen
- Verkabelungsfehler (falsche Pinbelegung)

### 2.3 Fault quittieren

**Voraussetzungen vor der Quittierung:**
1. Mindestens 60 Sekunden seit dem Fault vergangen (`HW_FAULT_COOLDOWN_S`)
2. Keine Ventile laufen gerade
3. Hardware-Problem physisch behoben oder als akzeptabel eingestuft

```bash
# Via curl (mit API-Key):
curl -X POST http://localhost:8000/fault/clear \
  -H "X-API-Key: $(cat data/api_key.txt)"
```

Oder im Frontend: Fault-Banner → „Fault quittieren"-Button.

Nach erfolgreicher Quittierung: `hw_faulted` wird `false`, das System ist wieder betriebsbereit.

### 2.4 Checkliste nach Hardware-Fault

```
□ Log-Datei auf Fehlerursache prüfen (jq-Abfrage oben)
□ Relais-Board: LED-Status prüfen (Power, Kanal-LEDs)
□ Verkabelung: GPIO-Pin gegen device_config.json prüfen
□ RPi.GPIO: Läuft der Backend-Prozess als Benutzer mit gpio-Gruppe?
□ Ventil: Manuelle Funktionsprüfung (Direktansteuerung)
□ Nach Behebung: sudo systemctl restart noria-backend
□ Mindestens 60 Sekunden warten
□ POST /fault/clear ausführen
□ Test: Manuellen Kurz-Start (5 Sekunden) durchführen und Log prüfen
```

---

## 3. Log-Analyse mit `jq`

```bash
# Alle Events des heutigen Tages
jq 'select(.ts | startswith("2025-03-05"))' logs/irrigation.jsonl

# Alle Auth-Failures
jq 'select(.event == "auth_failure")' logs/irrigation.jsonl

# Alle Fehler (level=error)
jq 'select(.level == "error")' logs/irrigation.jsonl

# Alle Ventilstarts heute
jq 'select(.event == "valve_start")' logs/irrigation.jsonl | jq '{ts, zone, duration_s, source}'

# Ventilstarts der letzten Woche, nur Zone 3
jq 'select(.event == "valve_start" and .zone == 3)' logs/irrigation.jsonl

# Rate-Limit-Überschreitungen
jq 'select(.event == "rate_limit_exceeded")' logs/irrigation.jsonl

# Anzahl Bewässerungsläufe pro Zone
jq 'select(.event == "valve_start") | .zone' logs/irrigation.jsonl | sort | uniq -c | sort -rn

# Alle Events mit Traceback (unbehandelte Fehler)
jq 'select(.traceback != null)' logs/irrigation.jsonl

# Events in einem Zeitraum
jq 'select(.ts >= "2025-03-01T00:00:00" and .ts <= "2025-03-05T23:59:59")' logs/irrigation.jsonl

# Alle Log-Dateien zusammen analysieren (inkl. rotierter Logs)
cat logs/irrigation.jsonl.* logs/irrigation.jsonl | jq 'select(.event == "hw_fault_latched")'
```

---

## 4. Backup und Recovery

### 4.1 Was muss gesichert werden?

Das gesamte `data/`-Verzeichnis enthält alle relevanten Daten:

| Datei | Inhalt | Kritikalität |
|---|---|---|
| `api_key.txt` | API-Key (64 Hex-Zeichen) | Hoch – ohne Key kein Frontend-Zugriff |
| `device_config.json` | Hardware-Konfiguration | Hoch – ohne korrekte Konfiguration kein GPIO |
| `frontend_config.json` | Frontend-Parameter | Mittel – schnell neu erstellbar |
| `user_settings.json` | Benutzereinstellungen | Niedrig – Defaults sind akzeptabel |
| `schedules.json` | Alle Zeitpläne | Hoch – Neuanlage aufwendig |
| `queue.json` | Aktuelle Queue | Niedrig – Queue ist flüchtig |
| `history.json` | Verlauf | Mittel – Historienanalyse, nicht betriebskritisch |
| `runtime_state.json` | Parallelmodus etc. | Niedrig – schnell neu konfigurierbar |

### 4.2 Backup erstellen

```bash
# Manuelles Backup
sudo cp -r /opt/noria/data /backup/noria-data-$(date +%Y%m%d)

# Automatisches tägliches Backup via cron (als root)
echo "0 3 * * * root cp -r /opt/noria/data /backup/noria-data-\$(date +\%Y\%m\%d) && find /backup -name 'noria-data-*' -mtime +30 -exec rm -rf {} +" | sudo tee /etc/cron.d/noria-backup
```

### 4.3 `.corrupt-*`-Dateien verstehen

Wenn beim Start eine JSON-Datei nicht geparst werden kann, erstellt das System automatisch ein Backup:

```
data/schedules.json.corrupt-20250305-091532
```

Dies bedeutet: Die Originaldatei war defekt. Eine saubere Vorlage wurde als Ersatz erstellt. Die `.corrupt-*`-Datei enthält den defekten Inhalt zur Analyse.

Pro Datendatei werden maximal 3 Backup-Dateien behalten (`CORRUPT_FILE_MAX_KEEP = 3`). Ältere werden automatisch gelöscht.

```bash
# Korrupte Backups anzeigen
ls data/*.corrupt-*

# Korrupten Inhalt ansehen (zur Analyse)
cat data/schedules.json.corrupt-20250305-091532

# Backup manuell löschen wenn nicht mehr benötigt
rm data/*.corrupt-*
```

### 4.4 Recovery nach Datenverlust

#### Zeitpläne wiederherstellen

```bash
# Aus Backup wiederherstellen
sudo systemctl stop noria-backend
sudo cp /backup/noria-data-20250301/schedules.json /opt/noria/data/
sudo chown noria:noria /opt/noria/data/schedules.json
sudo systemctl start noria-backend
```

#### API-Key wiederherstellen / neu generieren

Wenn `api_key.txt` verloren geht, wird beim nächsten Backend-Start automatisch ein neuer Key generiert. Das Frontend muss dann mit dem neuen Key aktualisiert werden.

```bash
sudo systemctl stop noria-backend
sudo rm /opt/noria/data/api_key.txt  # erzwingt Neugenerierung
sudo systemctl start noria-backend
sudo cat /opt/noria/data/api_key.txt  # neuen Key anzeigen
```

---

## 5. Neustart-Verhalten

### Was passiert nach Neustart oder Stromausfall?

| Zustand | Nach Neustart |
|---|---|
| Laufende Ventile | **Werden geschlossen** (Fail-Safe close_all beim Start) |
| `active_runs` | Wird geleert |
| `paused` | Wird auf `false` zurückgesetzt |
| Queue-Inhalt | **Bleibt erhalten** (aus `queue.json` geladen) |
| Queue-State | Wird auf `"bereit"` zurückgesetzt (nicht auf Vorwert) |
| Zeitpläne | **Bleiben erhalten** (aus `schedules.json` geladen) |
| Einmal-Zeitpläne (`repeat=false`) | Ausstehende Auslösungen bleiben erhalten |
| Verlauf | **Bleibt erhalten** (aus `history.json` geladen) |
| Parallelmodus | **Bleibt erhalten** (aus `runtime_state.json` geladen) |

**Sicherheitsgarantie:** Nach jedem Neustart sind alle Ventile physisch geschlossen, bevor die API Anfragen annimmt.

---

## 6. Systemd-Watchdog

Das Backend sendet alle ~15 Sekunden `WATCHDOG=1` via `sd_notify`. Der Watchdog ist auf 30 Sekunden konfiguriert (`WatchdogSec=30`).

```bash
# Watchdog-Status prüfen
sudo systemctl status noria-backend | grep -i watchdog

# Watchdog-Ereignisse in journald suchen
sudo journalctl -u noria-backend | grep -i watchdog
```

Wenn der Backend-Prozess einfriert (z.B. Deadlock), erkennt systemd dies spätestens nach 30 Sekunden und startet den Service neu.
