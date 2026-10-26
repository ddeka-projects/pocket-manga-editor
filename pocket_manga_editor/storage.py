"""Atomic, per-manga reading and editing state persistence.

Each manga has at most two flat files in the app metadata folder:
``<manga>.read.json`` and ``<manga>.edit.json``. A write changes only what
was touched: a page turn replaces the saved position, and a selection change
adds or removes one image name. Saved entries are never pruned because their
folder or image is currently missing from disk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping

from .library_lock import library_mutation_lock
from .models import FolderRef, ImageRef, MangaRef
from .scanner import natural_name_key
from .workspace import (
    manga_metadata_paths,
    validate_editing_file,
    validate_live_manga_item,
    validate_live_manga_root,
    validate_reading_file,
)


class StorageError(OSError):
    """Raised when application state cannot be handled safely."""


class ReadingStateError(StorageError):
    """Raised when reading state cannot be read or replaced safely."""


class EditingStateError(StorageError):
    """Raised when editing state cannot be read or replaced safely."""


@dataclass(frozen=True, slots=True)
class SavedPosition:
    last_folder: str
    last_image: str


@dataclass(frozen=True, slots=True)
class EditingState:
    """The saved Edit position and selections, exactly as stored."""

    position: SavedPosition | None
    folders: Mapping[str, tuple[str, ...]]


class ReadingStore:
    """Persist one manga's reading bookmark."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        return validate_reading_file(manga_metadata_paths(self.working_directory, manga.name))

    def load(self, manga: MangaRef) -> SavedPosition | None:
        root, _source = validate_live_manga_root(self.working_directory, manga)
        return _load_reading(validate_reading_file(manga_metadata_paths(root, manga.name)), manga)

    def set_position(self, manga: MangaRef, folder_name: str, image_name: str) -> SavedPosition:
        with library_mutation_lock(self.working_directory) as root:
            folder = _folder_for(manga, folder_name)
            validate_live_manga_item(root, manga, folder, _image_for(folder, image_name))
            path = validate_reading_file(manga_metadata_paths(root, manga.name))
            # Refuse to overwrite damaged metadata, including changes made
            # outside the app after the current reader was opened.
            _load_reading(path, manga)
            position = SavedPosition(folder_name, image_name)
            atomic_write_json(
                path,
                {"last_folder": position.last_folder, "last_image": position.last_image},
            )
            return position


class EditingStore:
    """Persist the Edit position and sparse selections."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        return validate_editing_file(manga_metadata_paths(self.working_directory, manga.name))

    def load(self, manga: MangaRef) -> EditingState:
        root, _source = validate_live_manga_root(self.working_directory, manga)
        return _load_editing(validate_editing_file(manga_metadata_paths(root, manga.name)), manga)

    def set_position(self, manga: MangaRef, folder_name: str, image_name: str) -> EditingState:
        def update(state: EditingState) -> EditingState:
            return replace(state, position=SavedPosition(folder_name, image_name))

        return self._mutate(manga, folder_name, image_name, update)

    def set_selection(
        self,
        manga: MangaRef,
        folder_name: str,
        image_name: str,
        selected: bool,
    ) -> EditingState:
        def update(state: EditingState) -> EditingState:
            folders = dict(state.folders)
            names = list(folders.get(folder_name, ()))
            if selected and image_name not in names:
                names = sorted((*names, image_name), key=natural_name_key)
            elif not selected:
                names = [name for name in names if name != image_name]
            if names:
                folders[folder_name] = tuple(names)
            else:
                folders.pop(folder_name, None)
            # A first-ever save also needs a position; use where the user is.
            position = state.position or SavedPosition(folder_name, image_name)
            return EditingState(position, folders)

        return self._mutate(manga, folder_name, image_name, update)

    def _mutate(
        self,
        manga: MangaRef,
        folder_name: str,
        image_name: str,
        update: Callable[[EditingState], EditingState],
    ) -> EditingState:
        with library_mutation_lock(self.working_directory) as root:
            folder = _folder_for(manga, folder_name)
            validate_live_manga_item(root, manga, folder, _image_for(folder, image_name))
            path = validate_editing_file(manga_metadata_paths(root, manga.name))
            updated = update(_load_editing(path, manga))
            atomic_write_json(path, _editing_payload(updated))
            return updated


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json(path: Path, manga: MangaRef, error: type[StorageError], kind: str) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error(f"{kind} metadata for '{manga.name}' could not be read safely: {exc}") from exc


def _load_reading(path: Path, manga: MangaRef) -> SavedPosition | None:
    if not path.exists():
        return None
    payload = _read_json(path, manga, ReadingStateError, "Reading")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"last_folder", "last_image"}
        or not _safe_component(payload["last_folder"])
        or not _safe_image_name(payload["last_image"])
    ):
        raise ReadingStateError("Reading metadata uses an invalid or unsupported format.")
    return SavedPosition(payload["last_folder"], payload["last_image"])


def _load_editing(path: Path, manga: MangaRef) -> EditingState:
    if not path.exists():
        return EditingState(None, {})
    payload = _read_json(path, manga, EditingStateError, "Editing")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"last_folder", "last_image", "folders"}
        or not isinstance(payload["folders"], dict)
    ):
        raise EditingStateError("Editing metadata uses an invalid or unsupported format.")
    if not _safe_component(payload["last_folder"]) or not _safe_image_name(payload["last_image"]):
        raise EditingStateError("Editing metadata contains an unsafe resume position.")

    folders: dict[str, tuple[str, ...]] = {}
    for folder_name, value in payload["folders"].items():
        if not _safe_component(folder_name):
            raise EditingStateError("Editing metadata contains an unsafe folder entry.")
        if (
            not isinstance(value, dict)
            or set(value) != {"selected_images"}
            or not isinstance(value["selected_images"], list)
        ):
            raise EditingStateError(
                f"Editing metadata contains an invalid folder entry for '{folder_name}'."
            )
        if not all(_safe_image_name(name) for name in value["selected_images"]):
            raise EditingStateError(
                f"Editing metadata contains an unsafe image identity in '{folder_name}'."
            )
        folders[folder_name] = tuple(value["selected_images"])
    return EditingState(SavedPosition(payload["last_folder"], payload["last_image"]), folders)


def _editing_payload(state: EditingState) -> dict[str, Any]:
    if state.position is None:
        raise EditingStateError("Editing state has no position to save.")
    return {
        "last_folder": state.position.last_folder,
        "last_image": state.position.last_image,
        "folders": {
            name: {"selected_images": list(state.folders[name])}
            for name in sorted(state.folders, key=natural_name_key)
        },
    }


def _folder_for(manga: MangaRef, folder_name: str) -> FolderRef:
    folder = next((folder for folder in manga.folders if folder.name == folder_name), None)
    if folder is None:
        raise StorageError(f"Folder '{folder_name}' is not in this manga.")
    return folder


def _image_for(folder: FolderRef, image_name: str) -> ImageRef:
    image = next((image for image in folder.images if image.name == image_name), None)
    if image is None:
        raise StorageError(f"Image '{image_name}' is not in folder '{folder.name}'.")
    return image


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and os.sep not in value
        and (os.altsep is None or os.altsep not in value)
        and "\x00" not in value
        and Path(value).name == value
    )


def _safe_image_name(value: object) -> bool:
    return _safe_component(value) and Path(value).suffix.casefold() in {
        ".jpg",
        ".png",
    }
