"""Immutable, path-free identifiers for one Companion Mode session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from types import MappingProxyType
from typing import Callable, Mapping

from ..models import MangaRef, PageRef, ScanResult, VolumeRef


class SnapshotError(RuntimeError):
    code = "invalid_snapshot"


class SnapshotLookupError(SnapshotError):
    code = "stale_snapshot"


class MissingImageError(SnapshotError):
    code = "missing_image"


@dataclass(frozen=True, slots=True)
class MangaSnapshotEntry:
    id: str
    ref: MangaRef
    volume_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VolumeSnapshotEntry:
    id: str
    manga_id: str
    ref: VolumeRef
    page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageSnapshotEntry:
    id: str
    volume_id: str
    ref: PageRef


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """A complete scanner snapshot whose public IDs reveal no filesystem data."""

    working_directory: Path
    snapshot_id: str
    issue_count: int
    mangas: tuple[MangaSnapshotEntry, ...]
    _manga_by_id: Mapping[str, MangaSnapshotEntry]
    _volume_by_id: Mapping[str, VolumeSnapshotEntry]
    _page_by_id: Mapping[str, PageSnapshotEntry]

    @classmethod
    def build(
        cls,
        working_directory: str | Path,
        scan_result: ScanResult,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> "LibrarySnapshot":
        requested_root = Path(working_directory).expanduser()
        if requested_root.is_symlink():
            raise SnapshotError("The working directory is not a safe directory.")
        root = requested_root.resolve(strict=True)
        if not root.is_dir():
            raise SnapshotError("The working directory is not a safe directory.")
        make_id = id_factory or (lambda: secrets.token_urlsafe(18))
        used: set[str] = set()

        def opaque_id(prefix: str) -> str:
            for _attempt in range(100):
                value = f"{prefix}_{make_id()}"
                if (
                    value not in used
                    and value.isascii()
                    and value
                    and all(character.isalnum() or character in "_-" for character in value)
                ):
                    used.add(value)
                    return value
            raise SnapshotError("Could not allocate a unique snapshot identifier.")

        manga_entries: list[MangaSnapshotEntry] = []
        manga_map: dict[str, MangaSnapshotEntry] = {}
        volume_map: dict[str, VolumeSnapshotEntry] = {}
        page_map: dict[str, PageSnapshotEntry] = {}

        for manga in scan_result.mangas:
            manga_path = Path(manga.path)
            if manga_path.is_symlink() or not manga_path.is_dir():
                raise SnapshotError(f"Source manga no longer exists safely: {manga.name}")
            try:
                resolved_manga = manga_path.resolve(strict=True)
            except OSError as exc:
                raise SnapshotError(f"Could not resolve source manga '{manga.name}'.") from exc
            if resolved_manga.parent != root or resolved_manga.name != manga.name:
                raise SnapshotError("A scanned manga is outside the working directory.")

            manga_id = opaque_id("m")
            volume_ids: list[str] = []
            if not manga.volumes:
                raise SnapshotError("A scanned manga has no readable volumes.")
            for volume in manga.volumes:
                if volume.manga_name != manga.name or Path(volume.manga_path) != manga_path:
                    raise SnapshotError("A scanned volume does not belong to its manga.")
                volume_id = opaque_id("v")
                volume_ids.append(volume_id)
                page_ids: list[str] = []
                if not volume.pages:
                    raise SnapshotError("A scanned volume has no readable pages.")
                for page in volume.pages:
                    if (
                        page.manga_name != manga.name
                        or Path(page.manga_path) != manga_path
                        or page.volume_number != volume.number
                    ):
                        raise SnapshotError("A scanned page does not belong to its volume.")
                    _validate_page_path(root, resolved_manga, page)
                    page_id = opaque_id("p")
                    page_ids.append(page_id)
                    page_map[page_id] = PageSnapshotEntry(page_id, volume_id, page)
                volume_map[volume_id] = VolumeSnapshotEntry(
                    volume_id, manga_id, volume, tuple(page_ids)
                )
            manga_entry = MangaSnapshotEntry(manga_id, manga, tuple(volume_ids))
            manga_entries.append(manga_entry)
            manga_map[manga_id] = manga_entry

        return cls(
            root,
            opaque_id("s"),
            len(scan_result.issues),
            tuple(manga_entries),
            MappingProxyType(manga_map),
            MappingProxyType(volume_map),
            MappingProxyType(page_map),
        )

    def manga(self, manga_id: str) -> MangaSnapshotEntry:
        try:
            return self._manga_by_id[manga_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This manga ID is not in the active snapshot.") from exc

    def volume(self, volume_id: str) -> VolumeSnapshotEntry:
        try:
            return self._volume_by_id[volume_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This volume ID is not in the active snapshot.") from exc

    def page(self, page_id: str) -> PageSnapshotEntry:
        try:
            return self._page_by_id[page_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This page ID is not in the active snapshot.") from exc

    def page_in_volume(self, volume_id: str, page_id: str) -> PageSnapshotEntry:
        volume = self.volume(volume_id)
        page = self.page(page_id)
        if page.volume_id != volume.id:
            raise SnapshotLookupError("This page does not belong to the requested volume.")
        return page

    def validate_live_page(self, page_id: str) -> PageSnapshotEntry:
        page = self.page(page_id)
        manga = Path(page.ref.manga_path)
        try:
            resolved_manga = manga.resolve(strict=True)
        except OSError as exc:
            raise SnapshotLookupError("The source manga is no longer available.") from exc
        try:
            _validate_page_path(self.working_directory, resolved_manga, page.ref)
        except SnapshotError as exc:
            raise MissingImageError(str(exc)) from exc
        return page


def _validate_page_path(root: Path, resolved_manga: Path, page: PageRef) -> None:
    manga = Path(page.manga_path)
    source = Path(page.source_path)
    if manga.is_symlink() or resolved_manga.parent != root:
        raise SnapshotError("A source manga is no longer safely located.")
    try:
        relative = source.relative_to(manga)
    except ValueError as exc:
        raise SnapshotError("A source page is outside its manga.") from exc
    if page.relative_path != relative.as_posix():
        raise SnapshotError("A source page has inconsistent relative metadata.")
    candidate = manga
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise SnapshotError("A source page uses a symlinked path.")
    try:
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("A source page is no longer available.") from exc
    if (
        not resolved_source.is_relative_to(resolved_manga)
        or not resolved_source.is_file()
        or resolved_source.suffix.casefold() not in {".jpg", ".png"}
    ):
        raise SnapshotError("A source page is not a safe JPG or PNG.")
