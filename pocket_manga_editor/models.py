"""Filesystem-faithful, GUI-independent manga library models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ImageRef:
    """One supported image directly inside an image folder."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class FolderRef:
    """One exact, user-named source folder containing supported images."""

    name: str
    path: Path
    images: tuple[ImageRef, ...]


@dataclass(frozen=True, slots=True)
class MangaRef:
    """One manga source directory and its image-bearing child folders."""

    name: str
    path: Path
    folders: tuple[FolderRef, ...]


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A non-fatal problem encountered while scanning source folders."""

    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    mangas: tuple[MangaRef, ...]
    issues: tuple[ScanIssue, ...]
