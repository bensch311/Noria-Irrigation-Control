#!/usr/bin/env bash
# =============================================================================
# install.sh – Noria Installations-Script
# =============================================================================
#
# Verwendung (aus dem Repo-Root):
#   sudo bash scripts/install.sh
#
# Voraussetzungen:
#   - Raspberry Pi OS Lite 64-bit (Debian Bookworm oder neuer)
#   - Python >= 3.11  (sudo apt install python3.11 falls älter)
#   - Internetverbindung für pip-Pakete
#   - Script muss mit sudo / als root ausgeführt werden
#
# Was dieses Script tut:
#   1. Systemprüfung  (OS, Python, git)
#   2. IP-Adresse + Ventil-Konfiguration abfragen
#   3. Systembenutzer 'noria' anlegen
#   4. Code nach /opt/noria/app deployen
#   5. Python-Virtualenv erstellen + Pakete installieren
#   6. Konfigurationsdateien generieren (device_config, frontend_config, .env)
#   7. systemd-Services generieren, aktivieren und starten
#   8. Abschlusszusammenfassung mit Zugriffs-URLs ausgeben
#
# Bei einem Neustart des Pi starten die Services automatisch.
# =============================================================================

set -uo pipefail

# ── Farben & Ausgabe-Helfer ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'  # No Color

info()    { echo -e "${BLUE}[INFO]${NC}    $*"; }
success() { echo -e "${GREEN}[OK]${NC}      $*"; }
warn()    { echo -e "${YELLOW}[WARNUNG]${NC} $*"; }
error()   { echo -e "${RED}[FEHLER]${NC}  $*" >&2; }
die()     { error "$*"; echo -e "${RED}Installation abgebrochen.${NC}" >&2; exit 1; }
section() {
    echo
    echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}${BLUE}  $*${NC}"
    echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# ── Installations-Konstanten ──────────────────────────────────────────────────
INSTALL_DIR="/opt/noria"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
DATA_DIR="$APP_DIR/data"
LOGS_DIR="$APP_DIR/logs"
ENV_FILE="$INSTALL_DIR/.env"
SYSTEMD_DIR="/etc/systemd/system"
APP_USER="noria"
BACKEND_PORT=8000
FRONTEND_PORT=8080

# GPIO-Standardpins (BCM-Nummerierung, handelsübliches 8-Kanal-Relaisboard)
DEFAULT_PINS=(17 18 27 22 23 24 25 5 6 13 19 26 16 20 21 4)

# ── Quellverzeichnis ermitteln ────────────────────────────────────────────────
# Das Script liegt in scripts/ innerhalb des Repos.
# Python-Quellen sind entweder im Repo-Root (flache Struktur)
# oder in einem app/-Unterordner (modulare Struktur).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [[ -f "$REPO_DIR/main.py" ]]; then
    SOURCE_DIR="$REPO_DIR"
elif [[ -f "$REPO_DIR/app/main.py" ]]; then
    SOURCE_DIR="$REPO_DIR/app"
else
    die "main.py nicht gefunden. Bitte das Script aus dem Repo-Root ausführen:\n  sudo bash scripts/install.sh"
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║          Noria – Installation            ║${NC}"
echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo
echo "  Dieses Script installiert Noria"
echo "  auf diesem Raspberry Pi und richtet automatische"
echo "  Dienste ein, die bei jedem Systemstart laufen."
echo

# ── 1. SYSTEMPRÜFUNG ─────────────────────────────────────────────────────────
section "1 / 8  Systemprüfung"

# Root-Rechte prüfen
if [[ $EUID -ne 0 ]]; then
    die "Dieses Script muss als root ausgeführt werden.\n  Befehl: sudo bash scripts/install.sh"
fi
success "Root-Rechte vorhanden"

# Betriebssystem prüfen
if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    source /etc/os-release
    if [[ "${ID:-}" == "debian" || "${ID_LIKE:-}" == *"debian"* ]]; then
        success "Betriebssystem: ${PRETTY_NAME:-$ID}"
    else
        warn "Unbekanntes Betriebssystem (${PRETTY_NAME:-unbekannt})."
        warn "Dieses Script ist für Raspberry Pi OS (Debian) ausgelegt."
        read -rp "  Trotzdem fortfahren? [j/N]: " CONT
        [[ "${CONT,,}" == "j" || "${CONT,,}" == "ja" ]] || { echo "Abgebrochen."; exit 0; }
    fi
fi

# Python >= 3.11 prüfen
PYTHON_BIN=""
for py in python3.13 python3.12 python3.11 python3; do
    if command -v "$py" &>/dev/null; then
        PY_VER=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        PY_MAJOR="${PY_VER%%.*}"
        PY_MINOR="${PY_VER##*.}"
        if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
            PYTHON_BIN="$py"
            success "Python $PY_VER gefunden ($py)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    die "Python >= 3.11 nicht gefunden.\n  Bitte installieren: sudo apt install python3.11"
fi

# git prüfen
if ! command -v git &>/dev/null; then
    die "git nicht gefunden.\n  Bitte installieren: sudo apt install git"
fi
success "git verfügbar"

# rsync prüfen (wird für Code-Deployment benötigt)
if ! command -v rsync &>/dev/null; then
    info "rsync nicht gefunden – wird installiert..."
    apt-get install -y rsync --quiet || die "rsync-Installation fehlgeschlagen"
fi
success "rsync verfügbar"

# Bereits installiert?
REINSTALL=false
if [[ -d "$APP_DIR" ]]; then
    warn "Eine Installation wurde gefunden unter: $APP_DIR"
    read -rp "  Neuinstallation / Reparatur durchführen? Bestehende Daten bleiben erhalten. [j/N]: " REINST
    if [[ "${REINST,,}" == "j" || "${REINST,,}" == "ja" ]]; then
        REINSTALL=true
        info "Neuinstallation wird durchgeführt (Daten bleiben erhalten)"
    else
        echo "Abgebrochen."
        exit 0
    fi
fi

# ── 2. KONFIGURATION ABFRAGEN ─────────────────────────────────────────────────
section "2 / 8  Konfiguration"

echo
echo "  Bitte einige Fragen zur Hardware-Konfiguration beantworten."
echo "  Einfach Enter drücken um den Standardwert [in Klammern] zu übernehmen."
echo

# IP-Adresse des Pi ermitteln
DEFAULT_IP=$(ip route get 1.1.1.1 2>/dev/null \
    | grep -oP '(?<=src )[0-9.]+' | head -1 \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "127.0.0.1")

echo "─── Netzwerk ───────────────────────────────────────────"
echo "  Die IP-Adresse wird benötigt, damit andere Geräte"
echo "  (z.B. Touchscreen-Panel, Büro-PC) die Oberfläche"
echo "  im Netzwerk aufrufen können."
echo
read -rp "  IP-Adresse dieses Raspberry Pi [$DEFAULT_IP]: " PI_IP
PI_IP="${PI_IP:-$DEFAULT_IP}"
if ! [[ "$PI_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    die "Ungültige IP-Adresse: '$PI_IP'"
fi
success "Pi-IP: $PI_IP"

echo
echo "─── Ventile ────────────────────────────────────────────"
read -rp "  Anzahl der Ventile (1–16) [6]: " NUM_VALVES
NUM_VALVES="${NUM_VALVES:-6}"
if ! [[ "$NUM_VALVES" =~ ^[0-9]+$ ]] || [[ "$NUM_VALVES" -lt 1 || "$NUM_VALVES" -gt 16 ]]; then
    die "Ungültige Ventilanzahl: '$NUM_VALVES' (erlaubt: 1–16)"
fi
success "Ventile: $NUM_VALVES"

echo
echo "─── GPIO-Pins ──────────────────────────────────────────"
echo "  BCM-Pin-Nummer für jedes Ventil eingeben."
echo "  Enter = Standardwert übernehmen."
echo
declare -a GPIO_PINS
declare -A USED_PINS
for (( i=1; i<=NUM_VALVES; i++ )); do
    DEFAULT_PIN="${DEFAULT_PINS[$((i-1))]}"
    while true; do
        read -rp "  Ventil $i  → GPIO-Pin (BCM) [$DEFAULT_PIN]: " PIN
        PIN="${PIN:-$DEFAULT_PIN}"
        if ! [[ "$PIN" =~ ^[0-9]+$ ]] || [[ "$PIN" -lt 2 || "$PIN" -gt 27 ]]; then
            warn "    Ungültiger Pin: $PIN (erlaubt: BCM 2–27). Erneut eingeben."
            continue
        fi
        if [[ -n "${USED_PINS[$PIN]+x}" ]]; then
            warn "    Pin $PIN wird bereits für Ventil ${USED_PINS[$PIN]} verwendet. Anderen Pin wählen."
            continue
        fi
        GPIO_PINS+=("$PIN")
        USED_PINS[$PIN]=$i
        break
    done
done
success "GPIO-Pins: ${GPIO_PINS[*]}"

echo
echo "─── Relais-Konfiguration ───────────────────────────────"
echo "  Handelsübliche 8-Kanal-Relaisboards werden mit einem"
echo "  LOW-Signal aktiviert (Aktiv-Low). Das ist die sichere"
echo "  Standardeinstellung – im Zweifel J wählen."
echo
read -rp "  Relais Aktiv-Low? (Standard für die meisten Boards) [J/n]: " RELAY_AL
case "${RELAY_AL,,}" in
    n|nein) RELAY_ACTIVE_LOW="false"; info "Relais: Aktiv-High" ;;
    *)      RELAY_ACTIVE_LOW="true";  info "Relais: Aktiv-Low (Standard)" ;;
esac

echo
echo "─── Betriebsgrenzen ────────────────────────────────────"
read -rp "  Max. gleichzeitig offene Ventile [2]: " MAX_CONCURRENT
MAX_CONCURRENT="${MAX_CONCURRENT:-2}"
if ! [[ "$MAX_CONCURRENT" =~ ^[0-9]+$ ]] || [[ "$MAX_CONCURRENT" -lt 1 ]]; then
    die "Ungültige Eingabe: '$MAX_CONCURRENT'"
fi

read -rp "  Max. Ventil-Laufzeit in Sekunden (3600 = 1 Stunde) [3600]: " MAX_RUNTIME
MAX_RUNTIME="${MAX_RUNTIME:-3600}"
if ! [[ "$MAX_RUNTIME" =~ ^[0-9]+$ ]] || [[ "$MAX_RUNTIME" -lt 1 ]]; then
    die "Ungültige Eingabe: '$MAX_RUNTIME'"
fi
success "Betriebsgrenzen: max. $MAX_CONCURRENT gleichzeitig, max. ${MAX_RUNTIME}s Laufzeit"

# ── 3. ZUSAMMENFASSUNG & BESTÄTIGUNG ──────────────────────────────────────────
section "3 / 8  Zusammenfassung"

echo
echo -e "  ${BOLD}Installationsverzeichnis :${NC} $INSTALL_DIR"
echo -e "  ${BOLD}Python                   :${NC} $PYTHON_BIN ($PY_VER)"
echo -e "  ${BOLD}Quellcode                :${NC} $SOURCE_DIR"
echo
echo -e "  ${BOLD}Pi-IP-Adresse            :${NC} $PI_IP"
echo -e "  ${BOLD}Frontend (Oberfläche)    :${NC} http://$PI_IP:$FRONTEND_PORT"
echo -e "  ${BOLD}Backend (API)            :${NC} http://$PI_IP:$BACKEND_PORT"
echo
echo -e "  ${BOLD}Anzahl Ventile           :${NC} $NUM_VALVES"
echo -e "  ${BOLD}GPIO-Pins (Ventil 1→N)  :${NC} ${GPIO_PINS[*]}"
echo -e "  ${BOLD}Relais Aktiv-Low         :${NC} $RELAY_ACTIVE_LOW"
echo -e "  ${BOLD}Max. gleichz. Ventile    :${NC} $MAX_CONCURRENT"
echo -e "  ${BOLD}Max. Laufzeit            :${NC} ${MAX_RUNTIME}s"
echo
read -rp "  Installation jetzt starten? [J/n]: " CONFIRM
case "${CONFIRM,,}" in
    n|nein) echo "Abgebrochen."; exit 0 ;;
esac

echo

# ── 4. SYSTEMBENUTZER & SYSTEMABHÄNGIGKEITEN ──────────────────────────────────
section "4 / 8  Systembenutzer & Systemabhängigkeiten"

info "Installiere benötigte Systempakete..."
apt-get install -y \
    python3-venv \
    build-essential \
    libsystemd-dev \
    --quiet \
    2>&1 | grep -v "^$" || true
success "Systempakete installiert"

# Systembenutzer anlegen
if id "$APP_USER" &>/dev/null; then
    success "Systembenutzer '$APP_USER' existiert bereits"
else
    adduser --system --group --no-create-home "$APP_USER"
    success "Systembenutzer '$APP_USER' angelegt"
fi

# GPIO-Gruppe
if getent group gpio &>/dev/null; then
    usermod -aG gpio "$APP_USER"
    success "Benutzer '$APP_USER' zur Gruppe 'gpio' hinzugefügt"
else
    warn "Gruppe 'gpio' nicht gefunden – GPIO-Zugriff evtl. nicht möglich"
fi

# ── 5. VERZEICHNISSE & CODE ───────────────────────────────────────────────────
section "5 / 8  Code deployen"

info "Erstelle Verzeichnisse..."
mkdir -p "$APP_DIR" "$DATA_DIR" "$LOGS_DIR"
success "Verzeichnisse: $APP_DIR, $DATA_DIR, $LOGS_DIR"

info "Kopiere Anwendungscode nach $APP_DIR ..."
rsync -a \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='data/' \
    --exclude='logs/' \
    --exclude='tests/' \
    --exclude='test_*.py' \
    --exclude='conftest.py' \
    --exclude='pytest.ini' \
    --exclude='.pytest_cache/' \
    --exclude='.coverage' \
    --exclude='htmlcov/' \
    --exclude='scripts/' \
    --exclude='docs/' \
    "$SOURCE_DIR/" "$APP_DIR/"
success "Code kopiert"

# ── 6. PYTHON-UMGEBUNG ────────────────────────────────────────────────────────
section "6 / 8  Python-Umgebung & Pakete"

if [[ "$REINSTALL" == "true" && -d "$VENV_DIR" ]]; then
    info "Bestehende Virtualenv wird aktualisiert..."
else
    info "Erstelle Python-Virtualenv..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    success "Virtualenv erstellt: $VENV_DIR"
fi

info "Aktualisiere pip..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet

info "Installiere Anwendungspakete (kann 2–5 Minuten dauern)..."
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
success "Anwendungspakete installiert"

# systemd-python für READY=1 / WATCHDOG=1 Signale
info "Installiere systemd-python (Watchdog-Integration)..."
SYSTEMD_TYPE="simple"
if "$VENV_DIR/bin/pip" install systemd-python --quiet 2>/dev/null; then
    SYSTEMD_TYPE="notify"
    success "systemd-python installiert – Watchdog aktiv (Type=notify)"
else
    warn "systemd-python konnte nicht installiert werden."
    warn "Service läuft weiter, aber ohne systemd-Watchdog (Type=simple)."
    warn "Für Watchdog-Support: sudo apt install libsystemd-dev && sudo bash scripts/install.sh"
fi

# ── 7. KONFIGURATIONSDATEIEN ERSTELLEN ───────────────────────────────────────
section "7 / 8  Konfigurationsdateien"

# device_config.json generieren (wird immer neu geschrieben – Hardware-Konfiguration)
info "Erstelle device_config.json..."
GPIO_JSON=""
for (( i=1; i<=NUM_VALVES; i++ )); do
    [[ $i -gt 1 ]] && GPIO_JSON+=","$'\n'"      "
    GPIO_JSON+="\"$i\": ${GPIO_PINS[$((i-1))]}"
done

cat > "$DATA_DIR/device_config.json" << EOF
{
  "version": 1,
  "device": {
    "MAX_VALVES": $NUM_VALVES,
    "IRRIGATION_VALVE_DRIVER": "rpi",
    "IRRIGATION_RELAY_ACTIVE_LOW": $RELAY_ACTIVE_LOW,
    "IRRIGATION_GPIO_PINS": {
      $GPIO_JSON
    }
  },
  "hard_limits": {
    "MAX_RUNTIME_S": $MAX_RUNTIME,
    "MAX_CONCURRENT_VALVES": $MAX_CONCURRENT
  }
}
EOF
success "device_config.json erstellt"

# frontend_config.json generieren (immer neu – enthält IP-Adresse)
info "Erstelle frontend_config.json..."
cat > "$DATA_DIR/frontend_config.json" << EOF
{
  "base_url": "http://127.0.0.1:$BACKEND_PORT",
  "poll_status_s": 1,
  "poll_slow_s": 5,
  "backend_fail_threshold": 3,
  "health_timeout_s": 0.8,
  "anzahl_ventile_fallback": $NUM_VALVES
}
EOF
success "frontend_config.json erstellt"

# user_settings.json – nur bei Erstinstallation anlegen, Nutzerdaten erhalten
if [[ ! -f "$DATA_DIR/user_settings.json" ]]; then
    if [[ -f "$SOURCE_DIR/user_settings.json" ]]; then
        cp "$SOURCE_DIR/user_settings.json" "$DATA_DIR/user_settings.json"
    else
        echo '{}' > "$DATA_DIR/user_settings.json"
    fi
    success "user_settings.json angelegt"
else
    info "user_settings.json bereits vorhanden – bestehende Einstellungen behalten"
fi

# .env-Datei für systemd-Service (enthält Umgebungsvariablen)
info "Erstelle .env-Datei..."
cat > "$ENV_FILE" << EOF
# Noria – Umgebungsvariablen
# Automatisch generiert von install.sh – bei Bedarf manuell anpassen.

# Treiber: rpi = echter Raspberry Pi GPIO, sim = Simulation (kein GPIO)
IRRIGATION_VALVE_DRIVER=rpi

# Relais-Polarität: true = LOW aktiviert Relais (Standard für die meisten Boards)
IRRIGATION_RELAY_ACTIVE_LOW=$RELAY_ACTIVE_LOW

# CORS: Von welchen Adressen darf ein Browser auf die API zugreifen?
# Muss die tatsächliche IP/URL enthalten, von der das Frontend aufgerufen wird.
ALLOWED_ORIGINS=http://${PI_IP}:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT},http://127.0.0.1:${FRONTEND_PORT}
EOF
chmod 640 "$ENV_FILE"
success ".env erstellt"

# Berechtigungen setzen
info "Setze Dateiberechtigungen..."
chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 750 "$APP_DIR"
chmod 700 "$DATA_DIR"
chmod 700 "$LOGS_DIR"
chmod 640 "$ENV_FILE"
success "Berechtigungen gesetzt (Daten: 700, App: 750)"

# ── 8. SYSTEMD-SERVICES ───────────────────────────────────────────────────────
section "8 / 8  Dienste einrichten"

# ──── Backend-Service ────────────────────────────────────────────────────────
info "Erstelle noria-backend.service..."
cat > "$SYSTEMD_DIR/noria-backend.service" << EOF
# /etc/systemd/system/noria-backend.service
# Generiert von scripts/install.sh

[Unit]
Description=Noria Backend (FastAPI/uvicorn)
After=network.target
Wants=network.target

[Service]
Type=$SYSTEMD_TYPE
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PATH=$VENV_DIR/bin"
EnvironmentFile=$ENV_FILE

# Einzelner Worker-Prozess – mehrere Worker würden den geteilten In-Memory-State
# inkonsistent machen. --no-access-log reduziert Log-Spam.
ExecStart=$VENV_DIR/bin/uvicorn main:app \\
    --host 0.0.0.0 \\
    --port $BACKEND_PORT \\
    --workers 1 \\
    --no-access-log

# Graceful Shutdown: SIGTERM → lifespan-Cleanup → SIGKILL
# TimeoutStopSec muss > close_all-Timeout (10s) + Flush-Zeit sein
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30

# Neustart bei Absturz; max. 5 Neustarts in 60s (verhindert Crash-Schleife)
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

# systemd-Watchdog: Backend sendet WATCHDOG=1 alle 10s.
# Wenn kein Heartbeat innerhalb 30s → Service-Neustart.
WatchdogSec=30

LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
SyslogIdentifier=noria-backend

# Sicherheits-Härtung
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOGS_DIR

# GPIO-Zugriff auf /dev/gpiomem
DeviceAllow=/dev/gpiomem rw
SupplementaryGroups=gpio

[Install]
WantedBy=multi-user.target
EOF
success "noria-backend.service erstellt"

# ──── Frontend-Service ────────────────────────────────────────────────────────
info "Erstelle noria-frontend.service..."
cat > "$SYSTEMD_DIR/noria-frontend.service" << EOF
# /etc/systemd/system/noria-frontend.service
# Generiert von scripts/install.sh

[Unit]
Description=Noria Frontend (Python Shiny)
After=network.target noria-backend.service
Wants=noria-backend.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PATH=$VENV_DIR/bin"

ExecStart=$VENV_DIR/bin/shiny run app.py \\
    --host 0.0.0.0 \\
    --port $FRONTEND_PORT

KillSignal=SIGTERM
TimeoutStopSec=15
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=noria-frontend

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOGS_DIR

[Install]
WantedBy=multi-user.target
EOF
success "noria-frontend.service erstellt"

# ──── Hardware-Watchdog aktivieren (nur Raspberry Pi) ────────────────────────
# Erkennung: Raspberry Pi OS hat immer eine /boot/.../config.txt.
# Auf anderen Systemen (Dev-VM, x86) fehlt sie → stillschweigend überspringen.
#
# Zwei Ebenen:
#   1. dtparam=watchdog=on  → aktiviert /dev/watchdog im BCM-Chip (nach Neustart wirksam)
#   2. RuntimeWatchdogSec   → systemd füttert /dev/watchdog; wenn Kernel/systemd hängt → HW-Reset
#
# RuntimeWatchdogSec ohne /dev/watchdog (z.B. vor erstem Neustart oder Nicht-Pi):
#   systemd loggt intern eine Warnung und läuft weiter – kein Fehler, kein Absturz.
HW_WATCHDOG_ENABLED=false
info "Prüfe Hardware-Watchdog (Raspberry Pi)..."

# Boot-Config-Pfad: Bookworm → /boot/firmware/config.txt, Bullseye → /boot/config.txt
BOOT_CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$candidate" ]]; then
        BOOT_CONFIG="$candidate"
        break
    fi
done

if [[ -n "$BOOT_CONFIG" ]]; then
    # dtparam=watchdog=on idempotent eintragen
    if grep -q "dtparam=watchdog=on" "$BOOT_CONFIG" 2>/dev/null; then
        info "dtparam=watchdog=on bereits in $BOOT_CONFIG – keine Änderung nötig"
    else
        echo "dtparam=watchdog=on" >> "$BOOT_CONFIG"
        info "dtparam=watchdog=on zu $BOOT_CONFIG hinzugefügt"
    fi

    # systemd-Dropin: RuntimeWatchdogSec
    # Wird als eigene Drop-in-Datei angelegt um /etc/systemd/system.conf nicht zu ändern.
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/noria-watchdog.conf << 'WATCHDOG_EOF'
# Hardware-Watchdog für den Noria
# Generiert von scripts/install.sh – nicht manuell editieren.
#
# RuntimeWatchdogSec: systemd füttert /dev/watchdog alle 15s.
# Bleibt das Füttern aus (Kernel-Freeze, systemd-Deadlock) → BCM-Hardware-Reset.
# Auf Nicht-Pi-Systemen ohne /dev/watchdog: systemd loggt eine Warnung, kein Fehler.
#
# RebootWatchdogSec: Watchdog-Timeout beim systemd-Shutdown/Reboot.
# Verhindert dass ein hängender Shutdown den Pi dauerhaft blockiert.
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
WATCHDOG_EOF

    HW_WATCHDOG_ENABLED=true
    success "Hardware-Watchdog konfiguriert (wirksam nach erstem Neustart des Pi)"
else
    info "Kein Raspberry Pi Boot-Config gefunden – Hardware-Watchdog übersprungen"
    info "Aktiv: systemd-Prozess-Watchdog (WatchdogSec=30 im Service)"
fi

# ──── Services aktivieren und starten ────────────────────────────────────────
info "Services laden und aktivieren..."
systemctl daemon-reload
systemctl enable noria-backend noria-frontend
success "Services aktiviert (starten automatisch bei jedem Systemstart)"

info "Starte Backend..."
systemctl start noria-backend

# Kurz warten damit das Backend hochfahren kann bevor das Frontend startet
info "Warte auf Backend-Start (5 Sekunden)..."
sleep 5

info "Starte Frontend..."
systemctl start noria-frontend

# Status prüfen
sleep 2
BACKEND_OK=false
FRONTEND_OK=false

if systemctl is-active --quiet noria-backend; then
    BACKEND_OK=true
    success "noria-backend läuft"
else
    warn "noria-backend konnte nicht gestartet werden"
    warn "  Logs prüfen: sudo journalctl -u noria-backend -n 50"
fi

if systemctl is-active --quiet noria-frontend; then
    FRONTEND_OK=true
    success "noria-frontend läuft"
else
    warn "noria-frontend konnte nicht gestartet werden"
    warn "  Logs prüfen: sudo journalctl -u noria-frontend -n 50"
fi

# ── ABSCHLUSS ─────────────────────────────────────────────────────────────────
echo
if [[ "$BACKEND_OK" == "true" && "$FRONTEND_OK" == "true" ]]; then
    echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║     Installation erfolgreich abgeschlossen!        ║${NC}"
    echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${YELLOW}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${YELLOW}║  Installation abgeschlossen – Services prüfen!    ║${NC}"
    echo -e "${BOLD}${YELLOW}╚════════════════════════════════════════════════════╝${NC}"
fi
echo
echo -e "  ${BOLD}Oberfläche (Browser / Touchscreen):${NC}"
echo -e "  ${BOLD}${GREEN}→ http://${PI_IP}:${FRONTEND_PORT}${NC}"
echo
echo -e "  ${BOLD}Backend-API:${NC}"
echo -e "  → http://${PI_IP}:${BACKEND_PORT}/health"
echo
echo -e "  ${BOLD}Wichtige Befehle:${NC}"
echo "  Status:    sudo systemctl status noria-backend noria-frontend"
echo "  Logs:      sudo journalctl -u noria-backend -f"
echo "             sudo journalctl -u noria-frontend -f"
echo "  Neustart:  sudo systemctl restart noria-backend noria-frontend"
echo "  Stoppen:   sudo systemctl stop noria-frontend noria-backend"
echo
echo -e "  ${BOLD}Konfiguration:${NC}  $DATA_DIR/"
echo -e "  ${BOLD}Logs:${NC}           $LOGS_DIR/"
echo -e "  ${BOLD}Umgebung:${NC}       $ENV_FILE"
echo
echo -e "  ${BOLD}Watchdog:${NC}"
if [[ "$HW_WATCHDOG_ENABLED" == "true" ]]; then
    echo "  + Hardware-Watchdog (BCM) konfiguriert -- wirksam nach Neustart"
    echo "  + systemd-Prozess-Watchdog aktiv (sofort)"
    echo
    warn "  Fuer vollstaendigen Hardware-Watchdog-Schutz jetzt neu starten:"
    echo "    sudo reboot"
else
    echo "  + systemd-Prozess-Watchdog aktiv"
    echo "  - Hardware-Watchdog: nicht verfuegbar (kein Raspberry Pi erkannt)"
fi
echo
echo "  Nach einem Stromausfall starten die Dienste automatisch neu."
echo "  Alle Ventile werden beim Start sicher geschlossen."
echo
