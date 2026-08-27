"""Immutable, path-free identifiers for one live library snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from types import MappingProxyType
from typing import Callable, Mapping

from ..models import FolderRef, ImageRef, MangaRef, ScanResult
from ..path_safety import is_link_or_reparse


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
    folder_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FolderSnapshotEntry:
    id: str
    manga_id: str
    ref: FolderRef
    image_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImageSnapshotEntry:
    id: str
    folder_id: str
    ref: ImageRef


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """A complete scanner snapshot whose public IDs reveal no filesystem data."""

    working_directory: Path
    snapshot_id: str
    issue_count: int
    mangas: tuple[MangaSnapshotEntry, ...]
    _manga_by_id: Mapping[str, MangaSnapshotEntry]
    _folder_by_id: Mapping[str, FolderSnapshotEntry]
    _image_by_id: Mapping[str, ImageSnapshotEntry]

    @classmethod
    def build(
        cls,
        working_directory: str | Path,
        scan_result: ScanResult,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> "LibrarySnapshot":
        requested_root = Path(working_directory).expanduser()
        if is_link_or_reparse(requested_root):
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
        folder_map: dict[str, FolderSnapshotEntry] = {}
        image_map: dict[str, ImageSnapshotEntry] = {}

        for manga in scan_result.mangas:
            manga_path = Path(manga.path)
            if is_link_or_reparse(manga_path) or not manga_path.is_dir():
                raise SnapshotError(f"Source manga no longer exists safely: {manga.name}")
            try:
                resolved_manga = manga_path.resolve(strict=True)
            except OSError as exc:
                raise SnapshotError(f"Could not resolve source manga '{manga.name}'.") from exc
            if resolved_manga.parent != root or resolved_manga.name != manga.name:
                raise SnapshotError("A scanned manga is outside the working directory.")
            if not manga.folders:
                raise SnapshotError("A scanned manga has no readable image folders.")

            manga_id = opaque_id("m")
            folder_ids: list[str] = []
            for folder in manga.folders:
                folder_path = Path(folder.path)
                if folder_path.name != folder.name:
                    raise SnapshotError("A scanned folder does not belong to its manga.")
                resolved_folder = _validate_folder_path(
                    root, resolved_manga, folder_path, folder.name
                )
                if not folder.images:
                    raise SnapshotError("A scanned folder has no readable images.")

                folder_id = opaque_id("f")
                folder_ids.append(folder_id)
                image_ids: list[str] = []
                for image in folder.images:
                    if Path(image.path).parent != folder_path:
                        raise SnapshotError("A scanned image does not belong to its folder.")
                    _validate_image_path(
                        root, resolved_manga, resolved_folder, image
                    )
                    image_id = opaque_id("i")
                    image_ids.append(image_id)
                    image_map[image_id] = ImageSnapshotEntry(
                        image_id, folder_id, image
                    )

                folder_map[folder_id] = FolderSnapshotEntry(
                    folder_id, manga_id, folder, tuple(image_ids)
                )

            manga_entry = MangaSnapshotEntry(manga_id, manga, tuple(folder_ids))
            manga_entries.append(manga_entry)
            manga_map[manga_id] = manga_entry

        return cls(
            root,
            opaque_id("s"),
            len(scan_result.issues),
            tuple(manga_entries),
            MappingProxyType(manga_map),
            MappingProxyType(folder_map),
            MappingProxyType(image_map),
        )

    def manga(self, manga_id: str) -> MangaSnapshotEntry:
        try:
            return self._manga_by_id[manga_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError(
                "This manga ID is not in the active snapshot."
            ) from exc

    def folder(self, folder_id: str) -> FolderSnapshotEntry:
        try:
            return self._folder_by_id[folder_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError(
                "This folder ID is not in the active snapshot."
            ) from exc

    def image(self, image_id: str) -> ImageSnapshotEntry:
        try:
            return self._image_by_id[image_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError(
                "This image ID is not in the active snapshot."
            ) from exc

    def image_in_folder(
        self, folder_id: str, image_id: str
    ) -> ImageSnapshotEntry:
        folder = self.folder(folder_id)
        image = self.image(image_id)
        if image.folder_id != folder.id:
            raise SnapshotLookupError(
                "This image does not belong to the requested folder."
            )
        return image

    def validate_live_image(self, image_id: str) -> ImageSnapshotEntry:
        image = self.image(image_id)
        folder = self.folder(image.folder_id)
        manga = self.manga(folder.manga_id)
        try:
            manga_path = Path(manga.ref.path)
            if is_link_or_reparse(manga_path):
                raise SnapshotError("A source manga uses a linked path.")
            resolved_manga = manga_path.resolve(strict=True)
            resolved_folder = _validate_folder_path(
                self.working_directory,
                resolved_manga,
                Path(folder.ref.path),
                folder.ref.name,
            )
            _validate_image_path(
                self.working_directory,
                resolved_manga,
                resolved_folder,
                image.ref,
            )
        except (OSError, SnapshotError) as exc:
            raise MissingImageError(str(exc)) from exc
        return image


def _validate_folder_path(
    root: Path,
    resolved_manga: Path,
    folder_path: Path,
    folder_name: str,
) -> Path:
    if is_link_or_reparse(folder_path):
        raise SnapshotError("A source image folder uses a symlinked path.")
    try:
        resolved_folder = folder_path.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("A source image folder is no longer available.") from exc
    if (
        resolved_manga.parent != root
        or not resolved_manga.is_dir()
        or resolved_folder.parent != resolved_manga
        or resolved_folder.name != folder_name
        or not resolved_folder.is_dir()
    ):
        raise SnapshotError("A source image folder is not safely located.")
    return resolved_folder


def _validate_image_path(
    root: Path,
    resolved_manga: Path,
    resolved_folder: Path,
    image: ImageRef,
) -> None:
    source = Path(image.path)
    if is_link_or_reparse(source):
        raise SnapshotError("A source image uses a symlinked path.")
    if (
        resolved_manga.parent != root
        or resolved_folder.parent != resolved_manga
        or source.parent != resolved_folder
        or source.name != image.name
    ):
        raise SnapshotError("A source image has inconsistent folder metadata.")
    try:
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("A source image is no longer available.") from exc
    if (
        resolved_source.parent != resolved_folder
        or not resolved_source.is_file()
        or resolved_source.suffix.casefold() not in {".jpg", ".png"}
    ):
        raise SnapshotError("A source image is not a safe JPG or PNG.")
