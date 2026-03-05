# CONFIGURATION.md – Konfigurations-Referenz

Vollständige Referenz aller Konfigurationsdateien, Umgebungsvariablen und Hard-Limits.

---

## Übersicht: Konfigurationsquellen

| Quelle | Zweck | Änderbar im Betrieb? |
|---|---|---|
| `data/device_config.json` | Hardware/GPIO-Konfiguration | Nein – erfordert Neustart |
| `data/user_settings.json` | Benutzereinstellungen (Anzeige etc.) | Ja – via `POST /settings` |
| `data/frontend_config.json` | Frontend-Verbindungsparameter | Nein – erfordert Frontend-Neustart |
| `data/runtime_state.json` | Laufzeit-Toggles (Parallelmodus) | Ja – via `POST /parallel` |
| Umgebungsvariablen | Deployment-spezifische Overrides | Nein – erfordert Neustart |
| `core/config.py` | Code-interne Defaults und Hard-Limits | Nein – nur per Software-Update |

**Override-Reihenfolge:** ENV-Variable > `device_config.json` > Code-Default aus `config.py`

---

## 1. `data/device_config.json`

Hardware-Konfiguration. Wird beim Backend-Start einmalig geladen. Änderungen erfordern Backend-Neustart.

Wenn die Datei fehlt oder korrupt ist, wird eine Vorlage mit Simulationstreiber erstellt und ein Warning geloggt.

```json
{
  "version": 1,
  "device": {
    "MAX_VALVES": 6,
    "IRRIGATION_VALVE_DRIVER": "sim",
    "IRRIGATION_RELAY_ACTIVE_LOW": true,
    "IRRIGATION_GPIO_PINS": {
      "1": 17,
      "2": 18,
      "3": 27,
      "4": 22,
      "5": 23,
      "6": 24
    }
  },
  "hard_limits": {
    "MAX_RUNTIME_S": 3600,
    "MAX_CONCURRENT_VALVES": 2
  }
}
```

### Felder: `device`

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `MAX_VALVES` | int, ≥ 1 | `6` | Anzahl der angeschlossenen Ventile. Bestimmt den gültigen Bereich für `zone` in allen Requests (1..MAX_VALVES). |
| `IRRIGATION_VALVE_DRIVER` | `"sim"` \| `"rpi"` | `"sim"` | Ventiltreiber. `"sim"` = Simulation (kein GPIO), `"rpi"` = echter Raspberry Pi GPIO. |
| `IRRIGATION_RELAY_ACTIVE_LOW` | bool | `true` | Relais-Logik. `true`: LOW = Relais zieht an = Ventil öffnet (typisch für chinesische Relay-Boards mit Optokoppler). `false`: HIGH = Ventil öffnet. Details → [HARDWARE_SETUP.md](HARDWARE_SETUP.md). |
| `IRRIGATION_GPIO_PINS` | dict, `{"zone": pin}` | `{}` | BCM-Pin-Nummern pro Zone. Schlüssel = Zonen-Nummer (1..MAX_VALVES), Wert = BCM-GPIO-Pin (2..27). Muss alle Zonen 1..MAX_VALVES abdecken wenn `driver=rpi`. |

### Felder: `hard_limits`

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `MAX_RUNTIME_S` | int, ≥ 1 | `3600` | Maximale Laufzeit eines Ventils in Sekunden (1 Stunde). Requests mit höherer Laufzeit werden mit HTTP 400 abgelehnt. |
| `MAX_CONCURRENT_VALVES` | int, ≥ 1 | `2` | Maximale Anzahl gleichzeitig laufender Ventile im Parallelmodus. Wird auf `MAX_VALVES` begrenzt. |

### Beispiel: 4-Zonen-Setup

```json
{
  "version": 1,
  "device": {
    "MAX_VALVES": 4,
    "IRRIGATION_VALVE_DRIVER": "rpi",
    "IRRIGATION_RELAY_ACTIVE_LOW": true,
    "IRRIGATION_GPIO_PINS": {
      "1": 17,
      "2": 18,
      "3": 27,
      "4": 22
    }
  },
  "hard_limits": {
    "MAX_RUNTIME_S": 3600,
    "MAX_CONCURRENT_VALVES": 1
  }
}
```

---

## 2. `data/user_settings.json`

Benutzereinstellungen. Werden beim Start geladen und können zur Laufzeit über `POST /settings` geändert werden. Änderungen werden sofort atomar auf Disk geschrieben.

```json
{
  "version": 1,
  "user": {
    "MAX_HISTORY_ITEMS": 20,
    "NAVBAR_TITLE": "Bewaesserungscomputer",
    "ACCENT_COLOR": "#82372a",
    "DEFAULT_DURATION": 5,
    "DEFAULT_TIME_UNIT": "Minuten"
  }
}
```

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `MAX_HISTORY_ITEMS` | int, 1..1000 | `20` | Maximale Anzahl Einträge im Bewässerungsverlauf. Ältere Einträge werden automatisch abgeschnitten. |
| `NAVBAR_TITLE` | string | `"Bewaesserungscomputer"` | Titel in der Navigationsleiste des Frontends. |
| `ACCENT_COLOR` | string, `#RRGGBB` | `"#82372a"` | Akzentfarbe für das Frontend-UI (Hex-Format). |
| `DEFAULT_DURATION` | int, ≥ 1 | `5` | Standardlaufzeit beim manuellen Start. Einheit bestimmt `DEFAULT_TIME_UNIT`. |
| `DEFAULT_TIME_UNIT` | `"Sekunden"` \| `"Minuten"` | `"Minuten"` | Standardzeiteinheit für die Anzeige und das Startformular. |

---

## 3. `data/frontend_config.json`

Verbindungsparameter für das Shiny-Frontend. Wird einmalig beim Frontend-Start geladen. Änderungen erfordern Frontend-Neustart.

```json
{
  "_comment": "Kommentar-Keys (Prefix '_') werden beim Laden ignoriert.",
  "base_url": "http://127.0.0.1:8000",
  "poll_status_s": 1,
  "poll_slow_s": 5,
  "backend_fail_threshold": 3,
  "health_timeout_s": 0.8,
  "anzahl_ventile_fallback": 6,
  "navbar_logo": "logo.svg"
}
```

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `base_url` | string (URL) | `"http://127.0.0.1:8000"` | Backend-URL. Muss vom Frontend-Prozess aus erreichbar sein. Kein Trailing-Slash. |
| `poll_status_s` | int, ≥ 1 | `1` | Polling-Intervall in Sekunden für `/status` (Schnell-Daten: aktive Zonen, Queue-Status). |
| `poll_slow_s` | int, ≥ 1 | `5` | Polling-Intervall in Sekunden für `/schedule`, `/history`, `/settings` (Langsam-Daten). |
| `backend_fail_threshold` | int, ≥ 1 | `3` | Anzahl aufeinanderfolgender Backend-Fehler, bevor der Verbindungsfehler-Banner angezeigt wird. |
| `health_timeout_s` | float, > 0 | `0.8` | HTTP-Timeout in Sekunden für `/health`-Anfragen (für schnelle Verbindungsprüfung). |
| `anzahl_ventile_fallback` | int, ≥ 1 | `6` | Fallback-Wert für `MAX_VALVES` wenn `device_config.json` nicht gelesen werden kann. Verhindert Abstürze beim Frontend-Start ohne Backend. |
| `navbar_logo` | string | `""` | Dateiname des Logos im `www/`-Verzeichnis (z.B. `"logo.svg"`). Leer = kein Logo. Datei muss in `www/` vorhanden sein. |

**Hinweis:** Keys mit Prefix `_` (z.B. `_comment`) werden beim Laden ignoriert.

---

## 4. `data/runtime_state.json`

Persistierter Laufzeit-Zustand. Wird beim Start geladen und bei Änderungen via `POST /parallel` sofort geschrieben.

```json
{
  "version": 1,
  "runtime": {
    "parallel_enabled": false,
    "max_concurrent_valves": 2
  }
}
```

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `parallel_enabled` | bool | `false` | Ist der Parallelmodus aktiv? Wenn `false`: maximal 1 Ventil gleichzeitig. |
| `max_concurrent_valves` | int, ≥ 1 | `2` | Maximale gleichzeitige Ventile im Parallelmodus. Wird durch `hard_limits.MAX_CONCURRENT_VALVES` gedeckelt. |

---

## 5. Umgebungsvariablen

| Variable | Typ | Default | Beschreibung |
|---|---|---|---|
| `ALLOWED_ORIGINS` | string, komma-separiert | `"http://localhost:8080"` | CORS: erlaubte Browser-Origins. Leerzeichen um einzelne Origins werden ignoriert. Beispiel: `"http://192.168.1.100:8080,http://localhost:8080"` |
| `ENABLE_DOCS` | `"true"` \| `"false"` | `"false"` | Aktiviert Swagger UI (`/docs`) und ReDoc (`/redoc`). **Nur für Entwicklung.** Im Produktionsbetrieb nicht setzen – die Docs-Endpunkte machen die vollständige API-Struktur ohne zusätzliche Authentifizierung sichtbar. |
| `IRRIGATION_VALVE_DRIVER` | `"sim"` \| `"rpi"` | – | Überschreibt `device_config.json`-Einstellung. Wird beim Start einmal gelesen. |
| `IRRIGATION_RELAY_ACTIVE_LOW` | `"true"` \| `"false"` \| `"1"` \| `"0"` | – | Überschreibt `IRRIGATION_RELAY_ACTIVE_LOW` aus `device_config.json`. Akzeptierte Werte für `true`: `"1"`, `"true"`, `"yes"`, `"on"`. |

**Hinweis:** Umgebungsvariablen werden beim Modulimport einmalig gelesen. Änderungen zur Laufzeit haben keinen Effekt – Backend neu starten.

---

## 6. Hard-Limits (`core/config.py`)

Diese Werte sind Teil des Programmcodes und können nur per Software-Update geändert werden. Sie schützen das System vor fehlerhaften oder manipulierten Konfigurationswerten.

| Konstante | Wert | Beschreibung |
|---|---|---|
| `MAX_VALVES` | `6` | Code-Default für MAX_VALVES wenn `device_config.json` fehlt |
| `MAX_RUNTIME_S` | `3600` | Code-Default für maximale Laufzeit (1 Stunde) |
| `MAX_QUEUE_ITEMS` | `50` | Absolutes Maximum an Queue-Einträgen (DoS-Schutz) |
| `MAX_SCHEDULES` | `20` | Absolutes Maximum an gespeicherten Zeitplänen (DoS-Schutz) |
| `MAX_HISTORY_ITEMS` | `20` | Code-Default für Verlauf-Einträge |
| `MAX_CONCURRENT_VALVES` | `2` | Code-Default für gleichzeitige Ventile |
| `HW_CLOSE_MAX_RETRIES` | `5` | Maximale Hardware-Retry-Versuche beim Schließen |
| `HW_RETRY_BACKOFF_BASE_S` | `1.0` | Basis-Backoff in Sekunden (exponentiell: 1, 2, 4, 8, ...) |
| `HW_RETRY_BACKOFF_MAX_S` | `30.0` | Maximaler Backoff-Cap in Sekunden |
| `HW_FAULT_COOLDOWN_S` | `60.0` | Mindest-Wartezeit nach Fault vor Quittierung in Sekunden |
| `CORRUPT_FILE_MAX_KEEP` | `3` | Max. Anzahl `.corrupt-<ts>`-Backup-Dateien pro Datendatei |
| `GLOBAL_LIMIT` | `"120/minute"` | Rate-Limit für alle Endpunkte |
| `MUTATION_LIMIT` | `"30/minute"` | Rate-Limit für schreibende Endpunkte (POST/DELETE) |

---

## 7. GPIO-Pin-Mapping (BCM-Nummerierung)

Der `IRRIGATION_GPIO_PINS`-Wert in `device_config.json` verwendet BCM-Nummerierung (nicht physische Board-Pin-Nummern).

Gültige BCM-Pins: **2..27** (Pins 0 und 1 sind reserviert auf den meisten Boards).

Beispiel-Mapping für typisches 8-Kanal-Relay-Board:

```
Relay-Board-Kanal → BCM-Pin → Physischer Pin
        Kanal 1   →    17   →   Pin 11
        Kanal 2   →    18   →   Pin 12
        Kanal 3   →    27   →   Pin 13
        Kanal 4   →    22   →   Pin 15
        Kanal 5   →    23   →   Pin 16
        Kanal 6   →    24   →   Pin 18
        Kanal 7   →    25   →   Pin 22  (Reserve)
        Kanal 8   →     4   →   Pin 7   (Reserve)
```

<!-- TODO: Tatsächliches Relay-Board und Pin-Mapping eintragen und prüfen -->

Detaillierte Verkabelungsanleitung → [HARDWARE_SETUP.md](HARDWARE_SETUP.md).

---

## 8. `active_low`-Erklärung

**`IRRIGATION_RELAY_ACTIVE_LOW: true`** (Standardwert, empfohlen):
- GPIO LOW (0V) → Relais zieht an → Magnetventil öffnet
- GPIO HIGH (3.3V) → Relais fällt ab → Magnetventil schließt
- Typisch für Relay-Boards mit eingebautem Optokoppler und Pull-Up-Widerstand auf dem IN-Pin
- Sicherheitsrelevant: Beim GPIO-Init (noch vor Software-Konfiguration) liegen Pins auf HIGH → Ventile bleiben geschlossen

**`IRRIGATION_RELAY_ACTIVE_LOW: false`**:
- GPIO HIGH → Relais zieht an → Ventil öffnet
- GPIO LOW → Relais fällt ab → Ventil schließt
- Für Relay-Boards ohne Optokoppler oder mit invertierter Logik

Im Zweifel: Board-Datenblatt prüfen oder Testmessung durchführen. Details → [HARDWARE_SETUP.md](HARDWARE_SETUP.md).
