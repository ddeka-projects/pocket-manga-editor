"""Discover manga folders, and one manga's entries and images when it is opened."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .models import FolderRef, ImageRef, LibraryScan, MangaRef, MangaScan, ScanIssue
from .path_safety import is_link_or_reparse
from .workspace import METADATA_DIRECTORY_NAME


SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".png"})
_NATURAL_TOKEN = re.compile(r"(\d+)")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN_OR_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(
    stat, "FILE_ATTRIBUTE_SYSTEM", 0x4
)


class ScanError(RuntimeError):
    """Raised when the working directory or a manga folder cannot be scanned."""


class MangaMissingError(ScanError):
    """Raised when a listed manga folder is no longer a plain directory."""


def natural_name_key(value: str) -> tuple[tuple[tuple[int, object], ...], str]:
    """Return a deterministic, case-insensitive natural ordering key."""

    tokens: list[tuple[int, object]] = []
    for token in _NATURAL_TOKEN.split(value):
        if not token:
            continue
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token.casefold()))
    return tuple(tokens), value


def resolve_library_root(working_directory: str | Path) -> Path:
    raw_root = Path(working_directory).expanduser()
    if is_link_or_reparse(raw_root):
        raise ScanError("The working directory cannot be a symbolic link or junction.")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise ScanError(f"Could not resolve working directory '{raw_root}': {exc}") from exc
    if not root.is_dir():
        raise ScanError(f"Working directory does not exist or is not a folder: {root}")
    return root


def scan_library(working_directory: str | Path) -> LibraryScan:
    """List manga folder names only; their contents are scanned when opened."""

    root = resolve_library_root(working_directory)
    try:
        entries = _sorted_entries(root)
    except OSError as exc:
        raise ScanError(f"Could not read working directory '{root}': {exc}") from exc

    names: list[str] = []
    issues: list[ScanIssue] = []
    for entry in entries:
        if entry.name.casefold() == METADATA_DIRECTORY_NAME or _is_hidden(entry):
            continue
        kind = _entry_kind(entry)
        if kind == "link":
            issues.append(
                ScanIssue(
                    root / entry.name,
                    "Symbolic-link or junction manga folders are not supported.",
                )
            )
        elif kind == "directory":
            names.append(entry.name)
    return LibraryScan(root, tuple(names), tuple(issues))


def scan_manga(working_directory: str | Path, manga_name: str) -> MangaScan:
    """Scan one manga's direct entry folders and their direct JPG/PNG images."""

    root = resolve_library_root(working_directory)
    if not manga_name or manga_name in {".", ".."} or Path(manga_name).name != manga_name:
        raise ScanError("The manga name is not a safe folder name.")
    manga_path = root / manga_name
    if is_link_or_reparse(manga_path) or not manga_path.is_dir():
        raise MangaMissingError(f"Manga folder '{manga_name}' is no longer available.")
    try:
        entries = _sorted_entries(manga_path)
    except OSError as exc:
        raise ScanError(f"Could not read manga folder '{manga_name}': {exc}") from exc

    folders: list[FolderRef] = []
    issues: list[ScanIssue] = []
    entry_folder_count = 0
    for entry in entries:
        kind = _entry_kind(entry)
        if kind == "link":
            issues.append(
                ScanIssue(
                    manga_path / entry.name,
                    "Symbolic-link or junction entry folders are not supported.",
                )
            )
        elif kind == "directory":
            entry_folder_count += 1
            folder = _scan_folder(manga_path / entry.name, issues)
            if folder is not None:
                folders.append(folder)
    return MangaScan(
        MangaRef(manga_name, manga_path, tuple(folders)),
        entry_folder_count,
        tuple(issues),
    )


def _scan_folder(folder_path: Path, issues: list[ScanIssue]) -> FolderRef | None:
    try:
        entries = _sorted_entries(folder_path)
    except OSError as exc:
        issues.append(ScanIssue(folder_path, f"Could not read entry folder: {exc}"))
        return None

    images: list[ImageRef] = []
    for entry in entries:
        if Path(entry.name).suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        kind = _entry_kind(entry)
        if kind == "file":
            images.append(ImageRef(entry.name, folder_path / entry.name))
        elif kind == "link":
            issues.append(
                ScanIssue(folder_path / entry.name, "Symbolic-link images are not supported.")
            )
        elif kind == "directory":
            issues.append(
                ScanIssue(folder_path / entry.name, "A supported image name is not a file.")
            )
    if not images:
        return None
    return FolderRef(folder_path.name, folder_path, tuple(images))


def _sorted_entries(path: Path) -> list[os.DirEntry]:
    with os.scandir(path) as iterator:
        return sorted(iterator, key=lambda entry: natural_name_key(entry.name))


def _entry_kind(entry: os.DirEntry) -> str | None:
    """Classify a listed entry without following links or opening it.

    The directory listing already carries this information (on Windows,
    including the reparse-point attribute), so a large folder costs one
    listing rather than several filesystem calls per image.
    """

    try:
        if entry.is_symlink() or _attributes(entry) & _REPARSE_POINT:
            return "link"
        if entry.is_dir(follow_symlinks=False):
            return "directory"
        if entry.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        return None
    return None


def _is_hidden(entry: os.DirEntry) -> bool:
    """Skip Windows hidden or system folders such as $RECYCLE.BIN."""

    try:
        return bool(_attributes(entry) & _HIDDEN_OR_SYSTEM)
    except OSError:
        return False


def _attributes(entry: os.DirEntry) -> int:
    if os.name != "nt":
        return 0
    return getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
