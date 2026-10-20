"""Thread-safe coordination for the always-on local web application."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading

from . import exporter as exporter_module
from .models import ScanResult
from .scanner import scan_working_directory
from .controller import ControllerOwnership
from .review import PositionMutation, ReviewService, SelectionMutation
from .snapshot import ImageSnapshotEntry, LibrarySnapshot
from .activity import (
    Activity,
    RescanError,
    WrongActivityError,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    active_controller: bool
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class ExportMutation:
    selected_folder_count: int
    selected_image_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActivityBinding:
    page_id: str
    manga_id: str
    activity: Activity


class LibraryService:
    """Own the live library snapshot, one controller, and filesystem mutations."""

    def __init__(
        self,
        working_directory: str | Path,
        scan_result: ScanResult,
        *,
        controller: ControllerOwnership | None = None,
    ) -> None:
        snapshot = LibrarySnapshot.build(working_directory, scan_result)
        self._lock = threading.RLock()
        self._controller = controller or ControllerOwnership()
        self._snapshot = snapshot
        self._review = ReviewService(snapshot)
        self._activity_binding: _ActivityBinding | None = None

    @property
    def working_directory(self) -> Path:
        with self._lock:
            return self._snapshot.working_directory

    def claim_controller(self, page_id: str, *, takeover: bool = False) -> dict[str, str]:
        with self._controller.claimed(page_id, takeover=takeover) as grant:
            with self._lock:
                self._activity_binding = None
            return grant

    def release_controller(self, page_id: str, token: str) -> None:
        with self._controller.authorized(page_id, token):
            with self._lock:
                self._activity_binding = None
            self._controller.release(page_id, token)

    def clear_controller(self) -> None:
        """Drop volatile controller state during process shutdown."""

        self._controller.disconnect()
        with self._lock:
            self._activity_binding = None

    def library(self, page_id: str, token: str) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                return self._review.library_payload()

    def open_manga(
        self,
        page_id: str,
        token: str,
        manga_id: str,
        activity: Activity,
    ) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                self._review.snapshot.manga(manga_id)
                self._activity_binding = _ActivityBinding(page_id, manga_id, activity)
                return self._review.manga_payload(manga_id, activity)

    def folder(
        self,
        page_id: str,
        token: str,
        folder_id: str,
        activity: Activity,
    ) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                self._require_activity_locked(page_id, activity, folder_id=folder_id)
                return self._review.folder_payload(folder_id, activity)

    def set_position(
        self,
        page_id: str,
        token: str,
        activity: Activity,
        folder_id: str,
        image_id: str,
    ) -> PositionMutation:
        with self._controller.authorized(page_id, token):
            with self._lock:
                self._require_activity_locked(page_id, activity, folder_id=folder_id)
                return self._review.set_position(activity, folder_id, image_id)

    def set_selection(
        self,
        page_id: str,
        token: str,
        activity: Activity,
        folder_id: str,
        image_id: str,
        selected: bool,
    ) -> SelectionMutation:
        with self._controller.authorized(page_id, token):
            with self._lock:
                self._require_activity_locked(page_id, activity, folder_id=folder_id)
                if activity is not Activity.EDIT:
                    raise WrongActivityError("Selections are available only in Edit activity.")
                return self._review.set_selection(activity, folder_id, image_id, selected)

    def image_for_delivery(
        self, page_id: str, token: str, image_id: str
    ) -> tuple[LibrarySnapshot, ImageSnapshotEntry]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                image = self._review.snapshot.image(image_id)
                self._require_activity_locked(page_id, None, folder_id=image.folder_id)
                return self._review.snapshot, image

    def rescan(self, page_id: str, token: str) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            root = self.working_directory
            try:
                exporter_module.recover_interrupted_exports(root)
                scan_result = scan_working_directory(root)
                snapshot = LibrarySnapshot.build(root, scan_result)
                review = ReviewService(snapshot)
            except Exception as exc:
                LOGGER.exception("Library rescan failed; retaining the prior snapshot.")
                raise RescanError(
                    "The library could not be rescanned; the previous library " "is still active."
                ) from exc

            with self._lock:
                self._snapshot = snapshot
                self._review = review
                self._activity_binding = None
                return review.library_payload()

    def export_preview(self, page_id: str, token: str, manga_id: str):
        with self._controller.authorized(page_id, token):
            with self._lock:
                snapshot = self._snapshot
                manga = snapshot.manga(manga_id).ref
            return exporter_module.inspect_export(snapshot.working_directory, manga)

    def export_manga(
        self,
        page_id: str,
        token: str,
        manga_id: str,
        confirm_unrecognized_output: bool,
    ) -> ExportMutation:
        with self._controller.authorized(page_id, token):
            with self._lock:
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
                    (
                        "Export succeeded, but temporary cleanup is pending. "
                        "The server will retry it automatically."
                    ),
                )
                if result.warnings
                else ()
            )
            return ExportMutation(
                result.folder_count,
                result.image_count,
                public_warnings,
            )

    def status(self) -> ServiceStatus:
        controller = self._controller.snapshot()
        with self._lock:
            return ServiceStatus(
                controller["claimed"],
                self._snapshot.snapshot_id,
            )

    def _require_activity_locked(
        self,
        page_id: str,
        activity: Activity | None,
        *,
        folder_id: str,
    ) -> None:
        binding = self._activity_binding
        if (
            binding is None
            or binding.page_id != page_id
            or (activity is not None and binding.activity is not activity)
        ):
            raise WrongActivityError("Choose the matching Read or Edit activity before continuing.")
        folder = self._snapshot.folder(folder_id)
        if folder.manga_id != binding.manga_id:
            raise WrongActivityError("This folder is outside the chosen manga activity.")
