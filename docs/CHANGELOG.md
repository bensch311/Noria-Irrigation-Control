# Changelog

Alle wesentlichen Änderungen an Noria werden in dieser Datei dokumentiert.

Format basiert auf [Keep a Changelog](https://keepachangelog.com/de/1.0.0/).
Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

---

## [Unreleased]

*(Änderungen, die noch kein Release-Tag haben, kommen hier rein)*

---

## [0.13.0] – Zonen-Pause

### Added
- **Zonen-Pause** (`zone_pause_s`): Konfigurierbare Wartepause zwischen zwei
  Bewässerungsgruppen (0–3600 Sekunden = 0–60 Minuten). Greift sobald
  `active_runs` nach dem Ende einer Zone oder parallelen Gruppe leer wird
  und noch Queue-Items warten.
  - Im **Parallel-Modus** gilt: erst wenn *alle* parallelen Zonen fertig sind,
    startet die Pause – nicht nach jeder einzelnen Zone.
  - Gilt für **alle Quellen**: Queue, Zeitplan (`schedule`), Sensor-Trigger.
  - `zone_pause_s` wird in `user_settings.json` persistiert (Schlüssel
    `ZONE_PAUSE_S`). Default: 0 (keine Pause).
  - Neues State-Feld `state.zone_pause_until` (rein in-memory, nicht
    persistiert): monotonic-Timestamp bis wann keine neuen Items gestartet
    werden dürfen.
- **`/status`-Antwort**: zwei neue Felder:
  - `zone_pause_s` – konfigurierte Pause in Sekunden.
  - `zone_pause_remaining_s` – verbleibende Pause-Zeit in Sekunden (0 wenn
    keine Pause aktiv).
- **`GET /settings`**: liefert jetzt `zone_pause_s`.
- **`POST /settings`**: akzeptiert jetzt `zone_pause_s` (0–3600).
- **Dashboard** (Frontend): zeigt `⏳ MM:SS verbleibend`-Badge während eine
  Zonen-Pause aktiv ist.
- **Einstellungen-Tab** (Frontend): neuer Slider `sld_zone_pause_min` (0–60 Min).

### Changed
- **`/stop`**: setzt `state.zone_pause_until = 0.0` zurück, damit ein
  manueller Stop eine laufende Zonen-Pause sofort aufhebt.
- **`/queue/clear`**: setzt `state.zone_pause_until = 0.0` zurück, da ohne
  Queue-Items kein Pause-Fenster mehr benötigt wird.
- **`core/config.py`**: neue Konstante `DEFAULT_ZONE_PAUSE_S = 0`.
- **`models/requests.py`**: `SettingsUpdateRequest` um `zone_pause_s`
  (0–3600, Pydantic-geprüft) erweitert.
- **`services/persistence.py`**: `_default_user_settings_payload`,
  `load_user_settings_from_disk` und `save_user_settings_to_disk` um
  `ZONE_PAUSE_S` erweitert.
- **`services/timer.py`**: Queue-Fill-Block prüft `zone_pause_until` vor dem
  Starten neuer Items; setzt `zone_pause_until` nach erfolgreichem Schließen
  wenn `active_runs` leer wird und Queue noch Items enthält.
- **`services/engine.py`**: `engine_status_payload_locked` liefert
  `zone_pause_s` und `zone_pause_remaining_s` in beiden Status-Branches.
- **`core/state.py`**: `RunState` um `zone_pause_s` (User-Setting) und
  `zone_pause_until` (In-Memory-Timestamp) erweitert.

---

## [0.12.0] – I2C Relay HAT

### Added
- **`I2cRelayValveDriver`**: Neuer Hardware-Treiber für Sequent Microsystems
  Relay HATs via I2C/SMBus (`smbus2`).
  - Unterstützt **8-Relay HAT** (PCA9554-kompatibel, Adresse 0x38–0x3F / 0x20–0x27)
  - Unterstützt **16-Relay HAT** (PCA9555-kompatibel, Adresse 0x20–0x27)
  - Register-Konstanten direkt aus offiziellen C-Headern (`relay.h` / `relay8.h`)
  - Sicherheitskritische Init-Reihenfolge: Output-Latch → Config-Register
    (verhindert kurzes Einschalten aller Relais beim Start durch PCA955x Power-on-Default)
  - Bitmask-State in-memory (kein I2C-Readback nötig)
  - `close_all()` best-effort: In-memory-State wird auch bei HW-Fehler auf 0 gesetzt
- **`validate_i2c_config()`**: Validierungsfunktion prüft HAT-Typ,
  I2C-Bus, Adressbereich und max_valves vor Driver-Init.
- **`smbus2`** zu `requirements.txt` ergänzt.
- **`state.py`**: Drei neue Felder in `RunState`:
  `relay_hat_type`, `i2c_bus`, `i2c_address`.
- **`device_config.json`**: Drei neue Felder im `device`-Block:
  `IRRIGATION_RELAY_HAT_TYPE`, `IRRIGATION_I2C_BUS`, `IRRIGATION_I2C_ADDRESS`.
- **`install.sh`**: Vollständige I2C-Integration:
  - Interaktive Treiber-Auswahl (GPIO direkt / 8-Relay HAT / 16-Relay HAT)
  - Automatische I2C-Kernel-Aktivierung (`dtparam=i2c_arm=on`, `i2c-dev`)
  - Bedingte GPIO-Pin- vs. I2C-Konfigurations-Sektion
  - `device_config.json` und `.env` mit korrektem Treibermodus generiert
  - `DeviceAllow` für `/dev/i2c-0` und `/dev/i2c-1` im systemd-Service

### Fixed
- **Sensor-Engine Cooldown auf frischen Systemen** (`sensor_engine.py`):
  `sensor_last_triggered.get(sensor_id, 0.0)` lieferte auf CI-Runnern und
  frisch gestarteten Pis mit Uptime < `sensor_cooldown_s` einen falschen
  elapsed-Wert → Sensor wurde fälschlich blockiert.
  Fix: `None`-Sentinel statt `0.0` — kein Eintrag = noch nie ausgelöst = kein Cooldown.

### Changed
- `services/valve_driver.py`: `get_valve_driver()` um `mode == "i2c"` Zweig erweitert.
- `services/persistence.py`: `load_device_config_from_disk()` erkennt `"i2c"` als
  gültigen Treibermodus; parst I2C-Adresse als Dezimalzahl und Hex-String.
- `requirements.txt`: `RPi.GPIO` → `rpi-lgpio` (Pi 5 / RP1-Chip-Kompatibilität).
- `version.py`: Bump `0.11.0 → 0.12.0`

### Infrastructure
- 47 neue Tests für `I2cRelayValveDriver` und `validate_i2c_config`
  (`tests/test_valve_driver.py`): Init-Reihenfolge, Bitmask-Logik,
  Register-Aufteilung Low/High-Byte, best-effort `close_all()`, cleanup.

---

## [0.11.0] – Sensor-Integration

### Added
- **`RpiGpioSwitchSensorDriver`**: Neuer Hardware-Treiber für digitale
  Trockenkontakt-Sensoren (z.B. MMM TXS Schalttensiometer) via lgpio / rpi-lgpio.
  - Liest GPIO-Eingangspins (BCM-Nummerierung) mit optionalem internen Pull-Up
  - Lazy-Init: Treiber wird erst beim ersten Poll initialisiert
  - `cleanup()` nur wenn tatsächlich initialisiert (vermeidet ungewollten Lazy-Init beim Shutdown)
- **Sensor-Engine** (`sensor_engine.py`): Background-Polling-Loop mit konfigurierbarem
  Intervall; wertet Readings aus und stellt `QueueItem`s für zugeordnete Zonen ein.
  - Priority-Queue-Strategie: 3 Fälle (leere Queue / läuft / befüllt-idle)
  - Cooldown pro Sensor (nicht pro Zone); `sensor_last_triggered` erst beim
    tatsächlichen Ventilstart gesetzt (engine.py COMMIT), nicht beim Einreihen
  - `sensor_pending_zones` als Sperrmechanismus gegen Neu-Trigger während
    Zonen noch in Queue oder active_runs warten
- **Sensor-Zuordnung**: `sensor_assignments.json` persistiert Sensor→Zonen-Mapping
  und per-Sensor-Parameter (`cooldown_s`, `duration_s`).
  Editierbar im Sensoren-Tab der Benutzeroberfläche.
- **Sensoren-Tab** (Frontend `app.py`): Sensor-Statusanzeige, Zuordnungs-UI,
  per-Sensor Cooldown- und Dauer-Einstellungen.
- **`device_config.json`**: Neue Felder im `sensors`-Block:
  `IRRIGATION_SENSOR_DRIVER`, `IRRIGATION_SENSOR_INTERNAL_PULL_UP`,
  `IRRIGATION_SENSOR_PINS`, `IRRIGATION_SENSOR_POLLING_INTERVAL_S`,
  `IRRIGATION_SENSOR_COOLDOWN_S`, `IRRIGATION_SENSOR_DEFAULT_DURATION_S`.
- **`state.py`**: Neue Felder in `RunState` für Sensor-Konfiguration,
  Sensor-Laufzeitdaten (`sensor_readings`, `sensor_last_triggered`,
  `sensor_pending_zones`) und Sensor-Zuordnung (`sensor_zone_assignments`,
  `sensor_settings_by_id`).
- **`install.sh`**: Interaktive Sensor-Konfiguration (Anzahl, GPIO-Pins,
  Pull-Up, Polling-Intervall); Sensor-Pins werden auf Duplikate mit
  Ventil-Pins geprüft.

### Changed
- `services/persistence.py`: Lädt und speichert Sensor-Konfiguration aus
  `device_config.json` und `sensor_assignments.json`.
- `core/lifecycle.py`: Sensor-Engine-Thread gestartet/gestoppt.
- `version.py`: Bump `0.10.2 → 0.11.0`

### Infrastructure
- Neue Testdateien `tests/test_sensor_engine.py` und `tests/test_sensor_driver.py`

---

## [0.10.2] – System-Monitoring

### Added
- **`GET /system/info`**: Neuer Endpunkt liefert OS-Metriken (Disk, RAM,
  Uptime, Netzwerk-Interfaces mit LAN/WLAN-Typ, SSID und Signalstärke).
  Alle Felder best-effort: Fehler → `null`, kein HTTP-500.
- **Systeminfo-Card** (Frontend): zwei Abschnitte „Konfiguration" und
  „System" mit Uptime, RAM-Nutzung, Speicherplatz, Netzwerk-Status,
  WLAN-SSID und Signalqualität.
- **`app_helpers.py`**: neue Formatter `fmt_uptime()`, `fmt_disk()`,
  `fmt_memory()`, `fmt_signal()` – pure functions, vollständig getestet.
- **`psutil==6.1.1`** in `requirements.txt` ergänzt.

### Changed
- `api/routes_system.py`: neuer Endpunkt `GET /system/info`.
- `app.py`: neue `_sysinfo_data()` reactive.calc (Slow-Poll),
  Import der neuen Formatter.
- `version.py`: Bump `0.10.1 → 0.10.2`

---

## [0.10.1] – Log-Download

### Added
- **Log-Download**: Neuer Endpunkt `GET /system/logs/download` liefert alle
  vorhandenen Log-Dateien (`irrigation.jsonl` + rotierte Backups `.1`–`.10`)
  als ZIP-Archiv in-memory – kein temporäres File auf Disk.
  Dateiname: `noria-logs-YYYY-MM-DD.zip`. Zugriff wird geloggt (`log_download_requested`).
- **Frontend**: Neue Card „Diagnose-Logs" in den Einstellungen (unterhalb
  Systeminfo) mit „Logs herunterladen"-Button. `@render.download` leitet die
  ZIP über den authentifizierten `_session`-Request an den Browser weiter.

### Changed
- `api/routes_system.py`: neuer Endpunkt, rate-limitiert auf 5/min.
- `app.py`: neue `_download_logs`-Funktion und Card im Settings-Tab.
- `version.py`: Bump `0.10.0 → 0.10.1`

---

## [0.10.0] – Neustart-Erkennung (Stromausfall-Detection)

### Added
- **Sentinel-File-Muster** (`data/running.lock`): Backend legt beim Start eine Lock-Datei
  an und löscht sie beim sauberen Shutdown als allererste Aktion. Existiert die Datei beim
  nächsten Start noch → unclean shutdown erkannt (Stromausfall, SIGKILL, OOM-Kill).
  Muster analog zu PostgreSQL WAL, SQLite lock-File, Redis RDB-Prüfung.
- **Neuer Endpunkt** `POST /system/ack-restart` (in `api/routes_system.py`):
  Quittiert den Neustart-Hinweis; setzt `state.unclean_restart=False` zurück.
  Idempotent, erfordert API-Key-Authentifizierung.
- **Neue Datei** `api/routes_system.py`: Grundlage für weitere System-Endpunkte.
- **Neue `/health`-Felder**: `unclean_restart` (bool) und `restart_detected_at` (ISO-8601-String)
  für Monitoring und Frontend-Integration.
- **Neustart-Modal im Frontend** (`app.py`): erscheint einmalig nach Backend-Neustart
  mit unclean-Flag; Bediener bestätigt mit „Verstanden" → ACK an Backend → Modal schließt sich.
  Modal erscheint nicht erneut bis zum nächsten unclean Restart.
- **Neue State-Felder** in `RunState`: `unclean_restart: bool`, `restart_detected_at: str`.

### Changed
- `core/lifecycle.py`: Startup-Sequenz um Sentinel-Check (Schritt 5) und Lock-Anlegen
  (Schritt 11) erweitert. Shutdown-Sequenz: Lock-Löschen als allererste Aktion vor `STOPPING=1`.
- `api/routes_health.py`: Response um `unclean_restart` und `restart_detected_at` ergänzt.
- `core/config.py`: Neue Konstante `RUNNING_LOCK_FILE` (`data/running.lock`).
- `main.py`: `system_router` importiert und registriert.
- `tests/conftest.py`: `system_router` in `app`-Fixture aufgenommen.
- `app.py`: `_ping_health()` gibt nun `tuple[bool, dict]` zurück (kein zweiter HTTP-Request
  pro Poll-Zyklus).

### Infrastructure
- Test-Suite: neue Testdatei `tests/test_system.py` (8 Tests für `/system/ack-restart`)
- Erweiterte Tests in `tests/test_health.py` für neue Health-Response-Felder

---

## [0.9.0] – Feature Complete / Pre-Production

### Added
- API-Key Authentifizierung (X-API-Key Header, `./data/api_key.txt`)
- Rate Limiting via SlowAPI (globale + strikte Mutation-Tier für POST/DELETE)
- CORS-Middleware mit korrekter Outermost-Reihenfolge für Preflight-Handling
- Input Validation Hardening via Pydantic Literal Types und Field Validators
- Audit Logging mit Client-IP-Extraktion (`get_client_ip()` in `core/security.py`)
- Zentrale Versionsverwaltung via `version.py` (SemVer, Single Source of Truth)
- Health-Endpoint liefert jetzt `app_version` (SemVer-String) zusätzlich zur API-Version
- FastAPI-App-Metadaten (title, version) aus `version.py`

### Fixed
- Thread-Safety Bug im Timer-Modul (GPIO-Calls serialisiert über `io_worker`-Thread)
- Stop-Route Partial-Failure-Semantik (Prepare/Execute/Commit-Pattern;
  fehlgeschlagene Zonen bleiben in `active_runs` für Retry)

### Infrastructure
- Systemd-Service-Integration (`irrigation.service`)
- Power-Loss Recovery via `runtime_state.json`
- Test-Suite: 331 passing Tests

---

## Versionshistorie (Zukunft)

```
[0.12.x] Bugfixes aus Field Testing
[1.0.0]  Production Release – nach abgeschlossener Field Testing Checkliste
[1.1.0]  Erstes Feature-Release (z.B. Wetterintegration, Prometheus Monitoring)
[2.0.0]  Breaking Change (z.B. Datenbankumstieg, inkompatibles Datenformat)
```

---

## Release-Prozess

```bash
# 1. version.py anpassen
# 2. Diesen CHANGELOG aktualisieren
# 3. Commit
git commit -m "chore: bump version to X.Y.Z"

# 4. Tag setzen
git tag -a vX.Y.Z -m "Noria X.Y.Z – <Kurzbeschreibung>"

# 5. Pushen
git push && git push --tags
```
