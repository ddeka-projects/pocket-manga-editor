"""Load settings, install the export companion, and run the local web application."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .companion_install import install_companion
from .config import ConfigurationError, Settings, load_settings
from .logging_setup import configure_logging
from .process_lock import ProcessLock
from .scanner import ScanError, scan_library
from .server import HTTPService
from .service import LibraryService


LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Hold the data-location guard until every HTTP worker has stopped."""

    parser = argparse.ArgumentParser(description="Pocket Manga Editor local server")
    parser.parse_args(argv)
    try:
        configure_logging(console=sys.stderr is not None)
    except (OSError, ValueError) as exc:
        if sys.stderr is not None:
            print(
                f"Pocket Manga Editor could not open its server log: {exc}",
                file=sys.stderr,
            )
        return 1

    try:
        settings = load_settings()
        with ProcessLock(settings.working_directory):
            return _serve(settings)
    except (
        ConfigurationError,
        ScanError,
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        LOGGER.error("Pocket Manga Editor could not start: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Pocket Manga Editor failed during startup.")
        return 1


def _serve(settings: Settings) -> int:
    try:
        companion = install_companion(settings.working_directory)
    except OSError as exc:
        # Reading and editing do not depend on the export companion.
        LOGGER.warning("Could not install the export companion: %s", exc)
    else:
        LOGGER.info("Export companion installed in %s.", companion)
    scan_result = scan_library(settings.working_directory)
    service = LibraryService(scan_result)
    server = HTTPService(service, host=settings.host, port=settings.port)
    previous_handlers: dict[int, object] = {}
    exit_code = 0
    try:
        status = server.start()
        if not status.running:
            LOGGER.error(
                "Could not start the web server: %s",
                status.error or "unknown listener error",
            )
            return 1

        for issue in scan_result.issues:
            LOGGER.warning("Ignored %s: %s", issue.path, issue.message)
        LOGGER.info(
            "Pocket Manga Editor is serving %s (%d manga).",
            status.url,
            len(scan_result.manga_names),
        )

        stop_requested = threading.Event()

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

        while not stop_requested.wait(0.5):
            if not server.running:
                LOGGER.error(
                    "The web server stopped unexpectedly: %s",
                    server.error or "unknown server error",
                )
                exit_code = 1
                break
    except KeyboardInterrupt:
        pass
    except Exception:
        LOGGER.exception("The web server failed.")
        exit_code = 1
    finally:
        for signum, previous in previous_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass
        try:
            stopped = server.stop(timeout=10.0)
            if stopped.error:
                LOGGER.warning("Web server shutdown warning: %s", stopped.error)
                exit_code = 1
        except Exception:
            LOGGER.exception("The web server could not be stopped cleanly.")
            exit_code = 1
        service.clear_controller()
        LOGGER.info("Pocket Manga Editor stopped.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
