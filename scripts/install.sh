#!/usr/bin/env bash
# =============================================================================
# install.sh – Noria Installations-Script
# =============================================================================
#
# Verwendung A – direkt aus dem geklonten Repository:
#   sudo bash scripts/install.sh
#
# Verwendung B – via Bootstrap-Script (empfohlen, Ein-Zeilen-Install):
#   curl -fsSL https://raw.githubusercontent.com/bensch311/Noria-Irrigation-Control/refs/heads/main/scripts/bootstrap.sh | sudo bash
#   Das Bootstrap-Script klont das Repository und ruft dieses Script auf.
#
# Voraussetzungen:
#   - Raspberry Pi OS with Desktop 64-bit (Debian Trixie/Bookworm) ← Standard
#     ODER Raspberry Pi OS Lite 64-bit (wenn Kiosk-Modus = Nein)
#   - Python >= 3.11  (in aktuellem Raspberry Pi OS enthalten)
#   - Internetverbindung
#   - Script muss mit sudo / als root ausgeführt werden
#
# Was dieses Script tut:
#   1. Systemprüfung + Systempakete aktualisieren
#   2. IP-Adresse + Ventil- + Sensor- + Kiosk-Konfiguration abfragen
#   3. Zusammenfassung & Bestätigung
#   4. Systembenutzer anlegen + Systempakete installieren
#   5. Code nach /opt/noria/app deployen
#   6. Python-Virtualenv erstellen + Pakete installieren
#   7. Konfigurationsdateien generieren (device_config, frontend_config, .env)
#   8. systemd-Services generieren, aktivieren und starten
#   9. Kiosk-Modus einrichten (optional)
#
# Bei einem Neustart des Pi starten die Services automatisch.
# Im Kiosk-Modus öffnet sich Chromium direkt nach dem Boot im Vollbild.
# =============================================================================

set -uo pipefail

# ── Farben & Ausgabe-Helfer ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;36m'
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
KIOSK_USER="kiosk"
KIOSK_HOME="/home/$KIOSK_USER"
BACKEND_PORT=8000
FRONTEND_PORT=8080

# GPIO-Standardpins (BCM-Nummerierung, handelsübliches 8-Kanal-Relaisboard)
DEFAULT_PINS=(17 18 27 22 23 24 25 5 6 13 19 26 16 20 21 4)

# Standard-Sensor-Eingangspins (BCM) – ausserhalb der Ventil-Defaults.
# Hinweis: 2/3 = I2C (HAT), 7–11 = SPI. Diese Werte sind nur Vorschläge –
# die Eingabe-Validierung verhindert Doppelbelegungen zuverlässig.
DEFAULT_SENSOR_PINS=(14 15 12 11 9 8 7 3)

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
echo -e "${BOLD}${GREEN}║          Noria – Installation                      ║${NC}"
echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo
echo "  Dieses Script installiert Noria"
echo "  auf diesem Raspberry Pi und richtet automatische"
echo "  Dienste ein, die bei jedem Systemstart laufen."
echo

# ── 1. SYSTEMPRÜFUNG ─────────────────────────────────────────────────────────
section "1 / 9  Systemprüfung & System-Update"

# Root-Rechte prüfen
if [[ $EUID -ne 0 ]]; then
    die "Dieses Script muss als root ausgeführt werden.\n  Befehl: sudo bash scripts/install.sh"
fi
success "Root-Rechte vorhanden"

# Systempakete aktualisieren
# Wird hier ausgeführt damit install.sh auch ohne Bootstrap-Script vollständig
# und selbstständig lauffähig ist. Beim Aufruf via bootstrap.sh ist dies der
# einzige apt-Update-Lauf (bootstrap.sh ruft direkt dieses Script auf).
info "Aktualisiere Paketliste und Systempakete (kann einige Minuten dauern)..."
apt-get update -q 2>&1 | tail -1 || true
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -q 2>&1 | grep -E "^(upgraded|Inst |Err )" | head -20 || true
success "Systempakete aktualisiert"

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

# git prüfen (wird für den Bootstrap-Pfad ggf. bereits installiert, zur
# Sicherheit aber auch hier nochmals geprüft, falls install.sh direkt aufgerufen wird)
if ! command -v git &>/dev/null; then
    info "git nicht gefunden – wird installiert..."
    apt-get install -y git --quiet || die "git-Installation fehlgeschlagen"
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
section "2 / 9  Konfiguration"

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

echo
echo "─── Sensoren ───────────────────────────────────────────"
echo "  Tensiometer-Sensoren (z.B. MMM-Tech TXS Schalttensiometer)"
echo "  melden Bodentrockenheit über einen Schalt-Trockenkontakt."
echo "  Jeder Sensor belegt einen GPIO-Eingangspin (BCM)."
echo
read -rp "  Sensoren an diesem Gerät installiert? [j/N]: " SENSOR_ANSWER
case "${SENSOR_ANSWER,,}" in
    j|ja) SENSORS_INSTALLED=true;  info "Sensoren: Ja" ;;
    *)    SENSORS_INSTALLED=false; info "Sensoren: Nein – Sensor-Tab wird ausgeblendet" ;;
esac

if [[ "$SENSORS_INSTALLED" == "true" ]]; then
    echo
    read -rp "  Anzahl der Sensoren (1–8) [1]: " NUM_SENSORS
    NUM_SENSORS="${NUM_SENSORS:-1}"
    if ! [[ "$NUM_SENSORS" =~ ^[0-9]+$ ]] || [[ "$NUM_SENSORS" -lt 1 || "$NUM_SENSORS" -gt 8 ]]; then
        die "Ungültige Sensoranzahl: '$NUM_SENSORS' (erlaubt: 1–8)"
    fi
    success "Sensoren: $NUM_SENSORS"

    echo
    echo "  GPIO-Eingangspin für jeden Sensor (BCM-Nummerierung)."
    echo "  Der Sensor verbindet den Pin bei Trockenheit mit GND."
    echo "  Pins die bereits für Ventile belegt sind werden abgewiesen."
    echo
    declare -a SENSOR_PINS
    for (( i=1; i<=NUM_SENSORS; i++ )); do
        DEFAULT_SENSOR_PIN="${DEFAULT_SENSOR_PINS[$((i-1))]}"
        while true; do
            read -rp "  Sensor $i → GPIO-Pin (BCM) [$DEFAULT_SENSOR_PIN]: " SPIN
            SPIN="${SPIN:-$DEFAULT_SENSOR_PIN}"
            if ! [[ "$SPIN" =~ ^[0-9]+$ ]] || [[ "$SPIN" -lt 2 || "$SPIN" -gt 27 ]]; then
                warn "    Ungültiger Pin: $SPIN (erlaubt: BCM 2–27). Erneut eingeben."
                continue
            fi
            if [[ -n "${USED_PINS[$SPIN]+x}" ]]; then
                warn "    Pin $SPIN ist bereits belegt (${USED_PINS[$SPIN]}). Anderen Pin wählen."
                continue
            fi
            SENSOR_PINS+=("$SPIN")
            USED_PINS[$SPIN]="Sensor $i"
            break
        done
    done
    success "Sensor-Pins: ${SENSOR_PINS[*]}"

    echo
    echo "─── Sensor Hardware-Konfiguration ─────────────────────"
    echo "  Internal Pull-Up: Der Pi schaltet intern einen Widerstand"
    echo "  an den Eingangspin. Nur aktivieren wenn kein externer"
    echo "  Pull-Up-Widerstand auf der Platine vorhanden ist."
    echo
    read -rp "  Internen Pull-Up aktivieren? [j/N]: " SENSOR_PU_ANSWER
    case "${SENSOR_PU_ANSWER,,}" in
        j|ja) SENSOR_INTERNAL_PULL_UP="true";  info "Sensor Pull-Up: intern (Software)" ;;
        *)    SENSOR_INTERNAL_PULL_UP="false"; info "Sensor Pull-Up: extern (Hardware)" ;;
    esac

    echo
    echo "─── Sensor Betriebsparameter ───────────────────────────"
    echo "  Diese Werte steuern wann und wie lange der Sensor"
    echo "  eine Bewässerung auslöst."
    echo
    read -rp "  Polling-Intervall in Sekunden [10]: " SENSOR_POLLING
    SENSOR_POLLING="${SENSOR_POLLING:-10}"
    if ! [[ "$SENSOR_POLLING" =~ ^[0-9]+$ ]] || [[ "$SENSOR_POLLING" -lt 1 ]]; then
        die "Ungültiges Polling-Intervall: '$SENSOR_POLLING'"
    fi

    read -rp "  Cooldown nach Auslösung in Sekunden [60]: " SENSOR_COOLDOWN
    SENSOR_COOLDOWN="${SENSOR_COOLDOWN:-60}"
    if ! [[ "$SENSOR_COOLDOWN" =~ ^[0-9]+$ ]] || [[ "$SENSOR_COOLDOWN" -lt 1 ]]; then
        die "Ungültiger Cooldown: '$SENSOR_COOLDOWN'"
    fi

    read -rp "  Standard-Bewässerungsdauer in Sekunden [30]: " SENSOR_DURATION
    SENSOR_DURATION="${SENSOR_DURATION:-30}"
    if ! [[ "$SENSOR_DURATION" =~ ^[0-9]+$ ]] || [[ "$SENSOR_DURATION" -lt 1 ]]; then
        die "Ungültige Bewässerungsdauer: '$SENSOR_DURATION'"
    fi

    success "Sensor-Parameter: Polling=${SENSOR_POLLING}s, Cooldown=${SENSOR_COOLDOWN}s, Dauer=${SENSOR_DURATION}s"
fi

echo
echo "─── Kiosk-Modus ────────────────────────────────────────"
echo "  Im Kiosk-Modus startet Chromium nach dem Boot automatisch"
echo "  im Vollbild und zeigt die Noria-Oberfläche an."
echo "  Der Benutzer kann den Browser nicht verlassen."
echo
echo "  Voraussetzung: Raspberry Pi OS with Desktop (64-bit, Trixie/Bookworm)"
echo "  (nicht Lite – ein Display-Stack muss vorhanden sein)"
echo
read -rp "  Kiosk-Modus auf diesem Gerät einrichten? [J/n]: " KIOSK_ANSWER
case "${KIOSK_ANSWER,,}" in
    n|nein) KIOSK_MODE=false; info "Kiosk-Modus: Nein" ;;
    *)      KIOSK_MODE=true;  info "Kiosk-Modus: Ja – Chromium Vollbild-Autostart wird konfiguriert" ;;
esac

# ── 3. ZUSAMMENFASSUNG & BESTÄTIGUNG ──────────────────────────────────────────
section "3 / 9  Zusammenfassung"

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
if [[ "$SENSORS_INSTALLED" == "true" ]]; then
    echo -e "  ${BOLD}Sensoren                 :${NC} ${GREEN}$NUM_SENSORS installiert${NC} – Pins: ${SENSOR_PINS[*]}"
    echo -e "  ${BOLD}Sensor Pull-Up           :${NC} $SENSOR_INTERNAL_PULL_UP"
    echo -e "  ${BOLD}Sensor Polling           :${NC} ${SENSOR_POLLING}s"
    echo -e "  ${BOLD}Sensor Cooldown          :${NC} ${SENSOR_COOLDOWN}s"
    echo -e "  ${BOLD}Sensor Bewässerungsdauer :${NC} ${SENSOR_DURATION}s"
else
    echo -e "  ${BOLD}Sensoren                 :${NC} Keine – Sensor-Tab ausgeblendet"
fi
echo
if [[ "$KIOSK_MODE" == "true" ]]; then
    echo -e "  ${BOLD}Kiosk-Modus              :${NC} ${GREEN}Ja${NC} – Chromium Vollbild (Benutzer: $KIOSK_USER)"
else
    echo -e "  ${BOLD}Kiosk-Modus              :${NC} Nein"
fi
echo
read -rp "  Installation jetzt starten? [J/n]: " CONFIRM
case "${CONFIRM,,}" in
    n|nein) echo "Abgebrochen."; exit 0 ;;
esac

echo

# ── 4. SYSTEMBENUTZER & SYSTEMABHÄNGIGKEITEN ──────────────────────────────────
section "4 / 9  Systembenutzer & Systemabhängigkeiten"

info "Installiere benötigte Systempakete..."
apt-get install -y \
    python3-venv \
    build-essential \
    libsystemd-dev \
    swig \
    liblgpio-dev \
    --quiet \
    2>&1 | grep -v "^$" || true
success "Systempakete installiert"

# Systembenutzer 'noria' anlegen (für Backend/Frontend-Services)
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

# Kiosk-spezifische Pakete und Benutzer
if [[ "$KIOSK_MODE" == "true" ]]; then
    echo
    info "Installiere Kiosk-Pakete (chromium, rpd-x-core, unclutter, xbindkeys, curl, x11-xserver-utils)..."

    # Chromium: Paketname variiert je nach Pi OS Version
    # Bookworm: 'chromium', Bullseye: 'chromium-browser'
    CHROMIUM_PKG=""
    for pkg in chromium chromium-browser; do
        if apt-cache show "$pkg" &>/dev/null 2>&1; then
            CHROMIUM_PKG="$pkg"
            break
        fi
    done
    if [[ -z "$CHROMIUM_PKG" ]]; then
        die "Chromium-Paket nicht in apt-cache gefunden.\n  Bitte sicherstellen dass Raspberry Pi OS with Desktop verwendet wird."
    fi

    apt-get install -y \
        "$CHROMIUM_PKG" \
        rpd-x-core \
        unclutter \
        xbindkeys \
        curl \
        x11-xserver-utils \
        --quiet \
        2>&1 | grep -v "^$" || true
    success "Kiosk-Pakete installiert (Chromium: $CHROMIUM_PKG)"

    # Chromium-Binary ermitteln
    CHROMIUM_BIN=""
    for bin in chromium chromium-browser; do
        if command -v "$bin" &>/dev/null; then
            CHROMIUM_BIN="$bin"
            break
        fi
    done
    [[ -z "$CHROMIUM_BIN" ]] && die "Chromium-Binary nicht gefunden nach Installation."
    success "Chromium-Binary: $CHROMIUM_BIN"

    # lightdm-Verfügbarkeit prüfen (nur mit Desktop vorhanden)
    if ! command -v lightdm &>/dev/null && ! dpkg -l lightdm 2>/dev/null | grep -q "^ii"; then
        warn "lightdm nicht gefunden."
        warn "Kiosk-Modus benötigt einen Display-Manager (lightdm)."
        warn "Bitte Raspberry Pi OS with Desktop verwenden."
        read -rp "  Trotzdem fortfahren (Kiosk-Konfiguration wird geschrieben, aber kein Autologin)? [j/N]: " KIOSK_CONT
        if [[ "${KIOSK_CONT,,}" != "j" && "${KIOSK_CONT,,}" != "ja" ]]; then
            KIOSK_MODE=false
            warn "Kiosk-Modus deaktiviert."
        fi
    fi

    if [[ "$KIOSK_MODE" == "true" ]]; then
        # Kiosk-Benutzer anlegen (normaler User, kein sudo, kein Passwort-Login)
        # Getrennt von 'noria' (System-User) für saubere Rollentrennnung:
        #   noria  → Service-User (Backend/Frontend systemd services)
        #   kiosk  → Display-User (autologin, startet Chromium)
        if id "$KIOSK_USER" &>/dev/null; then
            success "Benutzer '$KIOSK_USER' existiert bereits"
        else
            adduser \
                --gecos "Noria Kiosk Display" \
                --disabled-password \
                --shell /bin/bash \
                "$KIOSK_USER"
            # Login-Passwort explizit sperren (kein lokaler Login möglich,
            # nur über lightdm Autologin)
            passwd -l "$KIOSK_USER"
            success "Benutzer '$KIOSK_USER' angelegt (kein Passwort-Login)"
        fi

        # Nötige Gruppen für Display-Zugriff
        for grp in video audio input render; do
            if getent group "$grp" &>/dev/null; then
                usermod -aG "$grp" "$KIOSK_USER"
            fi
        done
        success "Benutzer '$KIOSK_USER' zu Display-Gruppen hinzugefügt"
    fi
fi

# ── 5. VERZEICHNISSE & CODE ───────────────────────────────────────────────────
section "5 / 9  Code deployen"

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
section "6 / 9  Python-Umgebung & Pakete"

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
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet
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
section "7 / 9  Konfigurationsdateien"

# device_config.json generieren (wird immer neu geschrieben – Hardware-Konfiguration)
info "Erstelle device_config.json..."
GPIO_JSON=""
for (( i=1; i<=NUM_VALVES; i++ )); do
    [[ $i -gt 1 ]] && GPIO_JSON+=","$'\n'"      "
    GPIO_JSON+="\"$i\": ${GPIO_PINS[$((i-1))]}"
done

# Sensor-Pins-JSON aufbauen.
# Wenn keine Sensoren installiert: leeres Objekt – Schlüssel existiert immer,
# damit der Code keinen Unterschied zwischen "Schlüssel fehlt" und "leer" machen muss.
SENSOR_PINS_JSON=""
if [[ "$SENSORS_INSTALLED" == "true" ]]; then
    SENSOR_DRIVER="rpi_switch"
    for (( i=1; i<=NUM_SENSORS; i++ )); do
        [[ $i -gt 1 ]] && SENSOR_PINS_JSON+=","$'\n'"      "
        SENSOR_PINS_JSON+="\"$i\": ${SENSOR_PINS[$((i-1))]}"
    done
else
    # Keine Sensoren: Treiber bleibt "sim", alle anderen Werte sind Defaults.
    # Das Frontend liest IRRIGATION_SENSOR_PINS und blendet den Tab aus wenn leer.
    SENSOR_DRIVER="sim"
    SENSOR_INTERNAL_PULL_UP="false"
    SENSOR_POLLING="10"
    SENSOR_COOLDOWN="60"
    SENSOR_DURATION="30"
fi

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
  "sensors": {
    "IRRIGATION_SENSOR_DRIVER": "$SENSOR_DRIVER",
    "IRRIGATION_SENSOR_INTERNAL_PULL_UP": $SENSOR_INTERNAL_PULL_UP,
    "IRRIGATION_SENSOR_PINS": {
      $SENSOR_PINS_JSON
    },
    "IRRIGATION_SENSOR_POLLING_INTERVAL_S": $SENSOR_POLLING,
    "IRRIGATION_SENSOR_COOLDOWN_S": $SENSOR_COOLDOWN,
    "IRRIGATION_SENSOR_DEFAULT_DURATION_S": $SENSOR_DURATION
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
  "_comment": "Frontend-Deployment-Konfiguration. Wird einmalig beim Start von app.py gelesen. Aenderungen erfordern Neustart. Wird NICHT zur Laufzeit beschrieben.",
  "base_url": "http://127.0.0.1:8000",
  "poll_status_s": 1,
  "poll_slow_s": 5,
  "backend_fail_threshold": 3,
  "health_timeout_s": 0.8,
  "anzahl_ventile_fallback": 6,
  "navbar_logo": "noria-icon-navbar.svg"
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
section "8 / 9  Dienste einrichten"

# ──── Backend-Service ────────────────────────────────────────────────────────
info "Erstelle noria-backend.service..."
cat > "$SYSTEMD_DIR/noria-backend.service" << EOF
# /etc/systemd/system/noria-backend.service
# Generiert von scripts/install.sh

[Unit]
Description=Noria Backend (FastAPI/uvicorn)
After=network.target
Wants=network.target
# Neustart-Limit: max. 5 Neustarts in 60s (verhindert Crash-Schleife)
StartLimitIntervalSec=60
StartLimitBurst=5

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

# Neustart bei Absturz
Restart=on-failure
RestartSec=5

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
# APP_DIR muss beschreibbar sein: rpi-lgpio/lgpio legt temporäre
# Notification-Pipes (.lgd-nfy*) im WorkingDirectory an.
ReadWritePaths=$APP_DIR $DATA_DIR $LOGS_DIR

# GPIO-Zugriff: Pi 4 und älter nutzen /dev/gpiomem, Pi 5 (RP1-Chip) nutzt
# /dev/gpiochip4. Beide werden erlaubt für maximale Kompatibilität.
DeviceAllow=/dev/gpiochip0 rw
DeviceAllow=/dev/gpiochip4 rw
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
# Neustart-Limit: max. 5 Neustarts in 60s (verhindert Crash-Schleife)
StartLimitIntervalSec=60
StartLimitBurst=5

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

info "Starte Backend (neu)..."
# restart statt start: idempotent – startet frisch bei Erstinstallation,
# startet neu bei Reinstallation (start wäre bei laufendem Service ein No-Op).
systemctl restart noria-backend

# Kurz warten damit das Backend hochfahren kann bevor das Frontend startet
info "Warte auf Backend-Start (5 Sekunden)..."
sleep 5

info "Starte Frontend (neu)..."
systemctl restart noria-frontend

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

# ── 9. KIOSK-MODUS ───────────────────────────────────────────────────────────
KIOSK_OK=false
if [[ "$KIOSK_MODE" == "true" ]]; then
    section "9 / 9  Kiosk-Modus einrichten"

    # ── 9a. X11 als Anzeigeserver erzwingen ──────────────────────────────────
    # Pi 5 + Trixie Desktop nutzt standardmäßig labwc (Wayland).
    # Pi 5 + Bookworm Desktop nutzte Wayfire (Wayland).
    # Für Kiosk-Betrieb mit rpd-x/LXDE ist X11 erforderlich; raspi-config
    # setzt es systemweit. Auf Pi 4 / anderen Systemen idempotent.
    if command -v raspi-config &>/dev/null; then
        info "Setze X11 als Anzeigeserver (erforderlich für rpd-x Kiosk)..."
        if raspi-config nonint do_wayland W1 2>/dev/null; then
            success "Anzeigeserver: X11 (wirksam nach Neustart)"
        else
            warn "raspi-config do_wayland fehlgeschlagen – bitte manuell prüfen:"
            warn "  sudo raspi-config → Advanced Options → Wayland → X11"
        fi
    else
        warn "raspi-config nicht gefunden – Anzeigeserver bitte manuell auf X11 setzen"
        warn "  raspi-config → Advanced Options → Wayland → X11"
    fi

    # ── 9b. lightdm Autologin konfigurieren ──────────────────────────────────
    # Der kiosk-User loggt sich nach dem Boot automatisch ein.
    # KRITISCH: autologin-session muss explizit auf einen in /usr/share/xsessions/
    # vorhandenen Namen gesetzt werden. Ohne gültigen Session-Namen fällt lightdm
    # auf die System-Standardsession zurück – auf Pi 5 + Trixie ist das labwc
    # (Wayland), womit der LXDE-Autostart nie ausgeführt wird.
    #
    # Session-Namens-Entwicklung im Raspberry Pi OS:
    #   Bullseye/Bookworm: LXDE-pi   (/usr/share/xsessions/LXDE-pi.desktop)
    #   Trixie:            rpd-x      (/usr/share/xsessions/rpd-x.desktop)
    # → Dynamisch ermitteln, Fallback-Kette: rpd-x → LXDE-pi → LXDE
    XSESSION_DIR="/usr/share/xsessions"
    KIOSK_SESSION=""
    for candidate in rpd-x LXDE-pi LXDE; do
        if [[ -f "$XSESSION_DIR/${candidate}.desktop" ]]; then
            KIOSK_SESSION="$candidate"
            break
        fi
    done
    if [[ -z "$KIOSK_SESSION" ]]; then
        warn "Keine bekannte Desktop-Session in $XSESSION_DIR gefunden."
        warn "Verfügbare Sessions: $(ls "$XSESSION_DIR" 2>/dev/null | tr '\n' ' ')"
        warn "Kiosk-Autologin-Session wird NICHT gesetzt – Kiosk startet möglicherweise nicht."
        warn "Nach der Installation manuell prüfen:"
        warn "  ls /usr/share/xsessions/   # → Session-Name ohne .desktop"
        warn "  sudo nano /etc/lightdm/lightdm.conf.d/50-noria-kiosk.conf"
        warn "  autologin-session=<NAME>  # korrekte Session eintragen, dann: sudo reboot"
    else
        info "Desktop-Session erkannt: $KIOSK_SESSION"
    fi

    if command -v lightdm &>/dev/null || dpkg -l lightdm 2>/dev/null | grep -q "^ii"; then
        info "Konfiguriere lightdm Autologin für Benutzer '$KIOSK_USER'..."

        # ── Drop-in-Config (conf.d) ───────────────────────────────────────────
        mkdir -p /etc/lightdm/lightdm.conf.d
        cat > /etc/lightdm/lightdm.conf.d/50-noria-kiosk.conf << EOF
# Noria Kiosk – lightdm Autologin
# Generiert von scripts/install.sh – nicht manuell editieren.
# Session-Name automatisch ermittelt: $KIOSK_SESSION
[Seat:*]
autologin-user=$KIOSK_USER
autologin-user-timeout=0
autologin-session=$KIOSK_SESSION
user-session=$KIOSK_SESSION
# Screensaver und DPMS auf X-Server-Ebene deaktivieren
xserver-command=X -s 0 -dpms
EOF

        # ── lightdm.conf direkt patchen ───────────────────────────────────────
        # Raspberry Pi OS schreibt bei der Ersteinrichtung und via raspi-config
        # den Hauptbenutzer (z.B. 'benny') direkt in /etc/lightdm/lightdm.conf.
        # lightdm wertet lightdm.conf NACH den conf.d-Dateien aus; der dort
        # hartcodierte autologin-user überschreibt deshalb unsere conf.d-Datei.
        # Lösung: lightdm.conf zusätzlich direkt patchen.
        LIGHTDM_CONF="/etc/lightdm/lightdm.conf"
        if [[ -f "$LIGHTDM_CONF" ]]; then
            sed -i "s|^autologin-user=.*|autologin-user=$KIOSK_USER|g"        "$LIGHTDM_CONF"
            sed -i "s|^autologin-session=.*|autologin-session=$KIOSK_SESSION|g" "$LIGHTDM_CONF"
            sed -i "s|^user-session=.*|user-session=$KIOSK_SESSION|g"           "$LIGHTDM_CONF"
            sed -i "s|^#autologin-user-timeout=.*|autologin-user-timeout=0|g"   "$LIGHTDM_CONF"
            success "lightdm.conf gepatcht (autologin-user=$KIOSK_USER)"
        else
            warn "lightdm.conf nicht gefunden – nur conf.d-Datei geschrieben"
        fi

        success "lightdm Autologin vollständig konfiguriert (Session: ${KIOSK_SESSION:-<nicht erkannt>})"
    else
        warn "lightdm nicht gefunden – Autologin nicht konfiguriert"
        warn "  Bitte Raspberry Pi OS with Desktop (64-bit) verwenden"
    fi

    # ── 9c. Kiosk-Start-Wrapper-Script ───────────────────────────────────────
    # Dieses Script läuft als kiosk-User nach dem Autologin.
    # Es wartet aktiv bis das Noria-Frontend auf Port 8080 antwortet,
    # dann startet es Chromium im Kiosk-Modus.
    #
    # --user-data-dir=/tmp/chromium-kiosk:
    #   Frisches Profil bei jedem Start → kein "Browser ist abgestürzt"-Dialog.
    #   /tmp wird bei Neustart geleert → kein Aufräumen nötig.
    #   WICHTIG: Da das Profil-Verzeichnis /tmp ist, wird die persistente
    #   ~/.config/chromium/Default/Preferences NICHT gelesen. Alle benötigten
    #   Chromium-Einstellungen werden deshalb im Script vor dem Start direkt
    #   in /tmp/chromium-kiosk/Default/Preferences geschrieben.
    #
    # --disable-features=TranslateUI,Translate,Notifications:
    #   Kein "Seite übersetzen?"-Popup.
    #   TranslateUI = alter Feature-Name (Chromium < v91)
    #   Translate   = aktueller Feature-Name (Chromium ≥ v91 / Raspberry Pi OS Trixie)
    info "Erstelle Kiosk-Wrapper-Script /opt/noria/kiosk-start.sh..."
    cat > "$INSTALL_DIR/kiosk-start.sh" << KIOSK_SCRIPT_EOF
#!/usr/bin/env bash
# =============================================================================
# kiosk-start.sh – Noria Kiosk-Starter
# Läuft als 'kiosk'-Benutzer nach dem lightdm Autologin.
# =============================================================================

FRONTEND_URL="http://localhost:${FRONTEND_PORT}"
LOG_PREFIX="[noria-kiosk]"

# DISPLAY explizit setzen – wird von rpd-x/LXDE normalerweise geerbt, aber
# als Absicherung exportiert damit xset/unclutter/Chromium zuverlässig den
# korrekten X-Server ansprechen.
export DISPLAY="\${DISPLAY:-:0}"

echo "\$LOG_PREFIX Starte – warte auf Noria-Frontend..."

# Warte bis Frontend antwortet (max. 120s, dann trotzdem starten)
WAIT_S=0
until curl -sf --max-time 2 "\$FRONTEND_URL" >/dev/null 2>&1; do
    if [[ \$WAIT_S -ge 120 ]]; then
        echo "\$LOG_PREFIX Frontend nach 120s nicht erreichbar – starte Chromium trotzdem"
        break
    fi
    sleep 2
    WAIT_S=\$((WAIT_S + 2))
done
echo "\$LOG_PREFIX Frontend erreichbar nach \${WAIT_S}s"

# X11-Screensaver und DPMS auf Session-Ebene deaktivieren
# (Ergänzung zur xserver-command-Einstellung in lightdm)
xset s off        2>/dev/null || true
xset -dpms        2>/dev/null || true
xset s noblank    2>/dev/null || true

# Maus-Cursor nach 0.5s Inaktivität ausblenden
unclutter -idle 0.5 -root &

# Chromium-Profil vorbereiten:
# /tmp wird bei jedem Neustart geleert → Preferences müssen jedes Mal neu
# gesetzt werden, bevor Chromium startet. Anderenfalls würde Chromium ein
# komplett leeres Profil lesen und alle Dialoge (Übersetzen, Willkommen usw.)
# wieder anzeigen.
mkdir -p /tmp/chromium-kiosk/Default
cat > /tmp/chromium-kiosk/Default/Preferences << 'PREFS_EOF'
{
   "browser": {
      "has_seen_welcome_page": true,
      "show_home_button": false
   },
   "profile": {
      "exit_type": "Normal",
      "exited_cleanly": true
   },
   "translate": {
      "enabled": false
   },
   "session": {
      "restore_on_startup": 4
   }
}
PREFS_EOF
echo "\$LOG_PREFIX Chromium-Profil vorbereitet (Übersetzungs-Prompt deaktiviert)"

# Chromium im Kiosk-Modus starten
# --kiosk:                  Vollbild, kein Schliessen, keine Adressleiste
# --noerrdialogs:           Keine Absturz-Dialoge
# --disable-infobars:       Keine Info-Leisten ("wird verwaltet von...")
# --no-first-run:           Kein Willkommens-Dialog
# --disable-pinch:          Touch-Zoom deaktiviert
# --overscroll-history-navigation=0: Kein Zurück/Vor durch Wischen
# --user-data-dir:          Frisches Profil pro Start (kein Absturz-Banner)
# --disable-features=TranslateUI,Translate:
#   Kein "Seite übersetzen?"-Popup.
#   TranslateUI = alter Feature-Name (Chromium < v91)
#   Translate   = aktueller Feature-Name (Chromium ≥ v91 / Pi OS Trixie)
#   Doppelt gesetzt für maximale Kompatibilität.
exec ${CHROMIUM_BIN} \\
    --kiosk \\
    --noerrdialogs \\
    --disable-infobars \\
    --no-first-run \\
    --disable-features=TranslateUI,Translate,Notifications,PasswordManager \\
    --disable-session-crashed-bubble \\
    --check-for-update-interval=31536000 \\
    --disable-pinch \\
    --overscroll-history-navigation=0 \\
    --password-store=basic \\
    --user-data-dir=/tmp/chromium-kiosk \\
    "\$FRONTEND_URL"
KIOSK_SCRIPT_EOF

    chmod +x "$INSTALL_DIR/kiosk-start.sh"
    chown "$KIOSK_USER:$KIOSK_USER" "$INSTALL_DIR/kiosk-start.sh"
    success "Kiosk-Wrapper-Script erstellt: $INSTALL_DIR/kiosk-start.sh"

    # ── 9d. lxsession Autostart konfigurieren ────────────────────────────────
    # rpd-x (Trixie) und LXDE-pi (Bookworm) lesen beide ihre Autostart-Datei
    # aus ~/.config/lxsession/<SESSION>/autostart. Da der Session-Name sich
    # zwischen Pi OS-Versionen geändert hat, werden alle drei Varianten
    # geschrieben damit das Script auf jeder Version korrekt funktioniert:
    #   LXDE-pi  → Bullseye / Bookworm
    #   rpd-x    → Trixie (Raspberry Pi Desktop for X)
    #   LXDE     → generischer Fallback
    # @-Präfix: lxsession startet den Prozess neu falls er beendet wird.
    info "Konfiguriere lxsession Autostart für '$KIOSK_USER'..."

    # xbindkeys-Konfiguration schreiben – BEVOR der Autostart die Datei referenziert.
    # xbindkeys arbeitet auf X11-Ebene, unabhängig vom Fenstermanager.
    # Es fängt Alt+F4 systemweit ab und ersetzt es durch einen No-Op (true).
    # Das ist die zweite Sicherheitsstufe gegen ungewolltes Schliessen von Chromium,
    # zusätzlich zur openbox rc.xml-Sperre (9f).
    XBINDKEYS_RC="$KIOSK_HOME/.config/xbindkeysrc"
    cat > "$XBINDKEYS_RC" << 'EOF'
# Noria Kiosk – xbindkeys-Konfiguration
# Generiert von scripts/install.sh – nicht manuell editieren.
# Fängt Tastenkombinationen auf X11-Ebene ab (vor dem Fenstermanager).

# Alt+F4: Fenster schliessen → No-Op (Chromium-Kiosk darf nicht beendet werden)
"true"
  alt + F4
EOF
    chown "$KIOSK_USER:$KIOSK_USER" "$XBINDKEYS_RC"
    success "xbindkeys-Konfiguration geschrieben: $XBINDKEYS_RC"

    # Autostart-Inhalt.
    # Reihenfolge ist wichtig:
    #   1. xset – Screensaver/DPMS deaktivieren (X11-Server-Einstellungen)
    #   2. xbindkeys – Tasten sperren, bevor Chromium startet
    #   3. kiosk-start.sh – Chromium starten (startet bei Absturz automatisch neu)
    # @-Präfix: lxsession startet den Prozess neu falls er beendet wird.
    AUTOSTART_CONTENT="# Noria Kiosk – lxsession Autostart
# Generiert von scripts/install.sh
# Desktop-Elemente deaktivieren
@xset s off
@xset -dpms
@xset s noblank
# Alt+F4 und weitere Tasten auf X11-Ebene sperren (Schicht 2, unabh. von openbox)
@xbindkeys --file $KIOSK_HOME/.config/xbindkeysrc
# Kiosk-Browser starten (wird bei Absturz automatisch neu gestartet)
@$INSTALL_DIR/kiosk-start.sh"

    for SESSION_DIR in LXDE-pi rpd-x LXDE; do
        mkdir -p "$KIOSK_HOME/.config/lxsession/$SESSION_DIR"
        printf '%s\n' "$AUTOSTART_CONTENT" > "$KIOSK_HOME/.config/lxsession/$SESSION_DIR/autostart"
    done
    success "lxsession Autostart konfiguriert (LXDE-pi, rpd-x, LXDE)"

    # Alle kiosk-Home-Dateien dem kiosk-User zuweisen
    chown -R "$KIOSK_USER:$KIOSK_USER" "$KIOSK_HOME"

    # ── 9f. openbox-Konfiguration (kein Rechtsklick-Desktop-Menü, keine Tasten) ──
    # openbox ist der Fenstermanager unter LXDE/rpd-x.
    # rc.xml wird vollständig aus eigenem Template geschrieben (nicht aus
    # /etc/xdg/openbox/ kopiert, da dieses Verzeichnis versionsabhängig fehlen kann).
    OPENBOX_CONFIG_DIR="$KIOSK_HOME/.config/openbox"
    info "Konfiguriere openbox (Tastensperren + kein Desktop-Kontextmenü)..."
    mkdir -p "$OPENBOX_CONFIG_DIR"

    # Leere menu.xml → kein Rechtsklick-Menü
    cat > "$OPENBOX_CONFIG_DIR/menu.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- Noria Kiosk: Rechtsklick-Menü deaktiviert -->
<openbox_menu xmlns="http://openbox.org/3.4/menu">
</openbox_menu>
EOF

    # Vollständige rc.xml aus eigenem Template.
    # Gesperrte Tasten im Kiosk-Betrieb:
    #   A-F4       → Fenster schließen  (würde Chromium beenden)
    #   W-d / W-D  → Desktop anzeigen
    #   W-e        → Dateimanager öffnen
    #   C-A-t      → Terminal öffnen
    #   C-A-F2..F6 → Wechsel zu virtuellen Terminals
    #   A-Tab      → Fensterwechsel (nur ein Fenster im Kiosk sinnvoll)
    #
    # <closable>no</closable>: openbox sendet bei A-F4 ein WM_DELETE_WINDOW
    # an das Fenster. Mit closable=no wird dieses Ereignis für Chromium
    # komplett unterdrückt – dritte Sicherheitsstufe hinter xbindkeys und
    # dem Keybind-No-Op weiter oben.
    cat > "$OPENBOX_CONFIG_DIR/rc.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!--
  Noria Kiosk – openbox-Konfiguration
  Generiert von scripts/install.sh – nicht manuell editieren.
-->
<openbox_config xmlns="http://openbox.org/3.4/rc"
                xmlns:xi="http://www.w3.org/2001/XInclude">

  <resistance>
    <strength>10</strength>
    <screen_edge_strength>20</screen_edge_strength>
  </resistance>

  <focus>
    <focusNew>yes</focusNew>
    <followMouse>no</followMouse>
    <focusLast>yes</focusLast>
    <underMouse>no</underMouse>
    <focusDelay>200</focusDelay>
    <raiseOnFocus>no</raiseOnFocus>
  </focus>

  <placement>
    <policy>Smart</policy>
    <center>yes</center>
    <monitor>Primary</monitor>
    <primaryMonitor>1</primaryMonitor>
  </placement>

  <theme>
    <font place="ActiveWindow">
      <name>Sans</name>
      <size>9</size>
    </font>
    <font place="InactiveWindow">
      <name>Sans</name>
      <size>9</size>
    </font>
  </theme>

  <desktops>
    <number>1</number>
    <firstdesk>1</firstdesk>
    <names><name>Desktop</name></names>
    <popupTime>0</popupTime>
  </desktops>

  <resize>
    <drawContents>yes</drawContents>
    <popupShow>NonPixel</popupShow>
    <popupPosition>Center</popupPosition>
    <popupFixedPosition>
      <x>10</x>
      <y>10</y>
    </popupFixedPosition>
  </resize>

  <applications>
    <!-- Chromium immer vollständig maximiert, ohne Fensterrahmen und nicht schliessbar -->
    <application class="Chromium-browser" type="normal">
      <maximized>yes</maximized>
      <decor>no</decor>
      <closable>no</closable>
    </application>
    <application class="chromium" type="normal">
      <maximized>yes</maximized>
      <decor>no</decor>
      <closable>no</closable>
    </application>
  </applications>

  <keyboard>
    <chainQuitKey>C-g</chainQuitKey>

    <!-- ══════════════════════════════════════════════════════ -->
    <!--   GESPERRTE TASTEN – kein Benutzer-Zugriff im Kiosk   -->
    <!-- ══════════════════════════════════════════════════════ -->

    <!-- Alt+F4: Fenster schließen → No-Op -->
    <keybind key="A-F4">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

    <!-- Super+D / Super+d: Desktop anzeigen → No-Op -->
    <keybind key="W-d">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="W-D">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

    <!-- Super+E: Dateimanager → No-Op -->
    <keybind key="W-e">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

    <!-- Ctrl+Alt+T: Terminal → No-Op -->
    <keybind key="C-A-t">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

    <!-- Ctrl+Alt+F2..F6: Wechsel zu virtuellen Terminals → No-Op -->
    <keybind key="C-A-F2">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="C-A-F3">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="C-A-F4">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="C-A-F5">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="C-A-F6">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

    <!-- Alt+Tab / Alt+Shift+Tab: Fensterwechsel → No-Op -->
    <keybind key="A-Tab">
      <action name="Execute"><execute>true</execute></action>
    </keybind>
    <keybind key="A-S-Tab">
      <action name="Execute"><execute>true</execute></action>
    </keybind>

  </keyboard>

  <mouse>
    <dragThreshold>1</dragThreshold>
    <doubleClickTime>200</doubleClickTime>
    <screenEdgeWarpTime>0</screenEdgeWarpTime>
    <screenEdgeWarpMouse>false</screenEdgeWarpMouse>

    <context name="Frame">
      <mousebind button="A-Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
      <mousebind button="A-Left" action="Drag">
        <action name="Move"/>
      </mousebind>
      <mousebind button="A-Right" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
      <mousebind button="A-Right" action="Drag">
        <action name="Resize"/>
      </mousebind>
    </context>

    <context name="Desktop">
      <!-- Kein Rechtsklick-Kontextmenü auf dem Desktop -->
    </context>

    <context name="Root">
      <!-- Kein Rechtsklick-Kontextmenü -->
    </context>

  </mouse>

  <menu>
    <hideDelay>200</hideDelay>
    <middle>no</middle>
    <submenuShowDelay>100</submenuShowDelay>
    <submenuHideDelay>400</submenuHideDelay>
    <applicationIcons>yes</applicationIcons>
    <manageDesktops>no</manageDesktops>
  </menu>

</openbox_config>
EOF
    success "openbox rc.xml geschrieben: A-F4, C-A-t, A-Tab und weitere Tasten gesperrt"
    chown -R "$KIOSK_USER:$KIOSK_USER" "$OPENBOX_CONFIG_DIR"
    success "openbox vollständig konfiguriert (kein Desktop-Kontextmenü)"

    KIOSK_OK=true
    success "Kiosk-Modus vollständig konfiguriert"
    warn "  Neustart erforderlich damit Kiosk-Modus aktiv wird!"
else
    section "9 / 9  Kiosk-Modus"
    info "Kiosk-Modus nicht gewählt – übersprungen."
fi

# ── ABSCHLUSS ─────────────────────────────────────────────────────────────────
echo
if [[ "$BACKEND_OK" == "true" && "$FRONTEND_OK" == "true" ]]; then
    echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║     Installation erfolgreich abgeschlossen!        ║${NC}"
    echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${YELLOW}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${YELLOW}║  Installation abgeschlossen – Services prüfen!     ║${NC}"
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
else
    echo "  + systemd-Prozess-Watchdog aktiv"
    echo "  - Hardware-Watchdog: nicht verfuegbar (kein Raspberry Pi erkannt)"
fi
echo
if [[ "$KIOSK_OK" == "true" ]]; then
    echo -e "  ${BOLD}Kiosk-Modus:${NC}"
    echo "  + Autologin konfiguriert (Benutzer: $KIOSK_USER)"
    echo "  + Desktop-Session: ${KIOSK_SESSION:-<nicht ermittelt>}"
    echo "  + Chromium startet automatisch nach dem Boot im Vollbild"
    echo "  + Anzeigeserver: X11 (Wayland deaktiviert)"
    echo "  + Kiosk-Script: $INSTALL_DIR/kiosk-start.sh"
    echo
    echo -e "  ${BOLD}${YELLOW}► Neustart jetzt durchführen damit alle Einstellungen aktiv werden:${NC}"
    echo "    sudo reboot"
elif [[ "$KIOSK_MODE" == "false" ]]; then
    echo "  Kiosk-Modus:  nicht eingerichtet"
    echo "  (Nachträglich aktivierbar: sudo bash scripts/install.sh)"
fi
echo
if [[ "$HW_WATCHDOG_ENABLED" == "true" && "$KIOSK_OK" == "false" ]]; then
    warn "  Fuer vollstaendigen Hardware-Watchdog-Schutz jetzt neu starten:"
    echo "    sudo reboot"
fi
echo
echo "  Nach einem Stromausfall starten die Dienste automatisch neu."
echo "  Alle Ventile werden beim Start sicher geschlossen."
echo
