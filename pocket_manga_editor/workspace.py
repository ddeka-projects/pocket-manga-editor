"""Safe path construction for isolated per-manga application workspaces."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .library_lock import LOCK_FILENAME
from .models import FolderRef, ImageRef, MangaRef
from .path_safety import is_link_or_reparse


METADATA_DIRECTORY_NAME = ".pocket-manga-editor"


class WorkspaceError(OSError):
    """Raised when source or managed workspace paths are unsafe."""


@dataclass(frozen=True, slots=True)
class MangaWorkspacePaths:
    root: Path
    metadata: Path
    workspace: Path
    reading: Path
    editing: Path
    output: Path
    completed: Path
    transactions: Path


def manga_workspace_paths(
    working_directory: str | Path, manga_name: str
) -> MangaWorkspacePaths:
    """Return non-creating paths after validating their shared workspace parents."""

    root = _resolve_root(working_directory)
    _validate_manga_name(manga_name)
    metadata = root / METADATA_DIRECTORY_NAME
    workspace = metadata / manga_name
    paths = MangaWorkspacePaths(
        root=root,
        metadata=metadata,
        workspace=workspace,
        reading=workspace / "reading.json",
        editing=workspace / "editing.json",
        output=workspace / "output",
        completed=workspace / "completed",
        transactions=workspace / ".transactions",
    )

    _validate_directory_if_present(metadata, root, "app metadata")
    _validate_directory_if_present(workspace, metadata, "manga workspace")
    return paths


def validate_reading_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate only the managed leaf needed by reading operations."""

    paths = _validate_workspace_identity(paths)
    _validate_file_if_present(paths.reading, paths.workspace, "reading metadata")
    return paths


def validate_editing_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate only the managed leaf needed by editing operations."""

    paths = _validate_workspace_identity(paths)
    _validate_file_if_present(paths.editing, paths.workspace, "editing metadata")
    return paths


def validate_export_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate the editing, output, and transaction leaves used by export."""

    paths = validate_transaction_workspace(paths)
    _validate_file_if_present(paths.editing, paths.workspace, "editing metadata")
    validate_output_workspace(paths)
    return paths


def validate_output_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate only the active output leaf used by output-opening operations."""

    paths = _validate_workspace_identity(paths)
    _validate_directory_if_present(paths.output, paths.workspace, "manga output")
    return paths


def validate_transaction_workspace(
    paths: MangaWorkspacePaths,
) -> MangaWorkspacePaths:
    """Validate only the transient transaction root used during recovery discovery."""

    paths = _validate_workspace_identity(paths)
    _validate_directory_if_present(
        paths.transactions, paths.workspace, "transaction workspace"
    )
    return paths


def validate_completed_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate only the immutable completion-batch root."""

    paths = _validate_workspace_identity(paths)
    _validate_directory_if_present(paths.completed, paths.workspace, "completed batches")
    return paths


def validate_completion_workspace(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    """Validate every managed leaf used by destructive completion operations."""

    paths = validate_export_workspace(paths)
    _validate_file_if_present(paths.reading, paths.workspace, "reading metadata")
    validate_completed_workspace(paths)
    return paths


def validate_live_manga(working_directory: str | Path, manga: MangaRef) -> Path:
    """Prove a model still names safe, direct source folders and images."""

    root, resolved_source = validate_live_manga_root(working_directory, manga)

    folder_names: set[str] = set()
    for folder in manga.folders:
        if folder.name in folder_names:
            raise WorkspaceError("The manga model contains a duplicate folder name.")
        folder_names.add(folder.name)
        _validate_component(folder.name, "folder")
        resolved_folder = _validate_live_folder(resolved_source, folder)
        if not folder.images:
            raise WorkspaceError(f"Source folder '{folder.name}' contains no images.")

        image_names: set[str] = set()
        for image in folder.images:
            if image.name in image_names:
                raise WorkspaceError(
                    f"Source folder '{folder.name}' contains a duplicate image name."
                )
            image_names.add(image.name)
            _validate_component(image.name, "image")
            if Path(image.name).suffix.casefold() not in {".jpg", ".png"}:
                raise WorkspaceError(f"Source image '{image.name}' is not JPG or PNG.")
            _validate_live_image(resolved_folder, folder, image)
    return resolved_source


def validate_live_manga_root(
    working_directory: str | Path, manga: MangaRef
) -> tuple[Path, Path]:
    """Validate only the library and manga roots for an ordinary state load."""

    root = _resolve_root(working_directory)
    manga_workspace_paths(root, manga.name)
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


def _validate_live_image(
    resolved_folder: Path, folder: FolderRef, image: ImageRef
) -> Path:
    _validate_component(image.name, "image")
    if Path(image.name).suffix.casefold() not in {".jpg", ".png"}:
        raise WorkspaceError(f"Source image '{image.name}' is not JPG or PNG.")
    image_path = Path(image.path)
    if is_link_or_reparse(image_path):
        raise WorkspaceError(
            f"Source image '{folder.name}/{image.name}' cannot be a link."
        )
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


def _validate_workspace_identity(paths: MangaWorkspacePaths) -> MangaWorkspacePaths:
    if not isinstance(paths, MangaWorkspacePaths):
        raise WorkspaceError("Managed workspace paths use an invalid value.")
    canonical = manga_workspace_paths(paths.root, paths.workspace.name)
    if canonical != paths:
        raise WorkspaceError("Managed workspace paths do not match their library root.")
    return canonical


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


def _validate_manga_name(name: str) -> None:
    _validate_component(name, "manga")
    if name.casefold() == LOCK_FILENAME.casefold():
        raise WorkspaceError(
            f"Manga name '{name}' is reserved for the library mutation lock."
        )


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
