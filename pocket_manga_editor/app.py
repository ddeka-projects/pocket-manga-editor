"""Foreground bootstrap for the always-on local web server."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from .companion import CompanionCoordinator, CompanionHTTPService
from .config import ConfigurationError, load_configuration
from .exporter import ExportError, recover_interrupted_exports
from .scanner import ScanError, scan_working_directory


LOGGER = logging.getLogger(__name__)


def main() -> int:
    """Validate configuration, recover state, and serve until terminated."""

    _configure_logging()
    try:
        configuration = load_configuration()
        recovery = recover_interrupted_exports(configuration.working_directory)
        scan_result = scan_working_directory(configuration.working_directory)
        coordinator = CompanionCoordinator(
            configuration.working_directory,
            scan_result,
        )
        service = CompanionHTTPService(
            coordinator,
            host=configuration.host,
            port=configuration.port,
        )
    except (ConfigurationError, ExportError, ScanError, OSError, ValueError) as exc:
        LOGGER.error("Pocket Manga Editor could not start: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Pocket Manga Editor failed during startup.")
        return 1

    try:
        status = service.start()
    except Exception:
        LOGGER.exception("Pocket Manga Editor could not start its web server.")
        coordinator.disconnect_client()
        return 1
    if not status.running:
        LOGGER.error(
            "Pocket Manga Editor could not start its web server: %s",
            status.error or "unknown listener error",
        )
        service.stop()
        coordinator.disconnect_client()
        return 1

    if recovery.recovered_count:
        LOGGER.info(
            "Recovered %d interrupted export transaction(s).",
            recovery.recovered_count,
        )
    if scan_result.issues:
        LOGGER.warning(
            "Library scan completed with %d ignored item(s).",
            len(scan_result.issues),
        )
        for issue in scan_result.issues:
            LOGGER.warning("Ignored %s: %s", issue.path, issue.message)
    LOGGER.info(
        "Pocket Manga Editor is serving %s (%d manga).",
        status.url,
        len(scan_result.mangas),
    )

    stop_requested = threading.Event()
    previous_handlers: dict[int, object] = {}

    def request_stop(signum, _frame) -> None:
        LOGGER.info("Shutdown requested by signal %s.", signum)
        stop_requested.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except (OSError, RuntimeError, ValueError):
            continue

    exit_code = 0
    try:
        while not stop_requested.wait(0.5):
            if not service.running:
                LOGGER.error(
                    "The web server stopped unexpectedly: %s",
                    service.error or "unknown server error",
                )
                exit_code = 1
                break
    except KeyboardInterrupt:
        stop_requested.set()
    finally:
        for signum, previous in previous_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass
        try:
            stopped = service.stop(timeout=10.0)
            if stopped.error:
                LOGGER.warning("Web server shutdown warning: %s", stopped.error)
        except Exception:
            LOGGER.exception("The web server could not be stopped cleanly.")
            exit_code = 1
        coordinator.disconnect_client()
        LOGGER.info("Pocket Manga Editor stopped.")
    return exit_code


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
