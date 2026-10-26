"""Thread-safe coordination for the always-on local web application."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time

from .activity import Activity, MangaUnavailableError, RescanError, WrongActivityError
from .controller import ControllerOwnership
from .models import LibraryScan, ScanIssue
from .review import PositionMutation, ReviewService, SelectionMutation
from .scanner import MangaMissingError, ScanError, scan_library, scan_manga
from .snapshot import ImageEntry, LibraryIndex, MangaSnapshot


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    active_controller: bool
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class _ActivityBinding:
    page_id: str
    activity: Activity


class LibraryService:
    """Own the manga list, the one open manga, one controller, and filesystem mutations.

    The library scan lists manga folder names only. A manga's entries and
    images are scanned when it is opened, and that scan is replaced the next
    time any manga is opened.
    """

    def __init__(
        self,
        library_scan: LibraryScan,
        *,
        controller: ControllerOwnership | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._controller = controller or ControllerOwnership()
        self._library = LibraryIndex.build(library_scan)
        self._review: ReviewService | None = None
        self._activity_binding: _ActivityBinding | None = None

    @property
    def working_directory(self) -> Path:
        with self._lock:
            return self._library.working_directory

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
                return self._library_payload()

    def rescan(self, page_id: str, token: str) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            started = time.perf_counter()
            try:
                scan = scan_library(self.working_directory)
                library = LibraryIndex.build(scan)
            except Exception as exc:
                LOGGER.exception("Library rescan failed; retaining the prior library.")
                raise RescanError(
                    "The library could not be rescanned; the previous library is still active."
                ) from exc
            _log_issues(scan.issues)
            LOGGER.info(
                "Library rescanned: %d manga in %.2fs.",
                len(library.mangas),
                time.perf_counter() - started,
            )
            with self._lock:
                self._library = library
                self._review = None
                self._activity_binding = None
                return self._library_payload()

    def scan_manga(self, page_id: str, token: str, manga_id: str) -> dict[str, object]:
        """Scan one manga afresh, as when its library tile is tapped."""

        with self._controller.authorized(page_id, token):
            with self._lock:
                self._activity_binding = None
                manga = self._open_locked(manga_id).manga
                return {"manga": {"id": manga.id, "name": manga.ref.name}}

    def open_manga(
        self,
        page_id: str,
        token: str,
        manga_id: str,
        activity: Activity,
    ) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                review = self._review
                if review is None or review.manga.id != manga_id:
                    # Restoring a view after takeover or history navigation.
                    review = self._open_locked(manga_id)
                self._activity_binding = _ActivityBinding(page_id, activity)
                return review.manga_payload(activity)

    def folder(
        self,
        page_id: str,
        token: str,
        folder_id: str,
        activity: Activity,
    ) -> dict[str, object]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                review = self._require_activity_locked(page_id, activity)
                return review.folder_payload(folder_id, activity)

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
                review = self._require_activity_locked(page_id, activity)
                return review.set_position(activity, folder_id, image_id)

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
                review = self._require_activity_locked(page_id, activity)
                if activity is not Activity.EDIT:
                    raise WrongActivityError("Selections are available only in Edit activity.")
                return review.set_selection(activity, folder_id, image_id, selected)

    def image_for_delivery(
        self, page_id: str, token: str, image_id: str
    ) -> tuple[MangaSnapshot, ImageEntry]:
        with self._controller.authorized(page_id, token):
            with self._lock:
                manga = self._require_activity_locked(page_id, None).manga
                return manga, manga.image(image_id)

    def status(self) -> ServiceStatus:
        controller = self._controller.snapshot()
        with self._lock:
            return ServiceStatus(controller["claimed"], self._library.snapshot_id)

    def _library_payload(self) -> dict[str, object]:
        library = self._library
        return {
            "snapshot_id": library.snapshot_id,
            "mangas": [{"id": manga.id, "name": manga.name} for manga in library.mangas],
            "issue_count": library.issue_count,
        }

    def _open_locked(self, manga_id: str) -> ReviewService:
        entry = self._library.manga(manga_id)
        self._review = None
        started = time.perf_counter()
        try:
            scan = scan_manga(self.working_directory, entry.name)
        except MangaMissingError as exc:
            raise MangaUnavailableError(
                "manga_missing",
                f"'{entry.name}' is no longer in the library folder. Refresh the library.",
            ) from exc
        except (ScanError, OSError) as exc:
            LOGGER.exception("Could not scan manga '%s'.", entry.name)
            raise MangaUnavailableError(
                "scan_failed", f"'{entry.name}' could not be scanned. Check the server log."
            ) from exc
        _log_issues(scan.issues)
        LOGGER.info(
            "Scanned '%s': %d entries, %d images in %.2fs.",
            entry.name,
            len(scan.manga.folders),
            sum(len(folder.images) for folder in scan.manga.folders),
            time.perf_counter() - started,
        )
        if scan.entry_folder_count == 0:
            raise MangaUnavailableError(
                "no_entry_folders",
                f"'{entry.name}' has no entry folders. Its images must be inside folders "
                "such as chapters or volumes.",
            )
        if not scan.manga.folders:
            raise MangaUnavailableError(
                "no_images",
                f"None of the folders in '{entry.name}' contain JPG or PNG images.",
            )
        self._review = ReviewService(
            MangaSnapshot.build(self.working_directory, entry.id, scan.manga)
        )
        return self._review

    def _require_activity_locked(
        self,
        page_id: str,
        activity: Activity | None,
    ) -> ReviewService:
        binding = self._activity_binding
        if (
            binding is None
            or self._review is None
            or binding.page_id != page_id
            or (activity is not None and binding.activity is not activity)
        ):
            raise WrongActivityError("Choose the matching Read or Edit activity before continuing.")
        return self._review


def _log_issues(issues: tuple[ScanIssue, ...]) -> None:
    for issue in issues:
        LOGGER.warning("Ignored %s: %s", issue.path, issue.message)
