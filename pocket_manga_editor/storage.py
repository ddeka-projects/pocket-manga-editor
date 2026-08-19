"""Atomic, per-manga reading and editing state persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Collection, Mapping

from .library_lock import library_mutation_lock
from .models import FolderRef, MangaRef
from .scanner import natural_name_key
from .workspace import (
    MangaWorkspacePaths,
    manga_workspace_paths,
    validate_editing_workspace,
    validate_live_manga,
    validate_reading_workspace,
)


STATE_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")


class StorageError(OSError):
    """Raised when application state cannot be handled safely."""


class EditingStateError(StorageError):
    """Raised when editing state cannot support safe destructive decisions."""


@dataclass(frozen=True, slots=True)
class ReadingFolderState:
    current_image: str


@dataclass(frozen=True, slots=True)
class ReadingSnapshot:
    last_folder: str
    folders: Mapping[str, ReadingFolderState]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EditingFolderState:
    current_image: str
    selected_images: frozenset[str]


@dataclass(frozen=True, slots=True)
class ExportedImageState:
    output_name: str
    digest: str


@dataclass(frozen=True, slots=True)
class FolderExportState:
    files: Mapping[str, ExportedImageState]


@dataclass(frozen=True, slots=True)
class EditingSnapshot:
    last_folder: str
    folders: Mapping[str, EditingFolderState]
    exports: Mapping[str, FolderExportState]
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _ReadingDocument:
    last_folder: str | None
    positions: dict[str, str]
    warnings: list[str]


class ReadingStore:
    """Persist phone-only reading bookmarks in one manga workspace."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        paths = manga_workspace_paths(self.working_directory, manga.name)
        return validate_reading_workspace(paths).reading

    def load(self, manga: MangaRef) -> ReadingSnapshot:
        validate_live_manga(self.working_directory, manga)
        paths = manga_workspace_paths(self.working_directory, manga.name)
        validate_reading_workspace(paths)
        document = _load_reading_document(paths.reading, manga)
        return _resolve_reading(manga, document)

    def set_position(
        self, manga: MangaRef, folder_name: str, image_name: str
    ) -> ReadingSnapshot:
        with library_mutation_lock(self.working_directory) as root:
            validate_live_manga(root, manga)
            folder = _folder_for(manga, folder_name)
            _image_name_for(folder, image_name)
            paths = _ensure_workspace(root, manga)
            validate_reading_workspace(paths)
            document = _load_reading_document(paths.reading, manga)
            document.last_folder = folder_name
            document.positions[folder_name] = image_name
            _write_reading(paths.reading, manga, document)
            return _resolve_reading(manga, document)


class EditingStore:
    """Persist shared desktop/mobile editing state and export bookkeeping."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)

    def path_for(self, manga: MangaRef) -> Path:
        paths = manga_workspace_paths(self.working_directory, manga.name)
        return validate_editing_workspace(paths).editing

    def load(self, manga: MangaRef) -> EditingSnapshot:
        root = validate_live_manga(self.working_directory, manga).parent
        return self._load_locked(root, manga)

    def set_position(
        self, manga: MangaRef, folder_name: str, image_name: str
    ) -> EditingSnapshot:
        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            folder = _folder_for(manga, folder_name)
            _image_name_for(folder, image_name)
            folders = dict(snapshot.folders)
            previous = folders[folder_name]
            folders[folder_name] = replace(previous, current_image=image_name)
            return replace(snapshot, last_folder=folder_name, folders=folders, warnings=())

        return self._mutate(manga, update)

    def set_selection(
        self,
        manga: MangaRef,
        folder_name: str,
        image_name: str,
        selected: bool,
    ) -> EditingSnapshot:
        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            folder = _folder_for(manga, folder_name)
            _image_name_for(folder, image_name)
            folders = dict(snapshot.folders)
            previous = folders[folder_name]
            selected_images = set(previous.selected_images)
            if selected:
                selected_images.add(image_name)
            else:
                selected_images.discard(image_name)
            folders[folder_name] = replace(
                previous, selected_images=frozenset(selected_images)
            )
            return replace(snapshot, folders=folders, warnings=())

        return self._mutate(manga, update)

    def replace_folder_selections(
        self,
        manga: MangaRef,
        folder_name: str,
        selected_images: Collection[str],
    ) -> EditingSnapshot:
        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            folder = _folder_for(manga, folder_name)
            validated = _selected_image_names(folder, selected_images)
            folders = dict(snapshot.folders)
            folders[folder_name] = replace(
                folders[folder_name], selected_images=validated
            )
            return replace(snapshot, folders=folders, warnings=())

        return self._mutate(manga, update)

    def save_folder(
        self,
        manga: MangaRef,
        folder_name: str,
        current_image: str,
        selected_images: Collection[str],
    ) -> EditingSnapshot:
        """Atomically merge one folder position and selections into latest state."""

        def update(snapshot: EditingSnapshot) -> EditingSnapshot:
            folder = _folder_for(manga, folder_name)
            _image_name_for(folder, current_image)
            validated = _selected_image_names(folder, selected_images)
            folders = dict(snapshot.folders)
            folders[folder_name] = EditingFolderState(current_image, validated)
            return replace(snapshot, last_folder=folder_name, folders=folders, warnings=())

        return self._mutate(manga, update)

    def _mutate(self, manga: MangaRef, operation) -> EditingSnapshot:
        with library_mutation_lock(self.working_directory) as root:
            validate_live_manga(root, manga)
            snapshot = self._load_locked(root, manga)
            updated = operation(snapshot)
            self._write_locked(root, manga, updated)
            return updated

    def _load_locked(self, root: Path, manga: MangaRef) -> EditingSnapshot:
        """Load strictly while the caller owns or does not require the mutation lock."""

        validate_live_manga(root, manga)
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

        validate_live_manga(root, manga)
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


def _load_reading_document(path: Path, manga: MangaRef) -> _ReadingDocument:
    if not path.exists():
        return _ReadingDocument(None, {}, [])
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _ReadingDocument(
            None, {}, [f"Reading metadata was invalid and was reset: {exc}"]
        )

    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "last_folder", "folders"}
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or not isinstance(payload.get("last_folder"), str)
        or not isinstance(payload.get("folders"), dict)
    ):
        return _ReadingDocument(
            None, {}, ["Reading metadata used an invalid format and was reset."]
        )

    live_folders = {folder.name: folder for folder in manga.folders}
    warnings: list[str] = []
    positions: dict[str, str] = {}
    for folder_name, value in payload["folders"].items():
        if not _safe_component(folder_name) or not isinstance(value, dict):
            warnings.append("Ignored an unsafe reading bookmark.")
            continue
        if set(value) != {"current_image"} or not isinstance(
            value.get("current_image"), str
        ):
            warnings.append(f"Ignored an invalid bookmark for '{folder_name}'.")
            continue
        folder = live_folders.get(folder_name)
        if folder is None:
            warnings.append(f"Ignored stale reading folder '{folder_name}'.")
            continue
        image_name = value["current_image"]
        if image_name not in {image.name for image in folder.images}:
            warnings.append(
                f"Ignored stale reading image '{folder_name}/{image_name}'."
            )
            continue
        positions[folder_name] = image_name

    last_folder = payload["last_folder"]
    if last_folder not in live_folders:
        warnings.append(f"Ignored stale last reading folder '{last_folder}'.")
        last_folder = None
    return _ReadingDocument(last_folder, positions, warnings)


def _resolve_reading(manga: MangaRef, document: _ReadingDocument) -> ReadingSnapshot:
    first_folder = _first_folder(manga)
    folders = {
        folder.name: ReadingFolderState(
            document.positions.get(folder.name, folder.images[0].name)
        )
        for folder in manga.folders
    }
    return ReadingSnapshot(
        document.last_folder or first_folder.name,
        folders,
        tuple(document.warnings),
    )


def _write_reading(path: Path, manga: MangaRef, document: _ReadingDocument) -> None:
    live_folders = {folder.name: folder for folder in manga.folders}
    positions: dict[str, str] = {}
    for folder in manga.folders:
        image_name = document.positions.get(folder.name)
        if image_name is not None and image_name in {
            image.name for image in folder.images
        }:
            positions[folder.name] = image_name
    last_folder = (
        document.last_folder
        if document.last_folder in live_folders
        else _first_folder(manga).name
    )
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_folder": last_folder,
        "folders": {
            folder_name: {"current_image": positions[folder_name]}
            for folder_name in sorted(positions, key=natural_name_key)
        },
    }
    atomic_write_json(path, payload)


def _default_editing(manga: MangaRef) -> EditingSnapshot:
    first_folder = _first_folder(manga)
    return EditingSnapshot(
        first_folder.name,
        {
            folder.name: EditingFolderState(folder.images[0].name, frozenset())
            for folder in manga.folders
        },
        {},
    )


def _parse_editing(payload: object, manga: MangaRef) -> EditingSnapshot:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "last_folder", "folders", "exports"}
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or not isinstance(payload.get("last_folder"), str)
        or not isinstance(payload.get("folders"), dict)
        or not isinstance(payload.get("exports"), dict)
    ):
        raise EditingStateError("Editing metadata uses an invalid or unsupported format.")

    live_folders = {folder.name: folder for folder in manga.folders}
    folder_states: dict[str, EditingFolderState] = {
        folder.name: EditingFolderState(folder.images[0].name, frozenset())
        for folder in manga.folders
    }
    last_folder = payload["last_folder"]
    if not _safe_component(last_folder):
        raise EditingStateError("Editing metadata contains an unsafe last folder.")

    warnings: list[str] = []
    for folder_name, value in payload["folders"].items():
        if not _safe_component(folder_name):
            raise EditingStateError("Editing metadata contains an unsafe folder entry.")
        if (
            not isinstance(value, dict)
            or set(value) != {"current_image", "selected_images"}
            or not isinstance(value.get("current_image"), str)
            or not isinstance(value.get("selected_images"), list)
        ):
            raise EditingStateError(
                f"Editing metadata contains an invalid folder entry for '{folder_name}'."
            )
        current_image = value["current_image"]
        selected_values = value["selected_images"]
        if not _safe_image_name(current_image) or any(
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
        if current_image not in live_names:
            warnings.append(
                f"Ignored stale editing image '{folder_name}/{current_image}'."
            )
            current_image = folder.images[0].name
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
        folder_states[folder_name] = EditingFolderState(
            current_image, frozenset(selected)
        )

    exports: dict[str, FolderExportState] = {}
    for folder_name, value in payload["exports"].items():
        if not _safe_component(folder_name):
            raise EditingStateError("Editing metadata contains an unsafe export folder.")
        if not isinstance(value, dict) or set(value) != {"files"} or not isinstance(
            value.get("files"), dict
        ):
            raise EditingStateError(
                f"Editing metadata contains an invalid export for '{folder_name}'."
            )
        files: dict[str, ExportedImageState] = {}
        for image_name, entry in value["files"].items():
            if (
                not _safe_component(image_name)
                or Path(image_name).suffix.casefold() not in {".jpg", ".png"}
                or not isinstance(entry, dict)
                or set(entry) != {"output_name", "digest"}
                or not _safe_component(entry.get("output_name"))
                or entry.get("output_name")
                != _managed_output_name(folder_name, image_name)
                or not isinstance(entry.get("digest"), str)
                or not _DIGEST.fullmatch(entry["digest"])
            ):
                raise EditingStateError(
                    f"Editing metadata contains an invalid exported image in '{folder_name}'."
                )
            files[image_name] = ExportedImageState(
                entry["output_name"], entry["digest"]
            )
        if files:
            exports[folder_name] = FolderExportState(files)

    if last_folder not in live_folders:
        warnings.append(f"Ignored stale last editing folder '{last_folder}'.")
        last_folder = _first_folder(manga).name
    return EditingSnapshot(
        last_folder, folder_states, exports, tuple(warnings)
    )


def _editing_payload(manga: MangaRef, snapshot: EditingSnapshot) -> dict[str, Any]:
    live_folders = {folder.name: folder for folder in manga.folders}
    if snapshot.last_folder not in live_folders:
        raise EditingStateError("The last editing folder is not in the live manga.")

    folder_payload: dict[str, Any] = {}
    for folder in manga.folders:
        state = snapshot.folders.get(folder.name)
        if state is None:
            state = EditingFolderState(folder.images[0].name, frozenset())
        live_names = {image.name for image in folder.images}
        if state.current_image not in live_names or not set(
            state.selected_images
        ).issubset(live_names):
            raise EditingStateError(
                f"Editing state for '{folder.name}' contains stale image names."
            )
        selected = [
            image.name
            for image in folder.images
            if image.name in state.selected_images
        ]
        folder_payload[folder.name] = {
            "current_image": state.current_image,
            "selected_images": selected,
        }

    exports_payload: dict[str, Any] = {}
    for folder_name in sorted(snapshot.exports, key=natural_name_key):
        if not _safe_component(folder_name):
            raise EditingStateError("Editing state contains an unsafe export folder.")
        export = snapshot.exports[folder_name]
        files_payload: dict[str, Any] = {}
        for image_name in sorted(export.files, key=natural_name_key):
            entry = export.files[image_name]
            if (
                not _safe_component(image_name)
                or Path(image_name).suffix.casefold() not in {".jpg", ".png"}
                or not _safe_component(entry.output_name)
                or entry.output_name
                != _managed_output_name(folder_name, image_name)
                or not _DIGEST.fullmatch(entry.digest)
            ):
                raise EditingStateError("Editing state contains an invalid export entry.")
            files_payload[image_name] = {
                "output_name": entry.output_name,
                "digest": entry.digest,
            }
        if files_payload:
            exports_payload[folder_name] = {"files": files_payload}

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_folder": snapshot.last_folder,
        "folders": folder_payload,
        "exports": exports_payload,
    }


def _folder_for(manga: MangaRef, folder_name: str) -> FolderRef:
    if not isinstance(folder_name, str):
        raise StorageError("Folder name must be an exact string identity.")
    for folder in manga.folders:
        if folder.name == folder_name:
            return folder
    raise StorageError(f"Folder '{folder_name}' is not in this manga.")


def _image_name_for(folder: FolderRef, image_name: str) -> str:
    if not isinstance(image_name, str):
        raise StorageError("Image name must be an exact string identity.")
    if image_name not in {image.name for image in folder.images}:
        raise StorageError(f"Image '{image_name}' is not in folder '{folder.name}'.")
    return image_name


def _selected_image_names(
    folder: FolderRef, selected_images: Collection[str]
) -> frozenset[str]:
    try:
        selected = set(selected_images)
    except TypeError as exc:
        raise StorageError("Selected images must be exact filename strings.") from exc
    if any(not isinstance(value, str) for value in selected):
        raise StorageError("Selected images must be exact filename strings.")
    live_names = {image.name for image in folder.images}
    unknown = sorted(selected - live_names, key=natural_name_key)
    if unknown:
        raise StorageError(
            f"Selection contains images that are not in '{folder.name}': "
            + ", ".join(unknown)
        )
    return frozenset(selected)


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


def _managed_output_name(folder_name: str, image_name: str) -> str:
    """Return the sole authoritative filename for one managed export entry."""

    return f"{folder_name}__{image_name}"


def _safe_image_name(value: object) -> bool:
    return _safe_component(value) and Path(value).suffix.casefold() in {
        ".jpg",
        ".png",
    }
