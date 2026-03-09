#!/usr/bin/env bash
# =============================================================================
# update.sh – Noria Update-Script
# =============================================================================
#
# Verwendung (aus dem Repo-Root, nach git pull):
#   sudo bash scripts/update.sh
#
# Was dieses Script tut:
#   1. Services stoppen
#   2. Code nach /opt/noria/app deployen
#   3. Python-Pakete aktualisieren (requirements.txt)
#   4. Services neu starten
#   5. Status ausgeben
#
# WICHTIG: Datendateien (data/) werden NICHT überschrieben.
#          Konfigurationen bleiben erhalten.
# =============================================================================

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}    $*"; }
success() { echo -e "${GREEN}[OK]${NC}      $*"; }
warn()    { echo -e "${YELLOW}[WARNUNG]${NC} $*"; }
die()     { echo -e "${RED}[FEHLER]${NC}  $*" >&2; exit 1; }

INSTALL_DIR="/opt/noria"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [[ -f "$REPO_DIR/main.py" ]]; then
    SOURCE_DIR="$REPO_DIR"
elif [[ -f "$REPO_DIR/app/main.py" ]]; then
    SOURCE_DIR="$REPO_DIR/app"
else
    die "main.py nicht gefunden. Bitte aus dem Repo-Root ausführen:\n  sudo bash scripts/update.sh"
fi

echo
echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${BLUE}  NORIA – UPDATE${NC}"
echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen: sudo bash scripts/update.sh"
[[ -d "$APP_DIR" ]] || die "Keine Installation gefunden unter $APP_DIR.\n  Bitte zuerst scripts/install.sh ausführen."

# NORIA_BOOTSTRAP=1: vom bootstrap.sh aufgerufen (curl|bash – kein interaktives
# Terminal). Bestätigungs-Prompt überspringen; der User hat durch das Ausführen
# von bootstrap.sh bereits explizit ein Update angefordert.
if [[ "${NORIA_BOOTSTRAP:-0}" == "1" ]]; then
    info "Automatisches Update via Bootstrap – Bestätigung übersprungen."
else
    read -rp "Update durchführen? Services werden kurz gestoppt. [J/n]: " CONFIRM
    case "${CONFIRM,,}" in
        n|nein) echo "Abgebrochen."; exit 0 ;;
    esac
fi

# Services stoppen
info "Stoppe Services..."
systemctl stop noria-frontend noria-backend 2>/dev/null || true
success "Services gestoppt"

# Code aktualisieren
info "Aktualisiere Anwendungscode..."
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
    --exclude='scripts/' \
    --exclude='docs/' \
    "$SOURCE_DIR/" "$APP_DIR/"
success "Code aktualisiert"

# Abhängigkeiten aktualisieren
info "Aktualisiere Python-Pakete..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
# requirements.txt liegt im Repo-Root, nicht in app/ – daher $REPO_DIR, nicht $APP_DIR
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet
success "Pakete aktualisiert"

# Berechtigungen sicherstellen
chown -R noria:noria "$APP_DIR"

# Services neu starten
info "Starte Backend neu..."
systemctl daemon-reload
systemctl restart noria-backend
sleep 5

info "Starte Frontend neu..."
systemctl restart noria-frontend
sleep 2

# Status
if systemctl is-active --quiet noria-backend; then
    success "noria-backend läuft"
else
    warn "noria-backend nicht aktiv – Logs: sudo journalctl -u noria-backend -n 30"
fi

if systemctl is-active --quiet noria-frontend; then
    success "noria-frontend läuft"
else
    warn "noria-frontend nicht aktiv – Logs: sudo journalctl -u noria-frontend -n 30"
fi

echo
echo -e "${BOLD}${GREEN}Update abgeschlossen.${NC}"
echo
