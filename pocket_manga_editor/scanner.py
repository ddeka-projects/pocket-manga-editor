"""Discover manga, chapters, volumes, and supported image pages from a working folder."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import re

from .models import MangaRef, PageRef, ScanIssue, ScanResult, VolumeRef


CHAPTER_PATTERN = re.compile(
    r"^Vol\.\s+(?P<volume>\d+(?:\.\d+)?)\s+Ch\.\s+"
    r"(?P<chapter>\d+(?:\.\d+)?)(?:\s+-\s+(?P<title>.+?))?\s*$",
    re.IGNORECASE,
)
VOLUME_PATTERN = re.compile(
    r"^Vol\.\s+(?P<volume>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
PAGE_PATTERN = re.compile(
    r"^(?P<page>\d{3}(?:\.\d+)?)\.(?:jpg|png)$",
    re.IGNORECASE,
)


class ScanError(RuntimeError):
    """Raised when the selected working directory cannot be scanned."""


def scan_working_directory(working_directory: str | Path) -> ScanResult:
    """Scan immediate manga/chapter/page children in deterministic order.

    The expected hierarchy is::

        working directory/
          Manga name/
            Vol. 01 Ch. 001 - Optional chapter title/
              001.jpg
              002.png
            Vol. 02.5/
              001.jpg

    Non-matching folders and files are ignored. Names that appear intended to
    follow the chapter convention, but do not, are returned as scan issues.
    """

    root = Path(working_directory).expanduser().resolve()
    if not root.is_dir():
        raise ScanError(f"Working directory does not exist or is not a folder: {root}")

    try:
        manga_directories = sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir()
                and child.name.casefold() != ".pocket-manga-editor"
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise ScanError(f"Could not read working directory '{root}': {exc}") from exc

    mangas: list[MangaRef] = []
    issues: list[ScanIssue] = []

    for manga_path in manga_directories:
        manga, manga_issues = _scan_manga(manga_path)
        issues.extend(manga_issues)
        if manga is not None:
            mangas.append(manga)

    return ScanResult(tuple(mangas), tuple(issues))


def _scan_manga(manga_path: Path) -> tuple[MangaRef | None, list[ScanIssue]]:
    issues: list[ScanIssue] = []

    try:
        child_directories = sorted(
            (child for child in manga_path.iterdir() if child.is_dir()),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        return None, [ScanIssue(manga_path, f"Could not read manga folder: {exc}")]

    chapter_groups: dict[
        tuple[Decimal, Decimal], list[tuple[Path, re.Match[str]]]
    ] = defaultdict(list)
    direct_volume_groups: dict[Decimal, list[tuple[Path, re.Match[str]]]] = defaultdict(
        list
    )

    for source_path in child_directories:
        chapter_match = CHAPTER_PATTERN.fullmatch(source_path.name)
        volume_match = VOLUME_PATTERN.fullmatch(source_path.name)
        if chapter_match:
            key = (
                Decimal(chapter_match.group("volume")),
                Decimal(chapter_match.group("chapter")),
            )
            chapter_groups[key].append((source_path, chapter_match))
        elif volume_match:
            direct_volume_groups[Decimal(volume_match.group("volume"))].append(
                (source_path, volume_match)
            )
        elif source_path.name.casefold().startswith("vol."):
            issues.append(
                ScanIssue(
                    source_path,
                    "Folder does not match either 'Vol. <digits or decimal>' or "
                    "'Vol. <digits or decimal> Ch. <digits or decimal>' with an "
                    "optional ' - Chapter name' suffix.",
                )
            )

    chapter_volume_numbers = {volume for volume, _chapter in chapter_groups}
    conflicting_volumes = chapter_volume_numbers & direct_volume_groups.keys()
    for volume_number in sorted(conflicting_volumes):
        source_names = [
            path.name
            for (candidate_volume, _chapter), matches in chapter_groups.items()
            if candidate_volume == volume_number
            for path, _match in matches
        ]
        source_names.extend(
            path.name for path, _match in direct_volume_groups[volume_number]
        )
        issues.append(
            ScanIssue(
                manga_path,
                f"Volume {_decimal_text(volume_number)} exists as both chapter "
                "folders and a direct volume folder, so it was skipped: "
                + ", ".join(source_names),
            )
        )

    pages_by_volume: dict[Decimal, list[PageRef]] = defaultdict(list)
    labels_by_volume: dict[Decimal, str] = {}

    for (volume_number, chapter_number), matches in sorted(chapter_groups.items()):
        if volume_number in conflicting_volumes:
            continue
        if len(matches) > 1:
            joined_names = ", ".join(path.name for path, _ in matches)
            volume_label = matches[0][1].group("volume")
            chapter_label = matches[0][1].group("chapter")
            issues.append(
                ScanIssue(
                    manga_path,
                    f"Duplicate Vol. {volume_label} Ch. {chapter_label} "
                    f"folders were skipped: {joined_names}",
                )
            )
            continue

        chapter_path, match = matches[0]
        volume_label = match.group("volume")
        chapter_label = match.group("chapter")
        chapter_title = (match.group("title") or "").strip()
        labels_by_volume.setdefault(volume_number, volume_label)

        for page_number, page_label, source_path in _scan_pages(chapter_path, issues):
            pages_by_volume[volume_number].append(
                PageRef(
                    manga_name=manga_path.name,
                    manga_path=manga_path,
                    volume_number=volume_number,
                    volume_label=volume_label,
                    chapter_number=chapter_number,
                    chapter_label=chapter_label,
                    chapter_title=chapter_title,
                    page_number=page_number,
                    page_label=page_label,
                    source_path=source_path,
                    relative_path=source_path.relative_to(manga_path).as_posix(),
                )
            )

    for volume_number, matches in sorted(direct_volume_groups.items()):
        if volume_number in conflicting_volumes:
            continue
        if len(matches) > 1:
            joined_names = ", ".join(path.name for path, _match in matches)
            issues.append(
                ScanIssue(
                    manga_path,
                    f"Duplicate Vol. {matches[0][1].group('volume')} folders were "
                    f"skipped: {joined_names}",
                )
            )
            continue

        volume_path, match = matches[0]
        volume_label = match.group("volume")
        labels_by_volume[volume_number] = volume_label
        for page_number, page_label, source_path in _scan_pages(volume_path, issues):
            pages_by_volume[volume_number].append(
                PageRef(
                    manga_name=manga_path.name,
                    manga_path=manga_path,
                    volume_number=volume_number,
                    volume_label=volume_label,
                    chapter_number=None,
                    chapter_label="",
                    chapter_title="",
                    page_number=page_number,
                    page_label=page_label,
                    source_path=source_path,
                    relative_path=source_path.relative_to(manga_path).as_posix(),
                )
            )

    volumes: list[VolumeRef] = []
    for volume_number, pages in sorted(pages_by_volume.items()):
        pages.sort(
            key=lambda page: (
                page.chapter_number if page.chapter_number is not None else Decimal(-1),
                page.page_number,
                page.relative_path.casefold(),
            )
        )
        if pages:
            volumes.append(
                VolumeRef(
                    manga_name=manga_path.name,
                    manga_path=manga_path,
                    number=volume_number,
                    label=labels_by_volume[volume_number],
                    pages=tuple(pages),
                )
            )

    if not volumes:
        return None, issues

    return MangaRef(manga_path.name, manga_path, tuple(volumes)), issues


def _scan_pages(
    folder: Path, issues: list[ScanIssue]
) -> list[tuple[Decimal, str, Path]]:
    """Return valid, numerically ordered pages from one source folder."""

    try:
        page_files = sorted(
            (child for child in folder.iterdir() if child.is_file()),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        issues.append(ScanIssue(folder, f"Could not read image folder: {exc}"))
        return []

    page_groups: dict[Decimal, list[tuple[str, Path]]] = defaultdict(list)
    for page_path in page_files:
        page_match = PAGE_PATTERN.fullmatch(page_path.name)
        if page_match:
            page_label = page_match.group("page")
            page_groups[Decimal(page_label)].append((page_label, page_path))
        elif page_path.suffix.casefold() in {".jpg", ".jpeg", ".png"}:
            issues.append(
                ScanIssue(
                    page_path,
                        "Image file does not match a supported '###.jpg', "
                        "'###.png', or decimal-page equivalent.",
                )
            )

    pages: list[tuple[Decimal, str, Path]] = []
    for page_number, matching_files in sorted(page_groups.items()):
        if len(matching_files) > 1:
            joined_names = ", ".join(path.name for _label, path in matching_files)
            issues.append(
                ScanIssue(
                    folder,
                    f"Duplicate page {_decimal_text(page_number)} was skipped: "
                    f"{joined_names}",
                )
            )
            continue
        page_label, page_path = matching_files[0]
        pages.append((page_number, page_label, page_path))

    if not pages:
        issues.append(
            ScanIssue(
                folder,
                "Folder contains no matching ###.jpg, ###.png, or decimal pages.",
            )
        )
    return pages


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
