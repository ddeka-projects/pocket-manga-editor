"""Application bootstrap."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from .companion import (
    CompanionCoordinator,
    CompanionHTTPService,
    CompanionState,
    CredentialVerifierStore,
)
from .companion.server import DEFAULT_PORT
from .instance_guard import InstanceGuardError, acquire_instance_guard
from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("Pocket Manga Editor")
    app.setApplicationName("Pocket Manga Editor")
    app.setApplicationVersion("0.2.0")
    app.setStyle("Fusion")

    try:
        instance_guard = acquire_instance_guard()
    except InstanceGuardError as exc:
        QMessageBox.critical(None, "Pocket Manga Editor cannot start", str(exc))
        return 1

    coordinator: CompanionCoordinator | None = None
    companion_server: CompanionHTTPService | None = None
    try:
        settings = QSettings()
        raw_port = settings.value("companion/port", DEFAULT_PORT)
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        public_host = str(settings.value("companion/public_host", "")).strip()
        app_data = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        try:
            if not app_data:
                raise OSError(
                    "The operating system did not provide an application-data folder "
                    "for the paired-device verifier."
                )
            credential_store = CredentialVerifierStore(
                Path(app_data) / "companion-device.json"
            )
            coordinator = CompanionCoordinator(credential_store=credential_store)
            companion_server = CompanionHTTPService(
                coordinator,
                port=port,
                public_host=public_host or None,
            )
            companion_server.start()
        except (OSError, ValueError) as exc:
            coordinator = None
            companion_server = None
            QMessageBox.warning(
                None,
                "Companion Mode unavailable",
                f"The desktop editor can still be used, but Companion Mode could "
                f"not be initialized.\n\n{exc}",
            )

        window = MainWindow(
            companion_coordinator=coordinator,
            companion_server=companion_server,
        )
        window.show()
        return app.exec()
    finally:
        if companion_server is not None:
            companion_server.stop()
        if coordinator is not None:
            state = coordinator.status().state
            try:
                if state is CompanionState.COMPANION_ACTIVE:
                    coordinator.begin_exit()
                    coordinator.finish_exit()
                elif state is CompanionState.EXITING_COMPANION:
                    coordinator.finish_exit()
            except Exception:
                # The process is already terminating. The HTTP listener is stopped,
                # immediate review writes have completed before their requests return,
                # and a new process starts in desktop authority.
                pass
        instance_guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
