"""Discover manga, chapters, volumes, and JPG pages from a working folder."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from .models import MangaRef, PageRef, ScanIssue, ScanResult, VolumeRef


CHAPTER_PATTERN = re.compile(
    r"^Vol\.(?P<volume>\d{2})\s+Ch\.(?P<chapter>\d{3})\s+-\s+(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
PAGE_PATTERN = re.compile(r"^(?P<page>\d{3})\.jpg$", re.IGNORECASE)


class ScanError(RuntimeError):
    """Raised when the selected working directory cannot be scanned."""


def scan_working_directory(working_directory: str | Path) -> ScanResult:
    """Scan immediate manga/chapter/page children in deterministic order.

    The expected hierarchy is::

        working directory/
          Manga name/
            Vol.01 Ch.001 - Chapter title/
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

    chapter_groups: dict[tuple[int, int], list[tuple[Path, re.Match[str]]]] = defaultdict(list)
    for chapter_path in child_directories:
        match = CHAPTER_PATTERN.fullmatch(chapter_path.name)
        if match:
            key = (int(match.group("volume")), int(match.group("chapter")))
            chapter_groups[key].append((chapter_path, match))
        elif chapter_path.name.casefold().startswith("vol."):
            issues.append(
                ScanIssue(
                    chapter_path,
                    "Chapter folder does not match 'Vol.## Ch.### - Chapter name'.",
                )
            )

    pages_by_volume: dict[int, list[PageRef]] = defaultdict(list)

    for (volume_number, chapter_number), matches in sorted(chapter_groups.items()):
        if len(matches) > 1:
            joined_names = ", ".join(path.name for path, _ in matches)
            issues.append(
                ScanIssue(
                    manga_path,
                    f"Duplicate Vol.{volume_number:02d} Ch.{chapter_number:03d} "
                    f"folders were skipped: {joined_names}",
                )
            )
            continue

        chapter_path, match = matches[0]
        chapter_title = match.group("title").strip()

        try:
            page_files = sorted(
                (child for child in chapter_path.iterdir() if child.is_file()),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError as exc:
            issues.append(ScanIssue(chapter_path, f"Could not read chapter folder: {exc}"))
            continue

        page_groups: dict[int, list[Path]] = defaultdict(list)
        for page_path in page_files:
            page_match = PAGE_PATTERN.fullmatch(page_path.name)
            if page_match:
                page_groups[int(page_match.group("page"))].append(page_path)
            elif page_path.suffix.casefold() in {".jpg", ".jpeg"}:
                issues.append(
                    ScanIssue(
                        page_path,
                        "Image file does not match the supported '###.jpg' page name.",
                    )
                )

        chapter_page_count = 0
        for page_number, matching_files in sorted(page_groups.items()):
            if len(matching_files) > 1:
                joined_names = ", ".join(path.name for path in matching_files)
                issues.append(
                    ScanIssue(
                        chapter_path,
                        f"Duplicate page {page_number:03d} was skipped: {joined_names}",
                    )
                )
                continue

            source_path = matching_files[0]
            pages_by_volume[volume_number].append(
                PageRef(
                    manga_name=manga_path.name,
                    manga_path=manga_path,
                    volume_number=volume_number,
                    chapter_number=chapter_number,
                    chapter_title=chapter_title,
                    page_number=page_number,
                    source_path=source_path,
                    relative_path=source_path.relative_to(manga_path).as_posix(),
                )
            )
            chapter_page_count += 1

        if chapter_page_count == 0:
            issues.append(ScanIssue(chapter_path, "Chapter contains no matching ###.jpg pages."))

    volumes: list[VolumeRef] = []
    for volume_number, pages in sorted(pages_by_volume.items()):
        pages.sort(
            key=lambda page: (
                page.chapter_number,
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
                    pages=tuple(pages),
                )
            )

    if not volumes:
        return None, issues

    return MangaRef(manga_path.name, manga_path, tuple(volumes)), issues
