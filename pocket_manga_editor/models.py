"""Shared, GUI-independent models for manga discovery and review."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PageRef:
    """A source image and the chapter metadata encoded by its folders."""

    manga_name: str
    manga_path: Path
    volume_number: int
    chapter_number: Decimal
    chapter_label: str
    chapter_title: str
    page_number: int
    source_path: Path
    relative_path: str

    @property
    def output_filename(self) -> str:
        """Return an export name unique within a volume."""

        return f"C{self.chapter_label}_P{self.page_number:03d}.jpg"


@dataclass(frozen=True, slots=True)
class VolumeRef:
    """An ordered, virtual volume assembled from chapter directories."""

    manga_name: str
    manga_path: Path
    number: int
    pages: tuple[PageRef, ...]

    @property
    def display_name(self) -> str:
        return f"Vol.{self.number:02d}"


@dataclass(frozen=True, slots=True)
class MangaRef:
    """A discovered manga and all of its non-empty volumes."""

    name: str
    path: Path
    volumes: tuple[VolumeRef, ...]


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A non-fatal problem encountered while scanning source folders."""

    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    mangas: tuple[MangaRef, ...]
    issues: tuple[ScanIssue, ...]
