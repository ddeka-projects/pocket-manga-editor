"""Activity-isolated Read and Edit state for the open manga."""

from __future__ import annotations

from dataclasses import dataclass
import threading

from .activity import Activity, WrongActivityError
from .snapshot import FolderEntry, MangaSnapshot
from .storage import EditingState, EditingStore, ReadingStore, SavedPosition


class ReviewError(RuntimeError):
    code = "review_error"


class ReviewSaveError(ReviewError):
    code = "save_failure"


class ReviewLoadError(ReviewError):
    code = "invalid_editing_state"


@dataclass(frozen=True, slots=True)
class PositionMutation:
    activity: Activity
    folder_id: str
    image_id: str
    current_image_id: str


@dataclass(frozen=True, slots=True)
class SelectionMutation:
    folder_id: str
    image_id: str
    selected: bool


@dataclass(slots=True)
class _State:
    position: SavedPosition | None
    # Edit only: selected image names per entry, as saved. Names whose image
    # is currently missing stay here and in the file; they are simply not shown.
    selections: dict[str, tuple[str, ...]]


class ReviewService:
    """Serve Read and Edit for one scanned manga without letting their metadata cross."""

    def __init__(
        self,
        manga: MangaSnapshot,
        *,
        reading_store: ReadingStore | None = None,
        editing_store: EditingStore | None = None,
    ) -> None:
        self.manga = manga
        self._reading_store = reading_store or ReadingStore(manga.working_directory)
        self._editing_store = editing_store or EditingStore(manga.working_directory)
        self._lock = threading.RLock()
        self._states: dict[Activity, _State] = {}

    def manga_payload(self, activity: Activity) -> dict[str, object]:
        with self._lock:
            folder, image_id = self._resume(self._state(activity))
            return {
                "activity": activity.value,
                "manga": {
                    "id": self.manga.id,
                    "name": self.manga.ref.name,
                    "current_folder_id": folder.id,
                    "current_image_id": image_id,
                    "folders": [
                        {"id": entry.id, "name": entry.ref.name} for entry in self.manga.folders
                    ],
                },
            }

    def folder_payload(self, folder_id: str, activity: Activity) -> dict[str, object]:
        with self._lock:
            folder = self.manga.folder(folder_id)
            state = self._state(activity)
            selected = set(state.selections.get(folder.ref.name, ()))
            images: list[dict[str, object]] = []
            for image_id in folder.image_ids:
                image = self.manga.image(image_id)
                item: dict[str, object] = {"id": image.id, "name": image.ref.name}
                if activity is Activity.EDIT:
                    item["selected"] = image.ref.name in selected
                images.append(item)
            return {
                "activity": activity.value,
                "folder": {
                    "id": folder.id,
                    "manga_id": self.manga.id,
                    "name": folder.ref.name,
                    "current_image_id": self._current_image_id(folder, state),
                    "images": images,
                },
            }

    def set_position(
        self,
        activity: Activity,
        folder_id: str,
        image_id: str,
    ) -> PositionMutation:
        with self._lock:
            folder = self.manga.folder(folder_id)
            image = self.manga.image_in_folder(folder_id, image_id)
            self.manga.validate_live_image(image_id)
            state = self._state(activity)
            if state.position != SavedPosition(folder.ref.name, image.ref.name):
                try:
                    if activity is Activity.READ:
                        state.position = self._reading_store.set_position(
                            self.manga.ref, folder.ref.name, image.ref.name
                        )
                    else:
                        self._apply_editing(
                            state,
                            self._editing_store.set_position(
                                self.manga.ref, folder.ref.name, image.ref.name
                            ),
                        )
                except OSError as exc:
                    raise ReviewSaveError(
                        f"Could not save {activity.value} position: {exc}"
                    ) from exc
            return PositionMutation(activity, folder.id, image.id, image.id)

    def set_selection(
        self,
        activity: Activity,
        folder_id: str,
        image_id: str,
        selected: bool,
    ) -> SelectionMutation:
        if activity is not Activity.EDIT:
            raise WrongActivityError("Selections are available only in Edit activity.")
        if not isinstance(selected, bool):
            raise ReviewError("selected must be a boolean.")
        with self._lock:
            folder = self.manga.folder(folder_id)
            image = self.manga.image_in_folder(folder_id, image_id)
            self.manga.validate_live_image(image_id)
            state = self._state(Activity.EDIT)
            already_selected = image.ref.name in state.selections.get(folder.ref.name, ())
            if already_selected != selected:
                try:
                    self._apply_editing(
                        state,
                        self._editing_store.set_selection(
                            self.manga.ref, folder.ref.name, image.ref.name, selected
                        ),
                    )
                except OSError as exc:
                    raise ReviewSaveError(f"Could not save image selection: {exc}") from exc
            return SelectionMutation(folder.id, image.id, selected)

    def _state(self, activity: Activity) -> _State:
        state = self._states.get(activity)
        if state is not None:
            return state
        try:
            if activity is Activity.READ:
                state = _State(self._reading_store.load(self.manga.ref), {})
            else:
                state = _State(None, {})
                self._apply_editing(state, self._editing_store.load(self.manga.ref))
        except OSError as exc:
            raise ReviewLoadError(
                f"Could not load {activity.value} metadata for '{self.manga.ref.name}'."
            ) from exc
        self._states[activity] = state
        return state

    @staticmethod
    def _apply_editing(state: _State, saved: EditingState) -> None:
        state.position = saved.position
        state.selections = dict(saved.folders)

    def _resume(self, state: _State) -> tuple[FolderEntry, str]:
        """Saved entry and image; else that entry's first image; else the manga's first."""

        position = state.position
        if position is not None:
            for folder in self.manga.folders:
                if folder.ref.name == position.last_folder:
                    return folder, self._image_id_named(folder, position.last_image)
        first = self.manga.folder(self.manga.folder_ids[0])
        return first, first.image_ids[0]

    def _current_image_id(self, folder: FolderEntry, state: _State) -> str:
        position = state.position
        if position is not None and position.last_folder == folder.ref.name:
            return self._image_id_named(folder, position.last_image)
        return folder.image_ids[0]

    def _image_id_named(self, folder: FolderEntry, image_name: str) -> str:
        for image_id in folder.image_ids:
            if self.manga.image(image_id).ref.name == image_name:
                return image_id
        return folder.image_ids[0]
