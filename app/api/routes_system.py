# api/routes_system.py
"""
System-Endpunkte: administrative Aktionen und Monitoring auf Systemebene.

Endpunkte:
  POST /system/ack-restart     – Neustart-Hinweis quittieren
  GET  /system/logs/download   – Alle Log-Dateien als ZIP herunterladen
  GET  /system/info            – Betriebssystem-Metriken (Disk, RAM, Uptime, Netzwerk)

Alle Endpunkte erfordern API-Key-Authentifizierung (X-API-Key Header).

Hintergrund (Neustart-Erkennung):
  Das Backend legt beim Start eine Sentinel-Datei (running.lock) an und löscht
  sie beim sauberen Shutdown als allererstes. Existiert die Datei beim nächsten
  Start noch, wurde der letzte Shutdown nicht sauber durchgeführt (Stromausfall,
  SIGKILL, OOM-Kill). In diesem Fall setzt lifecycle.py state.unclean_restart=True
  und state.restart_detected_at auf den Erkennungszeitstempel.

  Das Frontend erkennt unclean_restart=True im /health-Response und zeigt
  einmalig ein Modal an. Nach Bestätigung durch den Bediener ruft das Frontend
  POST /system/ack-restart auf, was das Flag zurücksetzt und das Modal schließt.

Hintergrund (Log-Download):
  Der RotatingFileHandler erzeugt bis zu 11 Dateien:
    irrigation.jsonl          – aktuelle Log-Datei
    irrigation.jsonl.1 – .10 – rotierte Backup-Dateien (neueste = .1)

  GET /system/logs/download liest alle vorhandenen Dateien, zippt sie
  in-memory und gibt sie als StreamingResponse zurück.

Hintergrund (System-Info):
  GET /system/info liefert OS-Metriken für die Systeminfo-Anzeige im Frontend.
  Datenquellen: psutil (Disk, RAM, Uptime, Netzwerk-Status) und nmcli (WLAN-
  SSID + Signalstärke). Alle OS-Aufrufe sind best-effort: Fehler führen zu
  null-Werten im Response, nicht zu HTTP-Fehler-Codes. Der Endpunkt ist damit
  auch auf Nicht-Linux-Systemen (z.B. Windows-Dev-Umgebung) aufrufbar.
"""

import io
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from core.config import LOG_DIR, TZ
from core.state import state, state_lock
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT

router = APIRouter(dependencies=[Depends(require_api_key)])

# Name der aktuellen Log-Datei (identisch mit core/logging.py)
_LOG_BASENAME = "irrigation.jsonl"

# Rate-Limits
_DOWNLOAD_LIMIT = "5/minute"   # ZIP-Erstellung ist I/O-intensiv
_INFO_LIMIT     = "30/minute"  # Sysinfo-Poll im Slow-Takt


# ---------------------------------------------------------------------------
# POST /system/ack-restart
# ---------------------------------------------------------------------------

@router.post("/system/ack-restart")
@limiter.limit(MUTATION_LIMIT)
def ack_restart(request: Request):
    """Quittiert einen unclean Restart.

    Setzt state.unclean_restart=False und state.restart_detected_at="" zurück,
    damit das Frontend-Modal geschlossen wird und beim nächsten Poll nicht
    erneut erscheint.

    Idempotent: mehrfache Aufrufe sind harmlos.
    """
    with state_lock:
        was_set = bool(state.unclean_restart)
        state.unclean_restart = False
        state.restart_detected_at = ""

    if was_set:
        log_event("unclean_restart_acknowledged", source="operator")

    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /system/logs/download
# ---------------------------------------------------------------------------

@router.get("/system/logs/download")
@limiter.limit(_DOWNLOAD_LIMIT)
def download_logs(request: Request):
    """Liefert alle vorhandenen Log-Dateien als ZIP-Archiv.

    Sammelt irrigation.jsonl sowie alle rotierten Backup-Dateien
    (irrigation.jsonl.1 bis .10), zippt sie in-memory und gibt sie als
    Download zurück. Dateiname: noria-logs-YYYY-MM-DD.zip

    Enthält das Verzeichnis keine Log-Dateien, wird eine leere ZIP
    zurückgegeben (z.B. frisch installiertes System).
    """
    log_dir = Path(LOG_DIR)

    candidates = [log_dir / _LOG_BASENAME] + [
        log_dir / f"{_LOG_BASENAME}.{i}" for i in range(1, 11)
    ]
    log_files = [p for p in candidates if p.is_file()]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for log_path in log_files:
            zf.write(log_path, arcname=log_path.name)

    zip_bytes = buf.getvalue()
    today     = datetime.now(TZ).strftime("%Y-%m-%d")
    filename  = f"noria-logs-{today}.zip"

    log_event(
        "log_download_requested",
        source="operator",
        files_included=len(log_files),
        zip_size_bytes=len(zip_bytes),
        filename=filename,
    )

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# GET /system/info
# ---------------------------------------------------------------------------

def _collect_disk() -> dict:
    """Freier und gesamter Speicherplatz auf dem Root-Dateisystem."""
    try:
        usage = shutil.disk_usage("/")
        return {
            "total_gb":  round(usage.total / 1024**3, 1),
            "free_gb":   round(usage.free  / 1024**3, 1),
            "used_pct":  round(usage.used  / usage.total * 100, 1),
        }
    except Exception:
        return {"total_gb": None, "free_gb": None, "used_pct": None}


def _collect_memory() -> dict:
    """RAM-Nutzung via psutil."""
    try:
        mem = psutil.virtual_memory()
        return {
            "total_mb": round(mem.total / 1024**2),
            "used_mb":  round(mem.used  / 1024**2),
            "used_pct": round(mem.percent, 1),
        }
    except Exception:
        return {"total_mb": None, "used_mb": None, "used_pct": None}


def _collect_uptime() -> float | None:
    """Uptime in Sekunden seit letztem Boot (psutil.boot_time)."""
    try:
        return round(time.time() - psutil.boot_time())
    except Exception:
        return None


def _collect_wlan_details(iface_name: str) -> dict:
    """SSID und Signalstärke für ein WLAN-Interface via nmcli.

    nmcli ist auf Raspberry Pi OS (Bullseye/Bookworm/Trixie) standardmäßig
    verfügbar. Bei Fehler (z.B. Windows-Dev) werden None-Werte zurückgegeben.

    nmcli-Ausgabe (terse, -t):
      active:ssid:signal
      yes:MeinNetzwerk:72

    Wichtig:
      LC_ALL=C erzwingt englische Ausgabe – ohne dies gibt nmcli auf
      deutschen Systemen "ja" statt "yes" aus, was den Match zerstört.

      split(":", 2) mit maxsplit=2 verhindert, dass SSIDs mit Doppelpunkt
      (z.B. "Fritzbox:5G") die Feldindizes verschieben.
    """
    import os as _os
    try:
        env = {**_os.environ, "LC_ALL": "C", "LANG": "C"}
        result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid,signal", "dev", "wifi"],
            capture_output=True, text=True, timeout=3, env=env,
        )
        if result.returncode != 0:
            return {"ssid": None, "signal_pct": None}
        for line in result.stdout.splitlines():
            # nmcli -t escaped Doppelpunkte in Feldwerten als \: (terse mode).
            # re.split auf unescapte : stellt sicher dass SSIDs mit Doppelpunkt
            # (z.B. "Fritzbox:5G" → "yes:Fritzbox\:5G:65") korrekt geparst werden.
            parts = re.split(r'(?<!\\):', line, maxsplit=2)
            if len(parts) >= 3 and parts[0] == "yes":
                # \: im SSID-Teil zurück zu : übersetzen
                ssid = parts[1].replace(r'\:', ':') or None
                try:
                    signal = int(parts[2])
                except ValueError:
                    signal = None
                return {"ssid": ssid, "signal_pct": signal}
    except Exception:
        pass
    return {"ssid": None, "signal_pct": None}


def _collect_network() -> list[dict]:
    """Netzwerk-Interfaces via psutil.

    Liefert eine Liste aller relevanten Interfaces (kein Loopback, kein
    virtuelles Docker/VPN-Interface) mit Typ (LAN/WLAN), Verbindungsstatus
    und IP-Adresse. Bei WLAN-Interfaces werden zusätzlich SSID und Signal
    via nmcli abgefragt.

    Interface-Typen nach Namensprefix:
      eth*, enp*, eno*, ens* → LAN (kabelgebunden)
      wlan*, wlp*, wlx*      → WLAN (drahtlos)
      lo, docker*, veth*     → ignoriert
    """
    try:
        if_stats  = psutil.net_if_stats()
        if_addrs  = psutil.net_if_addrs()
    except Exception:
        return []

    _LAN_PREFIXES  = ("eth", "enp", "eno", "ens")
    _WLAN_PREFIXES = ("wlan", "wlp", "wlx")
    _IGNORE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr")

    results = []
    for name, stats in if_stats.items():
        # Loopback und virtuelle Interfaces überspringen
        if any(name.startswith(p) for p in _IGNORE_PREFIXES):
            continue

        if any(name.startswith(p) for p in _LAN_PREFIXES):
            iface_type = "LAN"
        elif any(name.startswith(p) for p in _WLAN_PREFIXES):
            iface_type = "WLAN"
        else:
            continue  # unbekannter Typ – nicht anzeigen

        # IPv4-Adresse suchen (AF_INET = 2)
        ip = None
        for addr in if_addrs.get(name, []):
            if addr.family == 2:  # socket.AF_INET
                ip = addr.address
                break

        entry: dict = {
            "name":    name,
            "type":    iface_type,
            "is_up":   bool(stats.isup),
            "ip":      ip,
        }

        if iface_type == "WLAN" and stats.isup:
            entry.update(_collect_wlan_details(name))
        elif iface_type == "WLAN":
            entry["ssid"]       = None
            entry["signal_pct"] = None

        results.append(entry)

    # LAN zuerst, dann WLAN
    results.sort(key=lambda x: (0 if x["type"] == "LAN" else 1, x["name"]))
    return results


@router.get("/system/info")
@limiter.limit(_INFO_LIMIT)
def system_info(request: Request):
    """Liefert Betriebssystem-Metriken für die Systeminfo-Anzeige.

    Alle Felder sind best-effort: ein Fehler bei einer einzelnen Metrik
    setzt das entsprechende Feld auf null, gibt aber keinen HTTP-Fehler zurück.
    Das Frontend zeigt null-Werte als '–' an.

    Response-Felder:
      disk.total_gb    – Gesamtkapazität des Root-Dateisystems in GB
      disk.free_gb     – Freier Speicherplatz in GB
      disk.used_pct    – Belegung in Prozent
      memory.total_mb  – Gesamter RAM in MB
      memory.used_mb   – Genutzter RAM in MB
      memory.used_pct  – RAM-Nutzung in Prozent
      uptime_s         – Sekunden seit letztem Boot
      network[]        – Liste der aktiven Netzwerk-Interfaces (LAN/WLAN)
        .name          – Interface-Name (z.B. "eth0", "wlan0")
        .type          – "LAN" oder "WLAN"
        .is_up         – true = verbunden
        .ip            – IPv4-Adresse oder null
        .ssid          – (nur WLAN) SSID oder null
        .signal_pct    – (nur WLAN) Signalstärke 0–100 % oder null
    """
    return {
        "disk":     _collect_disk(),
        "memory":   _collect_memory(),
        "uptime_s": _collect_uptime(),
        "network":  _collect_network(),
    }
