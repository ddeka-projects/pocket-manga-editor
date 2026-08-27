"""Activity-isolated Companion state with immediate durable persistence."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from ..storage import EditingSnapshot, EditingStore, ReadingSnapshot, ReadingStore
from .snapshot import FolderSnapshotEntry, LibrarySnapshot, MangaSnapshotEntry
from .state import CompanionActivity, WrongActivityError


class ReviewError(RuntimeError):
    code = "review_error"


class ReviewSaveError(ReviewError):
    code = "save_failure"


class ReviewLoadError(ReviewError):
    code = "invalid_editing_state"


@dataclass(frozen=True, slots=True)
class ActivityContext:
    activity: CompanionActivity
    manga_id: str
    manga_name: str
    folder_id: str
    folder_name: str
    image_id: str
    image_name: str
    selected_count: int | None


@dataclass(frozen=True, slots=True)
class PositionMutation:
    activity: CompanionActivity
    folder_id: str
    image_id: str
    current_image_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class SelectionMutation:
    folder_id: str
    image_id: str
    current_image_id: str
    selected: bool
    folder_selected_count: int
    manga_selected_count: int
    revision: int


@dataclass(slots=True)
class _FolderState:
    selected_images: set[str]
    revision: int = 0


@dataclass(slots=True)
class _MangaState:
    last_folder: str
    last_image: str
    folders: dict[str, _FolderState]
    warnings: tuple[str, ...]


class ReviewService:
    """Serve Read and Edit without allowing their metadata to cross."""

    def __init__(
        self,
        snapshot: LibrarySnapshot,
        *,
        reading_store: ReadingStore | None = None,
        editing_store: EditingStore | None = None,
        context_callback: Callable[[ActivityContext], None] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._reading_store = reading_store or ReadingStore(snapshot.working_directory)
        self._editing_store = editing_store or EditingStore(snapshot.working_directory)
        self._context_callback = context_callback
        self._lock = threading.RLock()
        self._states: dict[tuple[CompanionActivity, str], _MangaState] = {}
        self._last_context: ActivityContext | None = None

    def library_payload(self) -> dict[str, object]:
        """Return an activity-neutral library without reading either store."""

        with self._lock:
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "mangas": [
                    {
                        "id": manga.id,
                        "name": manga.ref.name,
                        "folder_count": len(manga.folder_ids),
                    }
                    for manga in self.snapshot.mangas
                ],
                "issue_count": self.snapshot.issue_count,
            }

    def manga_payload(
        self, manga_id: str, activity: CompanionActivity
    ) -> dict[str, object]:
        with self._lock:
            manga = self.snapshot.manga(manga_id)
            state = self._state(manga, activity)
            current_folder = self._folder_for_name(manga, state.last_folder)
            self._publish_context(activity, manga, current_folder, state)
            folders: list[dict[str, object]] = []
            for folder_id in manga.folder_ids:
                folder = self.snapshot.folder(folder_id)
                item: dict[str, object] = {
                    "id": folder.id,
                    "name": folder.ref.name,
                    "image_count": len(folder.image_ids),
                }
                if activity is CompanionActivity.EDIT:
                    item["selected_count"] = len(
                        self._folder_state(state, folder).selected_images
                    )
                folders.append(item)

            manga_payload: dict[str, object] = {
                "id": manga.id,
                "name": manga.ref.name,
                "current_folder_id": current_folder.id,
                "current_image_id": self._image_id_for_name(
                    current_folder, state.last_image
                ),
                "folders": folders,
            }
            if activity is CompanionActivity.EDIT:
                manga_payload["selected_count"] = self._selected_count(state)
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "activity": activity.value,
                "manga": manga_payload,
                "warnings": self._public_warnings(activity, state.warnings),
            }

    def folder_payload(
        self, folder_id: str, activity: CompanionActivity
    ) -> dict[str, object]:
        with self._lock:
            folder = self.snapshot.folder(folder_id)
            manga = self.snapshot.manga(folder.manga_id)
            state = self._state(manga, activity)
            folder_state = self._folder_state(state, folder)
            self._publish_context(activity, manga, folder, state)
            images: list[dict[str, object]] = []
            for image_id in folder.image_ids:
                image = self.snapshot.image(image_id)
                item: dict[str, object] = {
                    "id": image.id,
                    "name": image.ref.name,
                    "image_url": f"/api/image/{image.id}",
                }
                if activity is CompanionActivity.EDIT:
                    item["selected"] = image.ref.name in folder_state.selected_images
                images.append(item)

            payload: dict[str, object] = {
                "id": folder.id,
                "manga_id": folder.manga_id,
                "name": folder.ref.name,
                "current_image_id": self._image_id_for_name(
                    folder,
                    state.last_image
                    if state.last_folder == folder.ref.name
                    else folder.ref.images[0].name,
                ),
                "revision": folder_state.revision,
                "images": images,
            }
            if activity is CompanionActivity.EDIT:
                payload.update(
                    {
                        "selected_count": len(folder_state.selected_images),
                        "manga_selected_count": self._selected_count(state),
                    }
                )
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "activity": activity.value,
                "folder": payload,
                "warnings": self._public_warnings(activity, state.warnings),
            }

    def set_position(
        self,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
    ) -> PositionMutation:
        with self._lock:
            folder = self.snapshot.folder(folder_id)
            image = self.snapshot.image_in_folder(folder_id, image_id)
            self.snapshot.validate_live_image(image_id)
            manga = self.snapshot.manga(folder.manga_id)
            state = self._state(manga, activity)
            changed = (
                state.last_folder != folder.ref.name
                or state.last_image != image.ref.name
            )
            if changed:
                revision = self._folder_state(state, folder).revision
                try:
                    if activity is CompanionActivity.READ:
                        saved = self._reading_store.set_position(
                            manga.ref, folder.ref.name, image.ref.name
                        )
                        self._apply_reading_snapshot(state, saved)
                    else:
                        saved = self._editing_store.set_position(
                            manga.ref, folder.ref.name, image.ref.name
                        )
                        self._apply_editing_snapshot(state, saved)
                except OSError as exc:
                    raise ReviewSaveError(f"Could not save {activity.value} position: {exc}") from exc
                folder_state = self._folder_state(state, folder)
                folder_state.revision = revision + 1
            else:
                folder_state = self._folder_state(state, folder)

            current_id = self._image_id_for_name(folder, state.last_image)
            mutation = PositionMutation(
                activity,
                folder.id,
                image.id,
                current_id,
                folder_state.revision,
            )
            self._publish_context(activity, manga, folder, state)
            return mutation

    def set_selection(
        self,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
        selected: bool,
    ) -> SelectionMutation:
        if activity is not CompanionActivity.EDIT:
            raise WrongActivityError("Selections are available only in Edit activity.")
        if not isinstance(selected, bool):
            raise ReviewError("selected must be a boolean.")
        with self._lock:
            folder = self.snapshot.folder(folder_id)
            image = self.snapshot.image_in_folder(folder_id, image_id)
            self.snapshot.validate_live_image(image_id)
            manga = self.snapshot.manga(folder.manga_id)
            state = self._state(manga, CompanionActivity.EDIT)
            folder_state = self._folder_state(state, folder)
            already_selected = image.ref.name in folder_state.selected_images
            if already_selected != selected:
                revision = folder_state.revision
                try:
                    saved = self._editing_store.set_selection(
                        manga.ref,
                        folder.ref.name,
                        image.ref.name,
                        selected,
                    )
                    self._apply_editing_snapshot(state, saved)
                except OSError as exc:
                    raise ReviewSaveError(f"Could not save image selection: {exc}") from exc
                folder_state = self._folder_state(state, folder)
                folder_state.revision = revision + 1

            mutation = SelectionMutation(
                folder.id,
                image.id,
                self._image_id_for_name(
                    folder,
                    state.last_image
                    if state.last_folder == folder.ref.name
                    else folder.ref.images[0].name,
                ),
                image.ref.name in folder_state.selected_images,
                len(folder_state.selected_images),
                self._selected_count(state),
                folder_state.revision,
            )
            self._publish_context(CompanionActivity.EDIT, manga, folder, state)
            return mutation

    def context(self) -> ActivityContext | None:
        with self._lock:
            return self._last_context

    def flush(self) -> None:
        """Wait for any in-flight immediate save to finish."""

        with self._lock:
            return

    def _state(
        self, manga: MangaSnapshotEntry, activity: CompanionActivity
    ) -> _MangaState:
        key = (activity, manga.id)
        state = self._states.get(key)
        if state is not None:
            return state
        try:
            if activity is CompanionActivity.READ:
                loaded = self._reading_store.load(manga.ref)
                state = _MangaState("", "", {}, ())
                self._apply_reading_snapshot(state, loaded)
            else:
                loaded = self._editing_store.load(manga.ref)
                state = _MangaState("", "", {}, ())
                self._apply_editing_snapshot(state, loaded)
        except OSError as exc:
            raise ReviewLoadError(
                f"Could not load {activity.value} metadata for '{manga.ref.name}'."
            ) from exc
        self._states[key] = state
        return state

    @staticmethod
    def _apply_reading_snapshot(
        state: _MangaState, snapshot: ReadingSnapshot
    ) -> None:
        state.last_folder = snapshot.last_folder
        state.last_image = snapshot.last_image
        state.folders = {}
        state.warnings = snapshot.warnings

    @staticmethod
    def _apply_editing_snapshot(
        state: _MangaState, snapshot: EditingSnapshot
    ) -> None:
        revisions = {name: value.revision for name, value in state.folders.items()}
        state.last_folder = snapshot.last_folder
        state.last_image = snapshot.last_image
        state.folders = {
            name: _FolderState(set(folder.selected_images), revisions.get(name, 0))
            for name, folder in snapshot.folders.items()
        }
        state.warnings = snapshot.warnings

    def _folder_for_name(
        self, manga: MangaSnapshotEntry, folder_name: str
    ) -> FolderSnapshotEntry:
        for folder_id in manga.folder_ids:
            folder = self.snapshot.folder(folder_id)
            if folder.ref.name == folder_name:
                return folder
        return self.snapshot.folder(manga.folder_ids[0])

    def _image_id_for_name(
        self, folder: FolderSnapshotEntry, image_name: str
    ) -> str:
        for image_id in folder.image_ids:
            if self.snapshot.image(image_id).ref.name == image_name:
                return image_id
        return folder.image_ids[0]

    @staticmethod
    def _selected_count(state: _MangaState) -> int:
        return sum(len(folder.selected_images) for folder in state.folders.values())

    @staticmethod
    def _folder_state(
        state: _MangaState, folder: FolderSnapshotEntry
    ) -> _FolderState:
        return state.folders.setdefault(folder.ref.name, _FolderState(set()))

    @staticmethod
    def _public_warnings(
        activity: CompanionActivity, warnings: tuple[str, ...]
    ) -> list[str]:
        if not warnings:
            return []
        return [
            f"Some saved {activity.value} metadata entries were stale or invalid "
            "and were ignored."
        ]

    def _publish_context(
        self,
        activity: CompanionActivity,
        manga: MangaSnapshotEntry,
        folder: FolderSnapshotEntry,
        state: _MangaState,
    ) -> None:
        image_name = (
            state.last_image
            if state.last_folder == folder.ref.name
            else folder.ref.images[0].name
        )
        image_id = self._image_id_for_name(folder, image_name)
        image = self.snapshot.image(image_id)
        context = ActivityContext(
            activity,
            manga.id,
            manga.ref.name,
            folder.id,
            folder.ref.name,
            image.id,
            image.ref.name,
            self._selected_count(state)
            if activity is CompanionActivity.EDIT
            else None,
        )
        self._last_context = context
        if self._context_callback is not None:
            self._context_callback(context)
