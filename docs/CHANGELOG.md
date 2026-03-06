# Changelog

Alle wesentlichen Änderungen an Noria werden in dieser Datei dokumentiert.

Format basiert auf [Keep a Changelog](https://keepachangelog.com/de/1.0.0/).
Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

---

## [Unreleased]

*(Änderungen, die noch kein Release-Tag haben, kommen hier rein)*

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
[0.9.x]  Bugfixes aus Field Testing
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
