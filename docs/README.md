<p align="center">
  <img src="www/noria-logo-static-light.svg" width="260" alt="Noria Logo"/>
</p>

<p align="center">
  <strong>Produktionsreifes Bewässerungssteuersystem für den professionellen Einsatz in Gemüsebaubetrieben.</strong>
</p>

---

# Noria – Bewässerungssteuerung für Raspberry Pi

---

## Übersicht

Der Noria ist ein Python-basiertes System, das über Relaismodule Magnetventile ansteuert und eine browserbasierte Benutzeroberfläche für manuelle und automatische Steuerung bietet.

**Kernfunktionen:**
- Manuelle Einzelventilsteuerung mit konfigurierbarer Laufzeit
- Bewässerungswarteschlange (Queue) mit mehreren Einträgen
- Wochentag-basierte Zeitpläne (einmalig oder wiederkehrend)
- Parallelbetrieb mehrerer Ventile (konfigurierbar)
- Hardware-Fault-Erkennung mit exponentiellem Backoff-Retry und Operator-Quittierung
- Vollständiger Verlauf abgeschlossener Läufe
- Stromausfall-sichere Persistenz (atomares Schreiben, Korruptionserkennung)
- API-Key-Authentifizierung, Rate-Limiting, CORS, Security-Header

---

## Systemarchitektur (Überblick)

```
┌──────────────────┐   HTTP/JSON (X-API-Key)   ┌──────────────────────┐
│  Shiny-Frontend  │ ─────────────────────────► │   FastAPI-Backend    │
│   (app.py)       │ ◄─────────────────────────  │   (main.py)          │
│   Port 8080      │                            │   Port 8000          │
└──────────────────┘                            └────────┬─────────────┘
                                                         │  IO-Worker-Thread
                                                ┌────────▼─────────────┐
                                                │  RPi.GPIO / Simulation│
                                                └────────┬─────────────┘
                                                         │
                                                ┌────────▼─────────────┐
                                                │   Magnetventile      │
                                                └──────────────────────┘
```

Das Backend verwaltet alle Hintergrund-Threads (Timer, Scheduler, Persistenz, IO-Worker, Watchdog) und stellt eine REST-API bereit. Das Frontend ist eine Python-Shiny-Express-App, die per HTTP-Polling den Systemzustand abfragt.

Vollständige Architekturbeschreibung: → [ARCHITECTURE.md](ARCHITECTURE.md)

---

## Systemanforderungen

| Komponente | Anforderung |
|---|---|
| Hardware | Raspberry Pi 4B (2 GB RAM) oder Pi 5 (4/8 GB RAM) empfohlen |
| Betriebssystem | **Ohne Kiosk:** Raspberry Pi OS Lite 64-bit, Debian Bookworm · **Mit Kiosk:** Raspberry Pi OS with Desktop 64-bit, Debian Bookworm |
| Python | ≥ 3.11 |
| Speicherplatz | ≥ 1 GB frei (Logs, Daten, Venv) |
| Netzwerk | Lokales LAN oder direkter Zugriff; kein Internet erforderlich |
| Relay-Board | <!-- TODO: Relay-Board-Modell eintragen (z.B. 8-Kanal 5V Optokoppler-Relay) --> |

---

## Verzeichnisstruktur

```
.
├── main.py                   # FastAPI-Einstiegspunkt (Backend, Port 8000)
├── app.py                    # Shiny-Express-Frontend (Port 8080)
├── app_helpers.py            # Frontend-Hilfsfunktionen (Config, Formatierung)
├── requirements.txt          # Python-Abhängigkeiten (pinned)
├── pytest.ini                # Test-Konfiguration
│
├── scripts/
│   ├── install.sh            # Installations-Script (einmalig, als root)
│   └── update.sh             # Update-Script (nach git pull, als root)
│
├── api/                      # FastAPI-Router und Middleware
│   ├── errors.py             # Globale Exception-Handler
│   ├── middleware.py         # SecurityHeadersMiddleware
│   ├── routes_control.py     # /start /stop /pause /resume /status /fault/clear /automation /parallel
│   ├── routes_health.py      # /health (ohne Auth, für Monitoring)
│   ├── routes_history.py     # /history
│   ├── routes_queue.py       # /queue /queue/add /queue/start /queue/pause /queue/clear
│   ├── routes_schedule.py    # /schedule /schedule/add /schedule/enable|disable|delete
│   └── routes_settings.py    # /settings
│
├── core/                     # Kernmodule
│   ├── config.py             # Hard-Limits, Dateipfade, ENV-Variablen
│   ├── lifecycle.py          # Startup/Shutdown-Sequenz (FastAPI lifespan)
│   ├── limiter.py            # Rate-Limiting (SlowAPI, 120/min global, 30/min Mutationen)
│   ├── logging.py            # Strukturiertes JSONL-Logging (RotatingFileHandler)
│   ├── security.py           # API-Key-Authentifizierung (X-API-Key Header)
│   └── state.py              # Globaler In-Memory-Zustand (RunState + Dataclasses)
│
├── models/
│   └── requests.py           # Pydantic-Request-Modelle
│
├── services/                 # Business-Logik und Hardware-Services
│   ├── engine.py             # Ventilstart-Logik, Status-Payload
│   ├── io_worker.py          # Serialisierter Hardware-Thread (alle GPIO-Ops)
│   ├── persistence.py        # Atomares Schreiben/Lesen (schedules, queue, history, config)
│   ├── scheduler.py          # Zeitplan-Loop (prüft Zeitpläne, startet Queue-Items)
│   ├── timer.py              # Timer-Loop (schließt Ventile nach Ablauf, Backoff-Retry)
│   └── valve_driver.py       # Hardware-Abstraktion (SimValveDriver / RpiGpioValveDriver)
│
├── tests/                    # Pytest-Testsuite
│   ├── conftest.py           # Globale Fixtures, State-Reset, Mock-IO-Worker
│   └── test_*.py             # Modul-Tests
│
├── data/                     # Laufzeit-Daten (nicht in Git)
│   ├── api_key.txt           # API-Key (64 Hex-Zeichen, chmod 600)
│   ├── device_config.json    # Hardware-Konfiguration (GPIO, Treiber)
│   ├── frontend_config.json  # Frontend-Verbindungsparameter
│   ├── user_settings.json    # Benutzereinstellungen
│   ├── runtime_state.json    # Laufzeit-Zustand (Parallel-Modus etc.)
│   ├── schedules.json        # Persistierte Zeitpläne
│   ├── queue.json            # Persistierte Queue
│   └── history.json          # Verlauf abgeschlossener Bewässerungsläufe
│
├── logs/                     # Log-Dateien (nicht in Git)
│   └── irrigation.jsonl      # JSONL-Log (max. 10 MB, bis zu 10 Rotationsdateien)
│
└── www/                      # Statische Frontend-Assets (von Shiny als Root serviert)
    ├── app.css               # Frontend-CSS
    └── logo.svg              # Optionales Navbar-Logo (konfigurierbar)
```

---

## Produktionsbetrieb (Raspberry Pi)

### OS-Auswahl

- **Kein lokaler Bildschirm** (Bedienung per Browser vom PC/Tablet): → **Raspberry Pi OS Lite 64-bit** (Bookworm)
- **Direkt angeschlossener Touchscreen** (Kiosk-Modus): → **Raspberry Pi OS with Desktop 64-bit** (Bookworm)

Installation immer per **Raspberry Pi Imager**. X11 muss nicht manuell ausgewählt werden — das install.sh-Script setzt X11 bei Bedarf automatisch.

### Schnellinstallation

```bash
# System aktualisieren und git installieren
sudo apt update && sudo apt upgrade -y
sudo apt install -y git

# Repository klonen
git clone <REPO-URL> ~/noria

# Installations-Script ausführen (einmalig, interaktiv)
sudo bash ~/noria/scripts/install.sh
```

Das Script fragt IP-Adresse, Ventilanzahl, GPIO-Pins und optional den Kiosk-Modus ab — für alle anderen Einstellungen einfach Enter drücken. Danach sind beide systemd-Services aktiv und starten bei jedem Neustart automatisch.

**Oberfläche aufrufen:** `http://<PI-IP>:8080`

Bei aktiviertem Kiosk-Modus nach der Installation einmal neu starten:

```bash
sudo reboot
```

### Updates einspielen

```bash
cd ~/noria
git pull
sudo bash scripts/update.sh
```

Vollständige Deployment-Anleitung (OS-Installation, Kiosk-Modus, systemd, Firewall, HTTPS): → **[DEPLOYMENT.md](DEPLOYMENT.md)**

---

## Quick-Start (Entwicklung / erster Test)

### 1. Repository klonen und Abhängigkeiten installieren

```bash
git clone <REPO-URL> noria
cd noria

python3 -m venv .venv
source .venv/bin/activate

# Auf Nicht-Pi-Systemen: RPi.GPIO-Zeile in requirements.txt auskommentieren
pip install -r requirements.txt
```

### 2. Backend starten (Simulationsmodus, kein GPIO erforderlich)

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Beim ersten Start wird automatisch erstellt:
- `data/api_key.txt` – kryptografisch sicherer 256-bit-Key
- `data/device_config.json` – Vorlage (Simulationstreiber, 6 Ventile)
- `data/user_settings.json` – Benutzer-Defaults

### 3. API-Key auslesen

```bash
cat data/api_key.txt
# Ausgabe: a3f8e2d1b7c4...  (64 Hex-Zeichen)
```

### 4. Frontend starten

```bash
shiny run app.py --host 127.0.0.1 --port 8080
```

Frontend aufrufen: `http://localhost:8080`

Beim ersten Aufruf wird der API-Key aus `data/api_key.txt` automatisch geladen.

---

## Weiterführende Dokumentation

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Installations-Script, systemd, GPIO-Setup, Firewall, HTTPS | Inbetriebnahme |
| [CONFIGURATION.md](CONFIGURATION.md) | Alle Konfigurationsfelder mit Typen und Defaults | Einrichtung |
| [OPERATIONS.md](OPERATIONS.md) | Log-Referenz, Fault-Behandlung, Backup & Recovery | Betrieb |
| [API_REFERENCE.md](API_REFERENCE.md) | Alle Endpunkte, Request/Response, curl-Beispiele | Frontend-Dev / Integration |
| [HARDWARE_SETUP.md](HARDWARE_SETUP.md) | GPIO-Verkabelung, Relay-Board-Anleitung | Erstinstallation |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Thread-Modell, State-Machine, Concurrency-Design | Vertiefung / Wartung |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Lokale Entwicklung, Tests, neue Routen | Weiterentwicklung |

---

## Lizenz

<!-- TODO: Lizenz eintragen -->
