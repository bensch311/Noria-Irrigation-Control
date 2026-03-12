# Changelog

Alle wesentlichen Änderungen an Noria werden in dieser Datei dokumentiert.

Format basiert auf [Keep a Changelog](https://keepachangelog.com/de/1.0.0/).
Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

---

## [Unreleased]

*(Änderungen, die noch kein Release-Tag haben, kommen hier rein)*

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
[0.10.x] Bugfixes aus Field Testing
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
