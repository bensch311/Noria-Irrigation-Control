# DEPLOYMENT.md – Produktionsbetrieb

Anleitung für die Einrichtung des Norias auf einem Raspberry Pi für den Produktionseinsatz.

---

## 0. Schnellinstallation (empfohlen)

Für die meisten Installationen reicht das mitgelieferte Installations-Script. Es übernimmt alle Schritte aus den Abschnitten 1–4 automatisch und fragt nur das Nötigste ab.

### Voraussetzungen

- Raspberry Pi OS Lite 64-bit (Debian Bookworm oder neuer), frisch installiert
- Internetzugang für pip-Pakete
- Python ≥ 3.11 (in aktuellem Raspberry Pi OS enthalten)

### Schritt 1: System aktualisieren

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
```

### Schritt 2: Repository klonen

```bash
git clone <REPO-URL> ~/noria
```

### Schritt 3: Installations-Script ausführen

```bash
sudo bash ~/noria/scripts/install.sh
```

Das Script fragt:
- IP-Adresse des Pi (wird automatisch erkannt, Enter zum Bestätigen)
- Anzahl der Ventile (Standard: 6)
- GPIO-Pin je Ventil (Standard-Pins vorbelegt, Enter zum Übernehmen)
- Relais-Polarität (Standard: Aktiv-Low – für die meisten Relay-Boards korrekt)
- Maximale Laufzeit und gleichzeitige Ventile

Danach läuft alles automatisch. Nach Abschluss ist die Oberfläche unter `http://<PI-IP>:8080` erreichbar.

### Updates einspielen

```bash
cd ~/noria
git pull
sudo bash scripts/update.sh
```

---

## 1. Manuelle Installation (Referenz / Sonderfälle)

Die folgenden Abschnitte beschreiben die manuelle Installation Schritt für Schritt. Dieser Weg ist nur nötig wenn das Script nicht verwendbar ist (z.B. kein Internet, Sonderumgebung) oder zur Fehlersuche.

---

### 1.1 Betriebssystem

<!-- TODO: Konkrete OS-Version eintragen (z.B. Raspberry Pi OS Lite 64-bit, Debian Bookworm, August 2024) -->
<!-- TODO: Installationsweg eintragen (z.B. Raspberry Pi Imager) -->

Empfehlung: Raspberry Pi OS Lite (ohne Desktop), 64-bit.

Nach dem ersten Boot:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-pip python3-venv build-essential libsystemd-dev
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
sudo adduser --system --group --no-create-home noria

# Benutzer der GPIO-Gruppe hinzufügen (für RPi.GPIO)
sudo usermod -aG gpio noria
```

---

## 2. Anwendung installieren (manuell)

### 2.1 Code deployen

```bash
# Als root oder sudo-Benutzer
sudo mkdir -p /opt/noria/app
sudo chown noria:noria /opt/noria

# Code kopieren (ohne Test- und Dev-Dateien)
sudo rsync -a \
    --exclude='.git/' --exclude='data/' --exclude='logs/' \
    --exclude='test_*.py' --exclude='conftest.py' --exclude='pytest.ini' \
    ~/noria/  /opt/noria/app/
```

### 2.2 Python-Umgebung erstellen

```bash
cd /opt/noria
python3 -m venv venv
source venv/bin/activate

pip install -r app/requirements.txt

# systemd-Integration (für Watchdog / READY=1)
pip install systemd-python
```

### 2.3 Datenverzeichnis einrichten

```bash
mkdir -p /opt/noria/app/data
mkdir -p /opt/noria/app/logs
chown -R noria:noria /opt/noria
chmod 700 /opt/noria/app/data
chmod 700 /opt/noria/app/logs
```

---

## 3. Konfiguration

### 3.1 Hardware-Konfiguration

`/opt/noria/app/data/device_config.json` anlegen:

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

`/opt/noria/app/data/frontend_config.json` anlegen:

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

### 3.3 Umgebungsvariablen (.env)

`/opt/noria/.env` anlegen:

```bash
IRRIGATION_VALVE_DRIVER=rpi
IRRIGATION_RELAY_ACTIVE_LOW=true
ALLOWED_ORIGINS=http://192.168.1.100:8080,http://localhost:8080
```

`ALLOWED_ORIGINS` steuert, von welchen Browser-Origins aus die API aufgerufen werden darf. Muss die tatsächliche IP/URL enthalten, unter der das Frontend erreichbar ist.

**Hinweis:** CORS betrifft nur Browser-seitige Requests (JavaScript). Server-seitige Requests des Shiny-Frontends sind nicht betroffen.

```bash
chmod 640 /opt/noria/.env
chown noria:noria /opt/noria/.env
```

---

## 4. systemd-Services einrichten

Zwei separate Services: Backend (FastAPI) und Frontend (Shiny).

### 4.1 Backend-Service

Datei anlegen: `/etc/systemd/system/noria-backend.service`

```ini
[Unit]
Description=Noria Backend (FastAPI)
After=network.target
Wants=network.target

[Service]
Type=notify
User=noria
Group=noria
WorkingDirectory=/opt/noria/app
Environment="PATH=/opt/noria/venv/bin"
EnvironmentFile=/opt/noria/.env
ExecStart=/opt/noria/venv/bin/uvicorn main:app \
    --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
TimeoutStopSec=30
KillSignal=SIGTERM
KillMode=mixed
WatchdogSec=30
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
SyslogIdentifier=noria-backend

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/opt/noria/app/data /opt/noria/app/logs

DeviceAllow=/dev/gpiomem rw
SupplementaryGroups=gpio

[Install]
WantedBy=multi-user.target
```

**Wichtig:**
- `Type=notify` und `WatchdogSec=30`: Das Backend sendet `WATCHDOG=1` alle ~10 Sekunden via `sd_notify`. Wenn der Watchdog innerhalb von 30 Sekunden keinen Heartbeat empfängt, wird der Service neu gestartet. Erfordert `systemd-python` im Virtualenv.
- Falls `systemd-python` nicht installierbar ist: `Type=notify` durch `Type=simple` ersetzen.

### 4.2 Frontend-Service

Datei anlegen: `/etc/systemd/system/noria-frontend.service`

```ini
[Unit]
Description=Noria Frontend (Shiny)
After=network.target noria-backend.service
Wants=noria-backend.service

[Service]
Type=simple
User=noria
Group=noria
WorkingDirectory=/opt/noria/app
Environment="PATH=/opt/noria/venv/bin"
ExecStart=/opt/noria/venv/bin/shiny run app.py \
    --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=noria-frontend

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/opt/noria/app/data /opt/noria/app/logs

[Install]
WantedBy=multi-user.target
```

### 4.3 Services aktivieren und starten

```bash
sudo systemctl daemon-reload

sudo systemctl enable noria-backend
sudo systemctl enable noria-frontend

sudo systemctl start noria-backend
sleep 5
sudo systemctl start noria-frontend
```

### 4.4 Service-Status prüfen

```bash
sudo systemctl status noria-backend
sudo systemctl status noria-frontend

# Live-Log verfolgen
sudo journalctl -u noria-backend -f
sudo journalctl -u noria-frontend -f
```

---

## 5. API-Key abrufen und im Frontend hinterlegen

Nach dem ersten Start des Backends wurde der API-Key automatisch generiert:

```bash
sudo cat /opt/noria/app/data/api_key.txt
```

Das Frontend liest den Key automatisch aus `data/api_key.txt`. Bei korrektem Setup (Backend und Frontend auf demselben Pi, gleicher `WorkingDirectory`) ist keine manuelle Konfiguration erforderlich.

Wenn der Key ungültig oder nicht lesbar ist, zeigt das Frontend einen roten Auth-Modal. In diesem Fall:
1. Backend-Log prüfen: `sudo journalctl -u noria-backend --since "5 minutes ago"`
2. Dateiberechtigung prüfen: `ls -la /opt/noria/app/data/api_key.txt` (muss `600` sein)
3. Key manuell in das Modal eingeben oder `api_key.txt` vom Pi kopieren

---

## 6. Berechtigungen prüfen

```bash
# api_key.txt muss 600 sein (nur Owner lesen/schreiben)
ls -la /opt/noria/app/data/api_key.txt
# Erwartete Ausgabe: -rw------- 1 noria noria ...

# Das Backend setzt 600 automatisch beim Erstellen und Laden.
# Falls falsch:
sudo chmod 600 /opt/noria/app/data/api_key.txt
sudo chown noria:noria /opt/noria/app/data/api_key.txt
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

Konfiguration `/etc/nginx/sites-available/noria`:

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
sudo ln -s /etc/nginx/sites-available/noria /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 9. TLS/HTTPS-Setup (optional, empfohlen für Fernzugriff)

### Option A: Selbstsigniertes Zertifikat (lokales Netzwerk)

```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/noria.key \
  -out /etc/ssl/certs/noria.crt \
  -subj "/CN=<PI-IP-ODER-HOSTNAME>"
```

nginx-Konfiguration ergänzen:
```nginx
server {
    listen 443 ssl;
    ssl_certificate /etc/ssl/certs/noria.crt;
    ssl_certificate_key /etc/ssl/private/noria.key;
    # ... rest der Konfiguration wie oben
}
```

### Option B: Let's Encrypt (benötigt öffentlichen DNS und Port 80/443)

<!-- TODO: Domainname eintragen wenn Let's Encrypt verwendet werden soll -->

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <DOMAIN>
```

**Wichtig nach HTTPS-Umstellung:** `ALLOWED_ORIGINS` in `/opt/noria/.env` und `base_url` in `frontend_config.json` entsprechend auf `https://` aktualisieren, dann Services neu starten.

---

## 10. Updates einspielen

```bash
cd ~/noria

# Neuesten Code holen
git pull

# Update-Script ausführen (stoppt Services, deployt Code, startet neu)
sudo bash scripts/update.sh
```

Das Update-Script stoppt die Services graceful, kopiert den neuen Code, aktualisiert pip-Pakete und startet neu. Konfigurationsdateien und Nutzerdaten werden dabei nicht verändert.

---

## 11. Neustart-Verhalten

Nach einem Systemabsturz oder Stromausfall:

1. Backend startet neu (via systemd `Restart=on-failure`)
2. Beim Startup: `close_all()` via IO-Worker → alle Ventile werden geschlossen (Fail-Safe)
3. `active_runs` wird geleert, `paused=False` (Laufzeit-Zustand wird NICHT wiederhergestellt)
4. Queue und Zeitpläne werden aus `data/queue.json` bzw. `data/schedules.json` wiederhergestellt
5. Die Queue-State wird auf `"bereit"` zurückgesetzt (nicht auf den gespeicherten Wert)
6. Systemd-Watchdog läuft neu an

**Sicherheitsgarantie:** Nach einem Neustart laufen keine Ventile, bis sie explizit gestartet werden.
