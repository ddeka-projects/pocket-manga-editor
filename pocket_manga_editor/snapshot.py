"""Immutable, path-free identifiers for the library list and the open manga."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from types import MappingProxyType
from typing import Mapping

from .models import FolderRef, ImageRef, LibraryScan, MangaRef
from .workspace import validate_live_manga_item


class SnapshotError(RuntimeError):
    code = "invalid_snapshot"


class SnapshotLookupError(SnapshotError):
    code = "stale_snapshot"


class MissingImageError(SnapshotError):
    code = "missing_image"


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


@dataclass(frozen=True, slots=True)
class MangaEntry:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    """The manga names found by the last library scan, under opaque IDs."""

    working_directory: Path
    snapshot_id: str
    issue_count: int
    mangas: tuple[MangaEntry, ...]
    _manga_by_id: Mapping[str, MangaEntry]

    @classmethod
    def build(cls, scan: LibraryScan) -> "LibraryIndex":
        mangas = tuple(MangaEntry(_opaque_id("m"), name) for name in scan.manga_names)
        return cls(
            scan.root,
            _opaque_id("s"),
            len(scan.issues),
            mangas,
            MappingProxyType({manga.id: manga for manga in mangas}),
        )

    def manga(self, manga_id: str) -> MangaEntry:
        try:
            return self._manga_by_id[manga_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This manga ID is not in the active library.") from exc


@dataclass(frozen=True, slots=True)
class FolderEntry:
    id: str
    ref: FolderRef
    image_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImageEntry:
    id: str
    folder_id: str
    ref: ImageRef


@dataclass(frozen=True, slots=True)
class MangaSnapshot:
    """One opened manga's entries and images; a new scan issues new IDs."""

    working_directory: Path
    id: str
    ref: MangaRef
    folder_ids: tuple[str, ...]
    _folder_by_id: Mapping[str, FolderEntry]
    _image_by_id: Mapping[str, ImageEntry]

    @classmethod
    def build(cls, working_directory: Path, manga_id: str, ref: MangaRef) -> "MangaSnapshot":
        folders: dict[str, FolderEntry] = {}
        images: dict[str, ImageEntry] = {}
        for folder in ref.folders:
            folder_id = _opaque_id("f")
            image_ids: list[str] = []
            for image in folder.images:
                image_id = _opaque_id("i")
                image_ids.append(image_id)
                images[image_id] = ImageEntry(image_id, folder_id, image)
            folders[folder_id] = FolderEntry(folder_id, folder, tuple(image_ids))
        return cls(
            working_directory,
            manga_id,
            ref,
            tuple(folders),
            MappingProxyType(folders),
            MappingProxyType(images),
        )

    @property
    def folders(self) -> tuple[FolderEntry, ...]:
        return tuple(self._folder_by_id[folder_id] for folder_id in self.folder_ids)

    def folder(self, folder_id: str) -> FolderEntry:
        try:
            return self._folder_by_id[folder_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This folder ID is not in the open manga.") from exc

    def image(self, image_id: str) -> ImageEntry:
        try:
            return self._image_by_id[image_id]
        except (KeyError, TypeError) as exc:
            raise SnapshotLookupError("This image ID is not in the open manga.") from exc

    def image_in_folder(self, folder_id: str, image_id: str) -> ImageEntry:
        folder = self.folder(folder_id)
        image = self.image(image_id)
        if image.folder_id != folder.id:
            raise SnapshotLookupError("This image does not belong to the requested folder.")
        return image

    def validate_live_image(self, image_id: str) -> ImageEntry:
        """Confirm the image is still a plain file in its folder before use."""

        image = self.image(image_id)
        folder = self.folder(image.folder_id)
        try:
            validate_live_manga_item(self.working_directory, self.ref, folder.ref, image.ref)
        except OSError as exc:
            raise MissingImageError(str(exc)) from exc
        return image
