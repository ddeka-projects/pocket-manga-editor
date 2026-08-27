"""Atomic, per-manga reading and editing state persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Collection, Mapping

from .library_lock import library_mutation_lock
from .models import FolderRef, MangaRef
from .scanner import natural_name_key
from .workspace import (
    MangaWorkspacePaths,
    manga_workspace_paths,
    validate_editing_workspace,
    validate_live_manga_item,
    validate_live_manga_root,
    validate_reading_workspace,
)


STATE_SCHEMA_VERSION = 3


class StorageError(OSError):
    """Raised when application state cannot be handled safely."""


class EditingStateError(StorageError):
    """Raised when editing state cannot support safe destructive decisions."""


@dataclass(frozen=True, slots=True)
class ReadingSnapshot:
    last_folder: str
    last_image: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EditingFolderState:
    selected_images: frozenset[str]


@dataclass(frozen=True, slots=True)
class EditingSnapshot:
    last_folder: str
    last_image: str
    folders: Mapping[str, EditingFolderState]
    warnings: tuple[str, ...] = ()


class ReadingStore:
    """Persist phone-only reading bookmarks in one manga workspace."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        paths = manga_workspace_paths(self.working_directory, manga.name)
        return validate_reading_workspace(paths).reading

    def load(self, manga: MangaRef) -> ReadingSnapshot:
        root, _source = validate_live_manga_root(self.working_directory, manga)
        paths = manga_workspace_paths(root, manga.name)
        validate_reading_workspace(paths)
        return _load_reading(paths.reading, manga)

    def set_position(
        self, manga: MangaRef, folder_name: str, image_name: str
    ) -> ReadingSnapshot:
        with library_mutation_lock(self.working_directory) as root:
            folder = _folder_for(manga, folder_name)
            image = _image_for(folder, image_name)
            validate_live_manga_item(root, manga, folder, image)
            paths = _ensure_workspace(root, manga)
            validate_reading_workspace(paths)
            snapshot = ReadingSnapshot(folder_name, image_name)
            _write_reading(paths.reading, snapshot)
            return snapshot


class EditingStore:
    """Persist web editing position and sparse selections."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        paths = manga_workspace_paths(self.working_directory, manga.name)
        return validate_editing_workspace(paths).editing

    def load(self, manga: MangaRef) -> EditingSnapshot:
        root, _source = validate_live_manga_root(self.working_directory, manga)
        return self._load_locked(root, manga)

    def set_position(
        self, manga: MangaRef, folder_name: str, image_name: str
    ) -> EditingSnapshot:
        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            return replace(
                snapshot,
                last_folder=folder_name,
                last_image=image_name,
                warnings=(),
            )

        return self._mutate(manga, folder_name, (image_name,), update)

    def set_selection(
        self,
        manga: MangaRef,
        folder_name: str,
        image_name: str,
        selected: bool,
    ) -> EditingSnapshot:
        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            folders = dict(snapshot.folders)
            previous = folders.get(folder_name, EditingFolderState(frozenset()))
            selected_images = set(previous.selected_images)
            if selected:
                selected_images.add(image_name)
            else:
                selected_images.discard(image_name)
            if selected_images:
                folders[folder_name] = EditingFolderState(
                    frozenset(selected_images)
                )
            else:
                folders.pop(folder_name, None)
            return replace(snapshot, folders=folders, warnings=())

        return self._mutate(manga, folder_name, (image_name,), update)

    def _mutate(
        self,
        manga: MangaRef,
        folder_name: str,
        image_names: Collection[str],
        operation,
    ) -> EditingSnapshot:
        with library_mutation_lock(self.working_directory) as root:
            folder = _folder_for(manga, folder_name)
            images = tuple(_image_for(folder, name) for name in image_names)
            if images:
                for image in images:
                    validate_live_manga_item(root, manga, folder, image)
            else:
                validate_live_manga_item(root, manga, folder)
            snapshot = self._load_locked(root, manga)
            updated = operation(snapshot)
            self._write_locked(root, manga, updated)
            return updated

    def _load_locked(self, root: Path, manga: MangaRef) -> EditingSnapshot:
        """Load strictly while the caller owns or does not require the mutation lock."""

        validate_live_manga_root(root, manga)
        paths = manga_workspace_paths(root, manga.name)
        validate_editing_workspace(paths)
        if not paths.editing.exists():
            return _default_editing(manga)
        try:
            with paths.editing.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EditingStateError(
                f"Editing metadata for '{manga.name}' could not be read safely: {exc}"
            ) from exc
        return _parse_editing(payload, manga)

    def _write_locked(
        self, root: Path, manga: MangaRef, snapshot: EditingSnapshot
    ) -> None:
        """Write a validated snapshot while the caller holds the mutation lock."""

        validate_live_manga_root(root, manga)
        paths = _ensure_workspace(root, manga)
        validate_editing_workspace(paths)
        payload = _editing_payload(manga, snapshot)
        atomic_write_json(paths.editing, payload)


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


def _ensure_workspace(root: Path, manga: MangaRef) -> MangaWorkspacePaths:
    paths = manga_workspace_paths(root, manga.name)
    try:
        paths.workspace.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Could not create manga workspace: {exc}") from exc
    return manga_workspace_paths(root, manga.name)


def _load_reading(path: Path, manga: MangaRef) -> ReadingSnapshot:
    default = _default_reading(manga)
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return replace(
            default,
            warnings=(f"Reading metadata was invalid and was reset: {exc}",),
        )

    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "last_folder", "last_image"}
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or not isinstance(payload.get("last_folder"), str)
        or not isinstance(payload.get("last_image"), str)
        or not _safe_component(payload.get("last_folder"))
        or not _safe_image_name(payload.get("last_image"))
    ):
        return replace(
            default,
            warnings=("Reading metadata used an invalid format and was reset.",),
        )

    folder = _folder_by_name(manga, payload["last_folder"])
    if folder is None or payload["last_image"] not in {
        image.name for image in folder.images
    }:
        return replace(
            default,
            warnings=(
                "The saved reading position was missing and was reset to the first image.",
            ),
        )
    return ReadingSnapshot(payload["last_folder"], payload["last_image"])


def _default_reading(manga: MangaRef) -> ReadingSnapshot:
    folder = _first_folder(manga)
    return ReadingSnapshot(folder.name, folder.images[0].name)


def _write_reading(path: Path, snapshot: ReadingSnapshot) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_folder": snapshot.last_folder,
            "last_image": snapshot.last_image,
        },
    )


def _default_editing(manga: MangaRef) -> EditingSnapshot:
    first_folder = _first_folder(manga)
    return EditingSnapshot(
        first_folder.name,
        first_folder.images[0].name,
        {},
    )


def _parse_editing(payload: object, manga: MangaRef) -> EditingSnapshot:
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema_version", "last_folder", "last_image", "folders"}
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or not isinstance(payload.get("last_folder"), str)
        or not isinstance(payload.get("last_image"), str)
        or not isinstance(payload.get("folders"), dict)
    ):
        raise EditingStateError("Editing metadata uses an invalid or unsupported format.")

    live_folders = {folder.name: folder for folder in manga.folders}
    folder_states: dict[str, EditingFolderState] = {}
    last_folder = payload["last_folder"]
    last_image = payload["last_image"]
    if not _safe_component(last_folder) or not _safe_image_name(last_image):
        raise EditingStateError("Editing metadata contains an unsafe resume position.")

    warnings: list[str] = []
    for folder_name, value in payload["folders"].items():
        if not _safe_component(folder_name):
            raise EditingStateError("Editing metadata contains an unsafe folder entry.")
        if (
            not isinstance(value, dict)
            or set(value) != {"selected_images"}
            or not isinstance(value.get("selected_images"), list)
        ):
            raise EditingStateError(
                f"Editing metadata contains an invalid folder entry for '{folder_name}'."
            )
        selected_values = value["selected_images"]
        if any(
            not _safe_image_name(image_name) for image_name in selected_values
        ):
            raise EditingStateError(
                f"Editing metadata contains an unsafe image identity in '{folder_name}'."
            )

        folder = live_folders.get(folder_name)
        if folder is None:
            warnings.append(f"Ignored stale editing folder '{folder_name}'.")
            continue
        live_names = {image.name for image in folder.images}
        selected: set[str] = set()
        stale = 0
        for image_name in selected_values:
            if image_name in live_names:
                selected.add(image_name)
            else:
                stale += 1
        if stale:
            warnings.append(
                f"Ignored {stale} stale selection(s) in '{folder_name}'."
            )
        if selected:
            folder_states[folder_name] = EditingFolderState(frozenset(selected))

    resume_folder = live_folders.get(last_folder)
    if resume_folder is None or last_image not in {
        image.name for image in resume_folder.images
    }:
        warnings.append(
            "The saved editing position was missing and was reset to the first image."
        )
        first_folder = _first_folder(manga)
        last_folder = first_folder.name
        last_image = first_folder.images[0].name
    return EditingSnapshot(last_folder, last_image, folder_states, tuple(warnings))


def _editing_payload(manga: MangaRef, snapshot: EditingSnapshot) -> dict[str, Any]:
    live_folders = {folder.name: folder for folder in manga.folders}
    resume_folder = live_folders.get(snapshot.last_folder)
    if resume_folder is None or snapshot.last_image not in {
        image.name for image in resume_folder.images
    }:
        raise EditingStateError("The editing resume position is not in the live manga.")

    folder_payload: dict[str, Any] = {}
    for folder_name in sorted(snapshot.folders, key=natural_name_key):
        state = snapshot.folders[folder_name]
        folder = live_folders.get(folder_name)
        if folder is None:
            raise EditingStateError(
                f"Editing state contains a stale folder '{folder_name}'."
            )
        live_names = {image.name for image in folder.images}
        if not state.selected_images or not set(state.selected_images).issubset(
            live_names
        ):
            raise EditingStateError(
                f"Editing state for '{folder.name}' contains stale image names."
            )
        selected = [
            image.name
            for image in folder.images
            if image.name in state.selected_images
        ]
        folder_payload[folder.name] = {
            "selected_images": selected,
        }

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_folder": snapshot.last_folder,
        "last_image": snapshot.last_image,
        "folders": folder_payload,
    }


def _folder_for(manga: MangaRef, folder_name: str) -> FolderRef:
    if not isinstance(folder_name, str):
        raise StorageError("Folder name must be an exact string identity.")
    folder = _folder_by_name(manga, folder_name)
    if folder is not None:
        return folder
    raise StorageError(f"Folder '{folder_name}' is not in this manga.")


def _folder_by_name(manga: MangaRef, folder_name: str) -> FolderRef | None:
    return next(
        (folder for folder in manga.folders if folder.name == folder_name),
        None,
    )


def _image_for(folder: FolderRef, image_name: str):
    if not isinstance(image_name, str):
        raise StorageError("Image name must be an exact string identity.")
    image = next(
        (candidate for candidate in folder.images if candidate.name == image_name),
        None,
    )
    if image is None:
        raise StorageError(f"Image '{image_name}' is not in folder '{folder.name}'.")
    return image


def _first_folder(manga: MangaRef) -> FolderRef:
    if not manga.folders:
        raise StorageError(f"Manga '{manga.name}' contains no image folders.")
    return manga.folders[0]


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
