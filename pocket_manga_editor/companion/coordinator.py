"""Thread-safe coordination for the always-on local web application."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Iterator

from .. import exporter as exporter_module
from ..models import ScanResult
from ..scanner import scan_working_directory
from .lease import ControllerLease, LeaseSnapshot
from .review import PositionMutation, ReviewService, SelectionMutation
from .snapshot import ImageSnapshotEntry, LibrarySnapshot
from .state import (
    CompanionActivity,
    OperationBusyError,
    RescanError,
    WrongActivityError,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CoordinatorStatus:
    active_client: bool
    active_client_id: str | None
    lease_expires_at: float | None
    snapshot_id: str
    operation: str | None


@dataclass(frozen=True, slots=True)
class ExportMutation:
    selected_folder_count: int
    selected_image_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActivityBinding:
    client_id: str
    page_instance_id: str
    manga_id: str
    activity: CompanionActivity


class CompanionCoordinator:
    """Own the live library snapshot, one controller, and filesystem mutations."""

    def __init__(
        self,
        working_directory: str | Path,
        scan_result: ScanResult,
        *,
        controller_lease: ControllerLease | None = None,
    ) -> None:
        snapshot = LibrarySnapshot.build(working_directory, scan_result)
        self._lock = threading.RLock()
        self._operation_gate = threading.Lock()
        self._operation: str | None = None
        self._lease = controller_lease or ControllerLease()
        self._snapshot = snapshot
        self._review = ReviewService(snapshot)
        self._activity_binding: _ActivityBinding | None = None

    @property
    def working_directory(self) -> Path:
        with self._lock:
            return self._snapshot.working_directory

    def claim_controller(
        self, client_id: str, page_instance_id: str
    ) -> LeaseSnapshot:
        with self._lock:
            previous = self._lease.snapshot()
            claimed = self._lease.claim(client_id, page_instance_id)
            if (
                previous.instance_id != claimed.instance_id
                or previous.page_instance_id != claimed.page_instance_id
            ):
                self._activity_binding = None
            return claimed

    def heartbeat_controller(
        self, client_id: str, page_instance_id: str
    ) -> LeaseSnapshot:
        with self._lock:
            return self._lease.heartbeat(client_id, page_instance_id)

    def release_controller(self, client_id: str, page_instance_id: str) -> None:
        with self._lock:
            self._lease.release(client_id, page_instance_id)
            self._activity_binding = None

    def disconnect_client(self) -> None:
        """Drop volatile controller state during process shutdown."""

        with self._lock:
            self._lease.disconnect()
            self._activity_binding = None

    def library(
        self, client_id: str, page_instance_id: str
    ) -> dict[str, object]:
        with self._lock:
            return self._review_locked(client_id, page_instance_id).library_payload()

    def open_manga(
        self,
        client_id: str,
        manga_id: str,
        activity: CompanionActivity,
        page_instance_id: str,
    ) -> dict[str, object]:
        with self._lock:
            review = self._review_locked(client_id, page_instance_id)
            review.snapshot.manga(manga_id)
            self._activity_binding = _ActivityBinding(
                client_id, page_instance_id, manga_id, activity
            )
            return review.manga_payload(manga_id, activity)

    def folder(
        self,
        client_id: str,
        folder_id: str,
        activity: CompanionActivity,
        page_instance_id: str,
    ) -> dict[str, object]:
        with self._lock:
            review = self._review_locked(client_id, page_instance_id)
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            return review.folder_payload(folder_id, activity)

    def set_position(
        self,
        client_id: str,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
        page_instance_id: str,
    ) -> PositionMutation:
        with self._lock:
            self._require_mutations_available_locked()
            review = self._review_locked(client_id, page_instance_id)
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            return review.set_position(activity, folder_id, image_id)

    def set_selection(
        self,
        client_id: str,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
        selected: bool,
        page_instance_id: str,
    ) -> SelectionMutation:
        with self._lock:
            self._require_mutations_available_locked()
            review = self._review_locked(client_id, page_instance_id)
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            if activity is not CompanionActivity.EDIT:
                raise WrongActivityError(
                    "Selections are available only in Edit activity."
                )
            return review.set_selection(activity, folder_id, image_id, selected)

    def image_for_delivery(
        self, client_id: str, image_id: str, page_instance_id: str
    ) -> tuple[LibrarySnapshot, ImageSnapshotEntry]:
        with self._lock:
            review = self._review_locked(client_id, page_instance_id)
            image = review.snapshot.image(image_id)
            self._require_activity_locked(
                client_id,
                page_instance_id,
                None,
                folder_id=image.folder_id,
            )
            return review.snapshot, image

    def rescan(
        self, client_id: str, page_instance_id: str
    ) -> dict[str, object]:
        self._authorize(client_id, page_instance_id)
        with self._exclusive_operation("rescan"):
            self._authorize(client_id, page_instance_id)
            root = self.working_directory
            try:
                exporter_module.recover_interrupted_exports(root)
                scan_result = scan_working_directory(root)
                snapshot = LibrarySnapshot.build(root, scan_result)
                review = ReviewService(snapshot)
            except Exception as exc:
                LOGGER.exception("Library rescan failed; retaining the prior snapshot.")
                raise RescanError(
                    "The library could not be rescanned; the previous library is still active."
                ) from exc

            with self._lock:
                self._snapshot = snapshot
                self._review = review
                self._activity_binding = None
                return review.library_payload()

    def export_preview(
        self, client_id: str, manga_id: str, page_instance_id: str
    ):
        self._authorize(client_id, page_instance_id)
        with self._exclusive_operation("export inspection"):
            with self._lock:
                self._lease.authorize(client_id, page_instance_id)
                snapshot = self._snapshot
                manga = snapshot.manga(manga_id).ref
            return exporter_module.inspect_export(snapshot.working_directory, manga)

    def export_manga(
        self,
        client_id: str,
        manga_id: str,
        confirm_unrecognized_output: bool,
        page_instance_id: str,
    ) -> ExportMutation:
        self._authorize(client_id, page_instance_id)
        with self._exclusive_operation("export"):
            with self._lock:
                self._lease.authorize(client_id, page_instance_id)
                snapshot = self._snapshot
                manga = snapshot.manga(manga_id).ref

            result = exporter_module.export_manga(
                snapshot.working_directory,
                manga,
                confirm_unrecognized_output=confirm_unrecognized_output,
            )
            for warning in result.warnings:
                LOGGER.warning("Export cleanup warning for %s: %s", manga.name, warning)
            public_warnings = (
                (
                    "Export succeeded, but temporary cleanup is pending. "
                    "The server will retry it automatically."
                ),
            ) if result.warnings else ()
            return ExportMutation(
                result.folder_count,
                result.image_count,
                public_warnings,
            )

    def status(self) -> CoordinatorStatus:
        with self._lock:
            lease = self._lease.snapshot()
            return CoordinatorStatus(
                lease.connected,
                lease.instance_id,
                lease.lease_expires_at,
                self._snapshot.snapshot_id,
                self._operation,
            )

    def _authorize(self, client_id: str, page_instance_id: str) -> None:
        with self._lock:
            self._lease.authorize(client_id, page_instance_id)

    def _review_locked(
        self, client_id: str, page_instance_id: str
    ) -> ReviewService:
        self._lease.authorize(client_id, page_instance_id)
        return self._review

    def _require_activity_locked(
        self,
        client_id: str,
        page_instance_id: str,
        activity: CompanionActivity | None,
        *,
        folder_id: str,
    ) -> None:
        binding = self._activity_binding
        if (
            binding is None
            or binding.client_id != client_id
            or binding.page_instance_id != page_instance_id
            or (activity is not None and binding.activity is not activity)
        ):
            raise WrongActivityError(
                "Choose the matching Read or Edit activity before continuing."
            )
        folder = self._snapshot.folder(folder_id)
        if folder.manga_id != binding.manga_id:
            raise WrongActivityError(
                "This folder is outside the chosen manga activity."
            )

    def _require_mutations_available_locked(self) -> None:
        if self._operation is not None:
            raise OperationBusyError(
                f"The library is busy with {self._operation}. Try again shortly."
            )

    @contextmanager
    def _exclusive_operation(self, name: str) -> Iterator[None]:
        if not self._operation_gate.acquire(blocking=False):
            with self._lock:
                active = self._operation or "another operation"
            raise OperationBusyError(
                f"The library is busy with {active}. Try again shortly."
            )
        try:
            with self._lock:
                self._operation = name
            yield
        finally:
            with self._lock:
                self._operation = None
            self._operation_gate.release()
