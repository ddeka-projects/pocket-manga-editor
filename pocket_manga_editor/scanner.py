"""Discover filesystem-faithful manga folders and image files."""

from __future__ import annotations

from pathlib import Path
import re
import stat

from .library_lock import LOCK_FILENAME
from .models import FolderRef, ImageRef, MangaRef, ScanIssue, ScanResult
from .path_safety import is_link_or_reparse


SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".png"})
_NATURAL_TOKEN = re.compile(r"(\d+)")


class ScanError(RuntimeError):
    """Raised when the selected working directory cannot be scanned."""


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


def scan_working_directory(working_directory: str | Path) -> ScanResult:
    """Scan direct manga/folder/image children without interpreting their names."""

    raw_root = Path(working_directory).expanduser()
    if is_link_or_reparse(raw_root):
        raise ScanError("The working directory cannot be a symbolic link or junction.")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise ScanError(f"Could not resolve working directory '{raw_root}': {exc}") from exc
    if not root.is_dir():
        raise ScanError(f"Working directory does not exist or is not a folder: {root}")

    try:
        children = sorted(root.iterdir(), key=lambda path: natural_name_key(path.name))
    except OSError as exc:
        raise ScanError(f"Could not read working directory '{root}': {exc}") from exc

    mangas: list[MangaRef] = []
    issues: list[ScanIssue] = []
    for candidate in children:
        if candidate.name.casefold() == ".pocket-manga-editor":
            continue
        if candidate.name.casefold() == LOCK_FILENAME.casefold():
            if candidate.is_dir():
                issues.append(
                    ScanIssue(
                        candidate,
                        f"Manga name '{candidate.name}' is reserved for the library mutation lock.",
                    )
                )
            continue
        kind = _direct_child_kind(root, candidate, issues, "manga")
        if kind != "directory":
            continue
        manga, manga_issues = _scan_manga(root, candidate)
        issues.extend(manga_issues)
        if manga is not None:
            mangas.append(manga)

    return ScanResult(tuple(mangas), tuple(issues))


def _scan_manga(root: Path, manga_path: Path) -> tuple[MangaRef | None, list[ScanIssue]]:
    issues: list[ScanIssue] = []
    try:
        candidates = sorted(
            manga_path.iterdir(), key=lambda path: natural_name_key(path.name)
        )
    except OSError as exc:
        return None, [ScanIssue(manga_path, f"Could not read manga folder: {exc}")]

    folders: list[FolderRef] = []
    for candidate in candidates:
        kind = _direct_child_kind(manga_path, candidate, issues, "image folder")
        if kind != "directory":
            continue
        folder = _scan_folder(root, manga_path, candidate, issues)
        if folder is not None:
            folders.append(folder)

    if not folders:
        return None, issues
    return MangaRef(manga_path.name, manga_path, tuple(folders)), issues


def _scan_folder(
    root: Path,
    manga_path: Path,
    folder_path: Path,
    issues: list[ScanIssue],
) -> FolderRef | None:
    try:
        candidates = sorted(
            folder_path.iterdir(), key=lambda path: natural_name_key(path.name)
        )
    except OSError as exc:
        issues.append(ScanIssue(folder_path, f"Could not read image folder: {exc}"))
        return None

    images: list[ImageRef] = []
    for candidate in candidates:
        if candidate.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        kind = _direct_child_kind(folder_path, candidate, issues, "image")
        if kind != "file":
            if kind == "directory":
                issues.append(
                    ScanIssue(candidate, "A supported image name is not a regular file.")
                )
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            issues.append(ScanIssue(candidate, f"Could not resolve image: {exc}"))
            continue
        if resolved.parent != folder_path or manga_path.parent != root:
            issues.append(ScanIssue(candidate, "Image path escapes its source folder."))
            continue
        images.append(ImageRef(candidate.name, candidate))

    if not images:
        return None
    return FolderRef(folder_path.name, folder_path, tuple(images))


def _direct_child_kind(
    parent: Path,
    candidate: Path,
    issues: list[ScanIssue],
    description: str,
) -> str | None:
    """Classify an immediate child without following symbolic links."""

    try:
        if is_link_or_reparse(candidate):
            issues.append(
                ScanIssue(
                    candidate,
                    f"Symbolic-link or junction {description} paths are not supported.",
                )
            )
            return None
        information = candidate.stat(follow_symlinks=False)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        issues.append(ScanIssue(candidate, f"Could not inspect {description}: {exc}"))
        return None
    if resolved.parent != parent:
        issues.append(ScanIssue(candidate, f"The {description} path escapes its parent."))
        return None
    if stat.S_ISDIR(information.st_mode):
        return "directory"
    if stat.S_ISREG(information.st_mode):
        return "file"
    return None
