"""Safe paths for the flat per-manga metadata files and live source checks."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .models import FolderRef, ImageRef, MangaRef
from .path_safety import is_link_or_reparse


METADATA_DIRECTORY_NAME = ".pocket-manga-editor"
READING_SUFFIX = ".read.json"
EDITING_SUFFIX = ".edit.json"


class WorkspaceError(OSError):
    """Raised when source or managed metadata paths are unsafe."""


@dataclass(frozen=True, slots=True)
class MangaMetadataPaths:
    root: Path
    metadata: Path
    reading: Path
    editing: Path


def metadata_directory(working_directory: str | Path) -> Path:
    """Return the non-creating app metadata folder after validating it if present."""

    root = _resolve_root(working_directory)
    metadata = root / METADATA_DIRECTORY_NAME
    _validate_directory_if_present(metadata, root, "app metadata")
    return metadata


def manga_metadata_paths(working_directory: str | Path, manga_name: str) -> MangaMetadataPaths:
    """Return non-creating metadata file paths after validating their shared folder."""

    _validate_component(manga_name, "manga")
    metadata = metadata_directory(working_directory)
    root = metadata.parent
    return MangaMetadataPaths(
        root=root,
        metadata=metadata,
        reading=metadata / f"{manga_name}{READING_SUFFIX}",
        editing=metadata / f"{manga_name}{EDITING_SUFFIX}",
    )


def validate_reading_file(paths: MangaMetadataPaths) -> Path:
    _validate_file_if_present(paths.reading, paths.metadata, "reading metadata")
    return paths.reading


def validate_editing_file(paths: MangaMetadataPaths) -> Path:
    _validate_file_if_present(paths.editing, paths.metadata, "editing metadata")
    return paths.editing


def validate_live_manga_root(working_directory: str | Path, manga: MangaRef) -> tuple[Path, Path]:
    """Validate only the library and manga roots for an ordinary state load."""

    root = _resolve_root(working_directory)
    _validate_component(manga.name, "manga")
    source = Path(manga.path)
    if is_link_or_reparse(source):
        raise WorkspaceError("The source manga cannot be a symbolic link or junction.")
    try:
        information = source.stat(follow_symlinks=False)
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"The source manga could not be inspected: {exc}") from exc
    if not stat.S_ISDIR(information.st_mode):
        raise WorkspaceError("The source manga is not a directory.")
    if resolved_source.parent != root or resolved_source.name != manga.name:
        raise WorkspaceError(
            "The source manga is not an exact direct child of the working directory."
        )
    return root, resolved_source


def validate_live_manga_item(
    working_directory: str | Path,
    manga: MangaRef,
    folder: FolderRef,
    image: ImageRef | None = None,
) -> Path:
    """Validate one live folder or image without walking the whole manga."""

    _root, resolved_source = validate_live_manga_root(working_directory, manga)
    if not any(candidate is folder for candidate in manga.folders):
        raise WorkspaceError("The source folder is not part of the manga snapshot.")
    resolved_folder = _validate_live_folder(resolved_source, folder)
    if image is None:
        return resolved_folder
    if not any(candidate is image for candidate in folder.images):
        raise WorkspaceError("The source image is not part of its folder snapshot.")
    return _validate_live_image(resolved_folder, folder, image)


def _validate_live_folder(resolved_source: Path, folder: FolderRef) -> Path:
    _validate_component(folder.name, "folder")
    folder_path = Path(folder.path)
    if is_link_or_reparse(folder_path):
        raise WorkspaceError(
            f"Source folder '{folder.name}' cannot be a symbolic link or junction."
        )
    try:
        information = folder_path.stat(follow_symlinks=False)
        resolved_folder = folder_path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(
            f"Source folder '{folder.name}' could not be inspected: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(information.st_mode)
        or resolved_folder.parent != resolved_source
        or resolved_folder.name != folder.name
    ):
        raise WorkspaceError(
            f"Source folder '{folder.name}' is not an exact direct child of its manga."
        )
    return resolved_folder


def _validate_live_image(resolved_folder: Path, folder: FolderRef, image: ImageRef) -> Path:
    _validate_component(image.name, "image")
    if Path(image.name).suffix.casefold() not in {".jpg", ".png"}:
        raise WorkspaceError(f"Source image '{image.name}' is not JPG or PNG.")
    image_path = Path(image.path)
    if is_link_or_reparse(image_path):
        raise WorkspaceError(f"Source image '{folder.name}/{image.name}' cannot be a link.")
    try:
        information = image_path.stat(follow_symlinks=False)
        resolved_image = image_path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(
            f"Source image '{folder.name}/{image.name}' could not be inspected: {exc}"
        ) from exc
    if (
        not stat.S_ISREG(information.st_mode)
        or resolved_image.parent != resolved_folder
        or resolved_image.name != image.name
    ):
        raise WorkspaceError(
            f"Source image '{folder.name}/{image.name}' is not a safe direct child."
        )
    return resolved_image


def _resolve_root(working_directory: str | Path) -> Path:
    raw_root = Path(working_directory).expanduser()
    if is_link_or_reparse(raw_root):
        raise WorkspaceError("The working directory cannot be a symbolic link or junction.")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"The working directory could not be resolved: {exc}") from exc
    if not root.is_dir():
        raise WorkspaceError(f"The working directory is not a folder: {root}")
    return root


def _validate_component(name: str, description: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
        or "\x00" in name
        or Path(name).name != name
    ):
        raise WorkspaceError(f"The {description} name is not a safe path component.")


def _validate_directory_if_present(path: Path, parent: Path, description: str) -> None:
    if not os.path.lexists(path):
        return
    if is_link_or_reparse(path):
        raise WorkspaceError(f"The {description} directory cannot be a link.")
    try:
        information = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"The {description} directory is unreadable: {exc}") from exc
    if not stat.S_ISDIR(information.st_mode) or resolved.parent != parent:
        raise WorkspaceError(f"The {description} path is not a safe directory.")


def _validate_file_if_present(path: Path, parent: Path, description: str) -> None:
    if not os.path.lexists(path):
        return
    if is_link_or_reparse(path):
        raise WorkspaceError(f"The {description} file cannot be a link.")
    try:
        information = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"The {description} file is unreadable: {exc}") from exc
    if not stat.S_ISREG(information.st_mode) or resolved.parent != parent:
        raise WorkspaceError(f"The {description} path is not a safe regular file.")
