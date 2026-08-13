"""Shared, GUI-independent models for manga discovery and review."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PageRef:
    """A source image and the volume/chapter metadata encoded by its folder."""

    manga_name: str
    manga_path: Path
    volume_number: Decimal
    volume_label: str
    chapter_number: Decimal | None
    chapter_label: str
    chapter_title: str
    page_number: Decimal
    page_label: str
    source_path: Path
    relative_path: str

    @property
    def output_filename(self) -> str:
        """Return an export name unique within a volume."""

        extension = self.source_path.suffix.casefold()
        if self.chapter_number is None:
            return f"P{self.page_label}{extension}"
        return f"C{self.chapter_label}_P{self.page_label}{extension}"


@dataclass(frozen=True, slots=True)
class VolumeRef:
    """An ordered volume sourced from chapters or a direct volume folder."""

    manga_name: str
    manga_path: Path
    number: Decimal
    label: str
    pages: tuple[PageRef, ...]

    @property
    def display_name(self) -> str:
        return f"Vol. {self.label}"

    @property
    def storage_name(self) -> str:
        """Keep generated paths compatible with the original Vol.XX layout."""

        return f"Vol.{self.label}"

    @property
    def identity(self) -> str:
        """Return a formatting-independent serialized volume identifier."""

        return format(self.number.normalize(), "f")


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
