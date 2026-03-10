#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh – Noria Bootstrap-Script
# =============================================================================
#
# Dieses Script ist der EINZIGE manuelle Schritt bei einer Noria-Installation.
#
# Verwendung:
#   curl -fsSL https://raw.githubusercontent.com/bensch311/Noria-Irrigation-Control/refs/heads/main/scripts/bootstrap.sh | sudo bash
#
# Was dieses Script tut:
#   1. Root-Rechte prüfen
#   2. git und curl sicherstellen (minimale Voraussetzung für den Klon)
#   3. Repository klonen (Erstinstallation) ODER aktualisieren (Reinstallation)
#   4. install.sh aus dem Repository aufrufen – dieses übernimmt alles weitere,
#      inklusive apt upgrade, Python-Setup, systemd-Services etc.
#
# KONFIGURATION – vor Veröffentlichung anpassen:
# ─────────────────────────────────────────────
# REPO_URL: HTTPS-URL des Noria-GitHub-Repositories.
#           Format: https://github.com/BENUTZERNAME/REPONAME.git
REPO_URL="https://github.com/bensch311/Noria-Irrigation-Control.git"
#
# REPO_DIR: Lokales Verzeichnis in das das Repository geklont wird.
#           Da dieses Script als root (sudo) läuft, entspricht ~ dem
#           Home-Verzeichnis des root-Benutzers (/root).
REPO_DIR="/root/noria"
# =============================================================================

set -uo pipefail

# ── Farben & Ausgabe-Helfer ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}    $*"; }
success() { echo -e "${GREEN}[OK]${NC}      $*"; }
warn()    { echo -e "${YELLOW}[WARNUNG]${NC} $*"; }
error()   { echo -e "${RED}[FEHLER]${NC}  $*" >&2; }
die()     { error "$*"; echo -e "${RED}Bootstrap abgebrochen.${NC}" >&2; exit 1; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║       Noria – Bootstrap                            ║${NC}"
echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo
echo "  Lädt das Noria-Repository und startet die Installation."
echo

# ── 1. Root-Rechte prüfen ────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    die "Dieses Script muss als root ausgeführt werden.\n  Befehl: curl -fsSL <URL> | sudo bash"
fi
success "Root-Rechte vorhanden"

# ── 2. Minimale Abhängigkeiten sicherstellen ──────────────────────────────────
# Nur git und curl werden hier installiert – das ist alles was nötig ist um
# das Repository zu laden. Das vollständige apt upgrade übernimmt install.sh.
_NEED_APT_UPDATE=false
if ! command -v git &>/dev/null; then
    _NEED_APT_UPDATE=true
fi
if ! command -v curl &>/dev/null; then
    _NEED_APT_UPDATE=true
fi

if [[ "$_NEED_APT_UPDATE" == "true" ]]; then
    info "Installiere Voraussetzungen (git, curl)..."
    apt-get update -q 2>&1 | tail -1 || true
    apt-get install -y git curl --quiet || die "Konnte git/curl nicht installieren."
fi
success "git verfügbar ($(git --version))"

# ── 3. Repository klonen oder aktualisieren ───────────────────────────────────
echo
# _IS_REINSTALL steuert in Abschnitt 4 ob update.sh oder install.sh aufgerufen wird.
_IS_REINSTALL=false

if [[ -d "$REPO_DIR/.git" ]]; then
    # ── Update: bestehendes Repository aktualisieren ─────────────────────────
    _IS_REINSTALL=true
    info "Bestehendes Repository gefunden: $REPO_DIR"
    info "Aktualisiere Repository (git pull)..."

    # --ff-only: Nur Fast-Forward-Merges erlaubt. Falls es lokale Änderungen
    # gibt oder der Remote-Verlauf divergiert, schlägt pull sauber fehl statt
    # einen Merge-Commit zu erzeugen.
    if ! git -C "$REPO_DIR" pull --ff-only 2>&1; then
        echo
        warn "git pull fehlgeschlagen."
        warn "Mögliche Ursachen:"
        warn "  - Lokale Änderungen im Repository ($REPO_DIR)"
        warn "  - Remote-Verlauf ist nicht kompatibel (diverged)"
        echo
        warn "Lösung – Repository neu klonen:"
        warn "  sudo rm -rf $REPO_DIR"
        warn "  curl -fsSL <BOOTSTRAP-URL> | sudo bash"
        die "Repository konnte nicht aktualisiert werden."
    fi
    success "Repository aktualisiert"

elif [[ -d "$REPO_DIR" ]]; then
    # ── Verzeichnis existiert, ist aber kein Git-Repo ─────────────────────────
    # Kein stilles Überschreiben – der User muss bewusst aufräumen.
    die "Verzeichnis $REPO_DIR existiert bereits, ist aber kein Git-Repository.\n  Bitte manuell entfernen: sudo rm -rf $REPO_DIR\n  Dann dieses Script erneut ausführen."

else
    # ── Erstinstallation: Repository klonen ──────────────────────────────────
    info "Klone Repository nach $REPO_DIR ..."
    info "  Quelle: $REPO_URL"
    git clone "$REPO_URL" "$REPO_DIR" || die "git clone fehlgeschlagen.\n  URL prüfen: $REPO_URL\n  Internetverbindung prüfen."
    success "Repository geklont: $REPO_DIR"
fi
echo

# ── 4. Weiterleitung an install.sh oder update.sh ────────────────────────────
#
# Entscheidungslogik:
#   Erstinstallation       → install.sh  (interaktiv: Hardware-Konfiguration)
#   Bestehende Installation → User-Wahl:
#       Update        → update.sh   (nicht-interaktiv: Code + Pakete + Restart)
#       Neuinstallation → install.sh (interaktiv: Hardware-Konfiguration neu)
#
# NORIA_BOOTSTRAP=1 signalisiert update.sh dass es vom Bootstrap aufgerufen
# wurde und den Bestätigungs-Prompt überspringen soll. Nötig weil beim Aufruf
# via "curl | sudo bash" stdin kein Terminal ist und read sofort EOF liefert.
#
# Wenn stdin kein Terminal ist (curl | bash) kann keine interaktive Wahl
# gestellt werden → sicherer Default: Update.

INSTALL_SCRIPT="$REPO_DIR/scripts/install.sh"
UPDATE_SCRIPT="$REPO_DIR/scripts/update.sh"

echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo

if [[ "$_IS_REINSTALL" == "true" ]]; then
    # ── Bestehende Installation: Update oder Neuinstallation? ────────────────

    _ACTION="update"  # sicherer Default

    if [[ -t 0 ]]; then
        # stdin ist ein Terminal → interaktive Wahl möglich
        echo -e "  ${BOLD}Bestehende Noria-Installation gefunden.${NC}"
        echo
        echo "  Was soll gemacht werden?"
        echo
        echo -e "  ${BOLD}[1] Update${NC}         – Code und Pakete aktualisieren, Einstellungen"
        echo    "                     bleiben erhalten. Kein Neueinrichten nötig."
        echo
        echo -e "  ${BOLD}[2] Neuinstallation${NC} – Hardware-Konfiguration neu eingeben, Services"
        echo    "                     werden neu generiert. Benutzerdaten bleiben erhalten."
        echo
        while true; do
            read -rp "  Auswahl [1/2] (Standard: 1 = Update): " _CHOICE
            _CHOICE="${_CHOICE:-1}"
            case "$_CHOICE" in
                1) _ACTION="update";  break ;;
                2) _ACTION="install"; break ;;
                *) warn "  Ungültige Eingabe. Bitte 1 oder 2 eingeben." ;;
            esac
        done
    else
        # stdin ist kein Terminal (curl | bash) → kein read möglich, Default nutzen
        warn "Kein interaktives Terminal erkannt (curl | bash)."
        warn "Führe automatisch: Update (Standard)."
        warn "Für Neuinstallation dieses Script direkt ausführen:"
        warn "  sudo bash $REPO_DIR/scripts/bootstrap.sh"
        _ACTION="update"
    fi

    echo

    if [[ "$_ACTION" == "update" ]]; then
        if [[ ! -f "$UPDATE_SCRIPT" ]]; then
            die "update.sh nicht gefunden: $UPDATE_SCRIPT\n  Repository möglicherweise unvollständig."
        fi
        info "Update gewählt → Übergabe an update.sh..."
        echo
        export NORIA_BOOTSTRAP=1
        exec bash "$UPDATE_SCRIPT"
    else
        if [[ ! -f "$INSTALL_SCRIPT" ]]; then
            die "install.sh nicht gefunden: $INSTALL_SCRIPT\n  Repository möglicherweise unvollständig."
        fi
        info "Neuinstallation gewählt → Übergabe an install.sh..."
        echo
        exec bash "$INSTALL_SCRIPT"
    fi

else
    # ── Erstinstallations-Pfad ───────────────────────────────────────────────
    if [[ ! -f "$INSTALL_SCRIPT" ]]; then
        die "install.sh nicht gefunden: $INSTALL_SCRIPT\n  Repository möglicherweise unvollständig."
    fi
    info "Erstinstallation → Übergabe an install.sh..."
    echo
    exec bash "$INSTALL_SCRIPT"
fi
