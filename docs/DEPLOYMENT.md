# DEPLOYMENT.md – Produktionsbetrieb

Anleitung für die Einrichtung des Bewässerungscomputers auf einem Raspberry Pi für den Produktionseinsatz.

---

## 1. Raspberry Pi vorbereiten

### 1.1 Betriebssystem

<!-- TODO: Konkrete OS-Version eintragen (z.B. Raspberry Pi OS Lite 64-bit, Debian Bookworm, August 2024) -->
<!-- TODO: Installationsweg eintragen (z.B. Raspberry Pi Imager) -->

Empfehlung: Raspberry Pi OS Lite (ohne Desktop), 64-bit.

Nach dem ersten Boot:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-pip python3-venv
```

### 1.2 Python-Version prüfen

Das System erfordert Python ≥ 3.11.

```bash
python3 --version
# Erwartete Ausgabe: Python 3.11.x oder höher
```

Falls eine ältere Version installiert ist, Python 3.11 aus den Backports oder per Quellcode-Build installieren.

### 1.3 Systembenutzer anlegen (empfohlen)

```bash
# Dedizierten Benutzer ohne Login anlegen
sudo adduser --system --group --no-create-home irrigation

# Benutzer der GPIO-Gruppe hinzufügen (für RPi.GPIO)
sudo usermod -aG gpio irrigation
```

---

## 2. Anwendung installieren

### 2.1 Code deployen

```bash
# Als root oder sudo-Benutzer
sudo mkdir -p /opt/bewaesserung
sudo chown irrigation:irrigation /opt/bewaesserung

# Als irrigation-Benutzer (oder mit sudo -u irrigation)
cd /opt/bewaesserung
git clone <REPO-URL> .
```

### 2.2 Python-Umgebung erstellen

```bash
cd /opt/bewaesserung
python3 -m venv .venv
source .venv/bin/activate

# RPi.GPIO ist auf dem Pi verfügbar – MUSS installiert werden
pip install -r requirements.txt
# Falls RPi.GPIO in requirements.txt auskommentiert ist:
pip install RPi.GPIO
```

### 2.3 Datenverzeichnis einrichten

```bash
# Datenverzeichnis anlegen (falls nicht vorhanden)
mkdir -p /opt/bewaesserung/data
mkdir -p /opt/bewaesserung/logs

# Eigentümer setzen
chown -R irrigation:irrigation /opt/bewaesserung/data
chown -R irrigation:irrigation /opt/bewaesserung/logs
```

---

## 3. Konfiguration

### 3.1 Hardware-Konfiguration

`/opt/bewaesserung/data/device_config.json` anlegen:

```json
{
  "version": 1,
  "device": {
    "MAX_VALVES": 6,
    "IRRIGATION_VALVE_DRIVER": "rpi",
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

**Wichtig:** `IRRIGATION_RELAY_ACTIVE_LOW: true` ist die korrekte Einstellung für die meisten handelsüblichen 8-Kanal-Relay-Boards. Details → [HARDWARE_SETUP.md](HARDWARE_SETUP.md).

Vollständige Feldbeschreibung → [CONFIGURATION.md](CONFIGURATION.md).

### 3.2 Frontend-Konfiguration

`/opt/bewaesserung/data/frontend_config.json` anlegen:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "poll_status_s": 1,
  "poll_slow_s": 5,
  "backend_fail_threshold": 3,
  "health_timeout_s": 0.8,
  "anzahl_ventile_fallback": 6
}
```

Wenn Backend und Frontend auf demselben Pi laufen: `base_url` = `http://127.0.0.1:8000`.  
Wenn das Frontend von einem anderen Gerät aus auf den Pi zugreift: `base_url` = `http://<PI-IP>:8000`.

### 3.3 CORS-Konfiguration

CORS steuert, von welchen Browser-Origins aus die API aufgerufen werden darf.

Die erlaubten Origins werden über die Umgebungsvariable `ALLOWED_ORIGINS` gesetzt (komma-separiert):

```bash
# Beispiel: Pi-IP und localhost erlauben
ALLOWED_ORIGINS=http://192.168.1.100:8080,http://localhost:8080
```

Standardwert ohne Umgebungsvariable: `http://localhost:8080`

**Hinweis:** CORS betrifft nur Browser-seitige Requests (JavaScript). Server-seitige Requests des Shiny-Frontends sind nicht betroffen.

---

## 4. systemd-Services einrichten

Zwei separate Services: Backend (FastAPI) und Frontend (Shiny).

### 4.1 Backend-Service

Datei anlegen: `/etc/systemd/system/irrigation-backend.service`

```ini
[Unit]
Description=Bewässerungscomputer Backend (FastAPI)
After=network.target
Wants=network.target

[Service]
Type=notify
User=irrigation
Group=irrigation
WorkingDirectory=/opt/bewaesserung
Environment="PATH=/opt/bewaesserung/.venv/bin"
Environment="ALLOWED_ORIGINS=http://192.168.1.100:8080,http://localhost:8080"
# ENABLE_DOCS absichtlich NICHT gesetzt → Swagger UI und ReDoc sind deaktiviert.
# Nur für Entwicklung lokal aktivieren: ENABLE_DOCS=true uvicorn ...
ExecStart=/opt/bewaesserung/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log
Restart=always
RestartSec=5
WatchdogSec=30
KillSignal=SIGTERM
TimeoutStopSec=15

# Sicherheitshärtung
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/opt/bewaesserung/data /opt/bewaesserung/logs

# GPIO-Zugriff benötigt
DeviceAllow=/dev/gpiomem rw
SupplementaryGroups=gpio

[Install]
WantedBy=multi-user.target
```

**Wichtig:**
- `Type=notify` und `WatchdogSec=30`: Das Backend sendet `WATCHDOG=1` alle ~15 Sekunden via `sd_notify`. Wenn der Watchdog innerhalb von 30 Sekunden keinen Heartbeat empfängt, wird der Service neu gestartet.
- `ALLOWED_ORIGINS`: Mit der tatsächlichen IP des Pi ersetzen.

### 4.2 Frontend-Service

Datei anlegen: `/etc/systemd/system/irrigation-frontend.service`

```ini
[Unit]
Description=Bewässerungscomputer Frontend (Shiny)
After=network.target irrigation-backend.service
Wants=irrigation-backend.service

[Service]
Type=simple
User=irrigation
Group=irrigation
WorkingDirectory=/opt/bewaesserung
Environment="PATH=/opt/bewaesserung/.venv/bin"
ExecStart=/opt/bewaesserung/.venv/bin/shiny run app.py --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=10

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/opt/bewaesserung/data /opt/bewaesserung/logs

[Install]
WantedBy=multi-user.target
```

### 4.3 Services aktivieren und starten

```bash
sudo systemctl daemon-reload

sudo systemctl enable irrigation-backend
sudo systemctl enable irrigation-frontend

sudo systemctl start irrigation-backend
sudo systemctl start irrigation-frontend
```

### 4.4 Service-Status prüfen

```bash
sudo systemctl status irrigation-backend
sudo systemctl status irrigation-frontend

# Live-Log verfolgen
sudo journalctl -u irrigation-backend -f
sudo journalctl -u irrigation-frontend -f
```

---

## 5. API-Key abrufen und im Frontend hinterlegen

Nach dem ersten Start des Backends wurde der API-Key automatisch generiert:

```bash
sudo cat /opt/bewaesserung/data/api_key.txt
```

Das Frontend liest den Key automatisch aus `data/api_key.txt`. Bei korrektem Setup (Backend und Frontend auf demselben Pi, gleicher `WorkingDirectory`) ist keine manuelle Konfiguration erforderlich.

Wenn der Key ungültig oder nicht lesbar ist, zeigt das Frontend einen roten Auth-Modal. In diesem Fall:
1. Backend-Log prüfen: `sudo journalctl -u irrigation-backend --since "5 minutes ago"`
2. Dateiberechtigung prüfen: `ls -la /opt/bewaesserung/data/api_key.txt` (muss `600` sein)
3. Key manuell in das Modal eingeben oder `api_key.txt` vom Pi kopieren

---

## 6. Berechtigungen prüfen

```bash
# api_key.txt muss 600 sein (nur Owner lesen/schreiben)
ls -la /opt/bewaesserung/data/api_key.txt
# Erwartete Ausgabe: -rw------- 1 irrigation irrigation ...

# Das Backend setzt 600 automatisch beim Erstellen und Laden.
# Falls falsch:
sudo chmod 600 /opt/bewaesserung/data/api_key.txt
sudo chown irrigation:irrigation /opt/bewaesserung/data/api_key.txt
```

---

## 7. Firewall-Konfiguration

**Empfehlung:** Port 8000 (Backend) nur im lokalen Netzwerk erreichbar lassen. Port 8080 (Frontend) kann für alle lokalen Geräte offen sein.

Beispiel mit `ufw`:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8000
sudo ufw allow from 192.168.1.0/24 to any port 8080
sudo ufw deny 8000
sudo ufw deny 8080
sudo ufw enable
```

<!-- TODO: Netzwerkstruktur prüfen und Firewall-Regeln anpassen -->

---

## 8. Reverse-Proxy mit nginx (optional)

Wenn das Frontend über Standard-HTTP-Port 80 erreichbar sein soll oder HTTPS benötigt wird, empfiehlt sich nginx als Reverse-Proxy.

```bash
sudo apt install -y nginx
```

Konfiguration `/etc/nginx/sites-available/bewaesserung`:

```nginx
server {
    listen 80;
    server_name <PI-HOSTNAME-ODER-IP>;

    # Frontend (Shiny)
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Backend-API (wenn direkter Zugriff gewünscht)
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/bewaesserung /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 9. TLS/HTTPS-Setup (optional, empfohlen für Fernzugriff)

### Option A: Selbstsigniertes Zertifikat (lokales Netzwerk)

```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/bewaesserung.key \
  -out /etc/ssl/certs/bewaesserung.crt \
  -subj "/CN=<PI-IP-ODER-HOSTNAME>"
```

nginx-Konfiguration ergänzen:
```nginx
server {
    listen 443 ssl;
    ssl_certificate /etc/ssl/certs/bewaesserung.crt;
    ssl_certificate_key /etc/ssl/private/bewaesserung.key;
    # ... rest der Konfiguration wie oben
}
```

### Option B: Let's Encrypt (benötigt öffentlichen DNS und Port 80/443)

<!-- TODO: Domainname eintragen wenn Let's Encrypt verwendet werden soll -->

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <DOMAIN>
```

**Wichtig nach HTTPS-Umstellung:** `ALLOWED_ORIGINS` und `base_url` in `frontend_config.json` entsprechend auf `https://` aktualisieren.

---

## 10. Updates einspielen

```bash
cd /opt/bewaesserung

# Dienste stoppen
sudo systemctl stop irrigation-frontend irrigation-backend

# Code aktualisieren
git pull

# Abhängigkeiten aktualisieren (falls requirements.txt geändert)
source .venv/bin/activate
pip install -r requirements.txt

# Dienste neu starten
sudo systemctl start irrigation-backend irrigation-frontend

# Status prüfen
sudo systemctl status irrigation-backend irrigation-frontend
```

---

## 11. Neustart-Verhalten

Nach einem Systemabsturz oder Stromausfall:

1. Backend startet neu (via systemd `Restart=always`)
2. Beim Startup: `close_all()` via IO-Worker → alle Ventile werden geschlossen (Fail-Safe)
3. `active_runs` wird geleert, `paused=False` (Laufzeit-Zustand wird NICHT wiederhergestellt)
4. Queue und Zeitpläne werden aus `data/queue.json` bzw. `data/schedules.json` wiederhergestellt
5. Die Queue-State wird auf `"bereit"` zurückgesetzt (nicht auf den gespeicherten Wert)
6. Systemd-Watchdog läuft neu an

**Sicherheitsgarantie:** Nach einem Neustart laufen keine Ventile, bis sie explizit gestartet werden.
