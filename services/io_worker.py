# services/io_worker.py
"""
IO-Worker: Thread-safe Hardware-Abstraktion für Ventilsteuerung

Der IO-Worker läuft in einem separaten Thread und führt alle Hardware-Operationen aus.
Dies verhindert, dass der State-Lock während langsamer GPIO-Operationen blockiert wird.

Vorteile:
- State-Lock wird nicht während I/O blockiert
- Timeout-Handling für Hardware-Operationen
- Bessere Fehler-Isolation
- Test-Friendly (Mock-Worker möglich)
"""

from __future__ import annotations
import queue
import threading
import time
from dataclasses import dataclass
from typing import Literal
from core.logging import log_event, logger

# BUGFIX: ValveDriverError muss auf Modul-Ebene importiert werden,
# damit _execute_command() (separate Methode) darauf zugreifen kann.
# Vorher war der Import nur innerhalb von _worker_loop() → NameError
# sobald ein echter GPIO-Fehler auftrat.
from services.valve_driver import ValveDriverError


@dataclass
class IOCommand:
    """Command für IO-Worker"""
    action: Literal["open", "close", "close_all"]
    zone: int | None = None
    response_queue: queue.Queue | None = None
    request_id: str = ""  # Für Logging/Debugging


@dataclass
class IOResult:
    """Ergebnis einer Hardware-Operation"""
    success: bool
    zone: int | None = None
    error: str | None = None
    duration_ms: float = 0.0  # Wie lange hat die Operation gedauert?


class IOWorker:
    """
    IO-Worker für Hardware-Operationen.
    
    Läuft in separatem Thread und führt Hardware-Ops sequenziell aus.
    Thread-safe: Mehrere Threads können gleichzeitig Commands senden.
    """
    
    def __init__(self, max_queue_size: int = 100):
        self._cmd_queue: queue.Queue[IOCommand] = queue.Queue(maxsize=max_queue_size)
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._started = False
        
    def start(self) -> None:
        """Startet den IO-Worker Thread"""
        if self._started:
            logger.warning("IO-Worker bereits gestartet")
            return
            
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="IOWorker"
        )
        self._thread.start()
        self._started = True
        log_event("io_worker_started", source="system")
        
    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Stoppt den IO-Worker gracefully"""
        if not self._started:
            return
            
        log_event("io_worker_shutdown_requested", source="system")
        self._shutdown.set()
        
        if self._thread:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                log_event(
                    "io_worker_shutdown_timeout",
                    level="warning",
                    source="system",
                    timeout_s=timeout_s
                )
            else:
                log_event("io_worker_stopped", source="system")
        
        self._started = False
    
    def send_command(
        self,
        cmd: IOCommand,
        timeout_s: float = 5.0
    ) -> IOResult:
        """
        Sendet Command an IO-Worker und wartet auf Ergebnis.
        
        Thread-safe: Kann von mehreren Threads gleichzeitig aufgerufen werden.
        
        Args:
            cmd: Das auszuführende Command
            timeout_s: Max. Wartezeit auf Ergebnis
            
        Returns:
            IOResult mit success/error
        """
        if not self._started:
            return IOResult(
                success=False,
                zone=cmd.zone,
                error="IO-Worker nicht gestartet"
            )
        
        # Response-Queue für diesen Command erstellen
        response_q: queue.Queue[IOResult] = queue.Queue(maxsize=1)
        cmd.response_queue = response_q
        
        # Command in Worker-Queue einreihen
        try:
            self._cmd_queue.put(cmd, timeout=1.0)
        except queue.Full:
            log_event(
                "io_worker_queue_full",
                level="error",
                source="system",
                action=cmd.action,
                zone=cmd.zone
            )
            return IOResult(
                success=False,
                zone=cmd.zone,
                error="IO-Worker Queue voll (System überlastet)"
            )
        
        # Auf Ergebnis warten
        try:
            result = response_q.get(timeout=timeout_s)
            return result
        except queue.Empty:
            log_event(
                "io_worker_timeout",
                level="error",
                source="system",
                action=cmd.action,
                zone=cmd.zone,
                timeout_s=timeout_s
            )
            return IOResult(
                success=False,
                zone=cmd.zone,
                error=f"IO-Worker Timeout nach {timeout_s}s"
            )
    
    def _worker_loop(self) -> None:
        """
        Hauptschleife des IO-Workers.
        Läuft in separatem Thread und verarbeitet Commands sequenziell.
        """
        # BUGFIX: ValveDriverError wird jetzt auf Modul-Ebene importiert,
        # daher hier nur noch get_valve_driver nötig.
        from services.valve_driver import get_valve_driver
        
        driver = None
        
        # Driver lazy initialisieren (erst hier, nicht im Main-Thread)
        try:
            driver = get_valve_driver()
            log_event(
                "io_worker_driver_initialized",
                source="system",
                driver=getattr(driver, "name", "unknown")
            )
        except Exception as e:
            logger.exception("IO-Worker: Driver-Initialisierung fehlgeschlagen")
            log_event(
                "io_worker_driver_init_failed",
                level="error",
                source="system",
                error=repr(e)
            )
            # Worker läuft weiter, aber alle Commands werden fehlschlagen
        
        while not self._shutdown.is_set():
            # Command holen (mit Timeout für Shutdown-Check)
            try:
                cmd = self._cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            
            # Command ausführen
            result = self._execute_command(cmd, driver)
            
            # Ergebnis zurücksenden
            if cmd.response_queue:
                try:
                    cmd.response_queue.put(result, timeout=0.1)
                except queue.Full:
                    # Response-Queue voll (sollte nie passieren, da maxsize=1)
                    log_event(
                        "io_worker_response_queue_full",
                        level="warning",
                        source="system",
                        action=cmd.action,
                        zone=cmd.zone
                    )
    
    def _execute_command(
        self,
        cmd: IOCommand,
        driver
    ) -> IOResult:
        """
        Führt ein einzelnes Command aus.
        
        Misst Ausführungszeit und fängt alle Exceptions.
        ValveDriverError ist auf Modul-Ebene importiert und daher hier verfügbar.
        """
        if driver is None:
            return IOResult(
                success=False,
                zone=cmd.zone,
                error="Valve-Driver nicht verfügbar"
            )
        
        start_time = time.monotonic()
        result = IOResult(success=False, zone=cmd.zone)
        
        try:
            if cmd.action == "open":
                driver.open(cmd.zone)
                result.success = True
                
            elif cmd.action == "close":
                driver.close(cmd.zone)
                result.success = True
                
            elif cmd.action == "close_all":
                driver.close_all()
                result.success = True
                
            else:
                result.error = f"Unbekannte Action: {cmd.action}"
                
        except ValveDriverError as e:
            # Erwarteter Hardware-Fehler – jetzt korrekt abgefangen dank Modul-Import
            result.error = str(e)
            log_event(
                "io_worker_valve_error",
                level="error",
                source="system",
                action=cmd.action,
                zone=cmd.zone,
                error=str(e)
            )
            
        except Exception as e:
            # Unerwarteter Fehler
            result.error = f"Unerwarteter Fehler: {repr(e)}"
            logger.exception("IO-Worker: Unerwarteter Fehler bei Command-Ausführung")
            log_event(
                "io_worker_unexpected_error",
                level="error",
                source="system",
                action=cmd.action,
                zone=cmd.zone,
                error=repr(e)
            )
        
        # Ausführungszeit messen
        end_time = time.monotonic()
        result.duration_ms = (end_time - start_time) * 1000.0
        
        # Log bei langsamen Operationen (>500ms)
        if result.duration_ms > 500.0:
            log_event(
                "io_worker_slow_operation",
                level="warning",
                source="system",
                action=cmd.action,
                zone=cmd.zone,
                duration_ms=result.duration_ms
            )
        
        return result


# ==================== GLOBAL SINGLETON ====================

_io_worker: IOWorker | None = None
_io_worker_lock = threading.Lock()


def get_io_worker() -> IOWorker:
    """
    Gibt die globale IO-Worker Instanz zurück.
    
    Thread-safe Singleton Pattern.
    """
    global _io_worker
    
    if _io_worker is None:
        with _io_worker_lock:
            # Double-checked locking
            if _io_worker is None:
                _io_worker = IOWorker()
    
    return _io_worker


def reset_io_worker() -> None:
    """
    Resettet den globalen IO-Worker.
    
    Nur für Tests verwenden!
    """
    global _io_worker
    
    with _io_worker_lock:
        if _io_worker is not None:
            _io_worker.shutdown(timeout_s=2.0)
        _io_worker = None
    
    log_event("io_worker_reset", source="system")


def set_io_worker(worker: IOWorker) -> None:
    """
    Setzt den globalen IO-Worker (für Tests).
    
    Nur für Tests verwenden!
    """
    global _io_worker
    
    with _io_worker_lock:
        if _io_worker is not None:
            _io_worker.shutdown(timeout_s=2.0)
        _io_worker = worker
    
    log_event("io_worker_set", source="system")
