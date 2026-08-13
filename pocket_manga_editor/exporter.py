"""Copy an explicit selection to a managed per-volume output folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .models import PageRef, VolumeRef
from .storage import atomic_write_json


EXPORT_SCHEMA_VERSION = 1
OUTPUT_FILENAME_PATTERN = re.compile(
    r"^C(?P<chapter>\d+(?:\.\d+)?)_P(?P<page>\d{3})\.(?:jpg|png)$"
)


class ExportError(RuntimeError):
    """Raised when selected pages cannot be exported safely."""


class ExportConflict(ExportError):
    """Raised rather than overwriting a file the app does not own."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_directory: Path
    copied_count: int
    removed_count: int


def output_directory_for(volume: VolumeRef) -> Path:
    return volume.manga_path.resolve() / "Output" / f"Vol.{volume.number:02d}"


def export_selected_pages(
    working_directory: str | Path,
    volume: VolumeRef,
    selected_paths: set[str] | frozenset[str],
) -> ExportResult:
    """Export the selected snapshot without touching source images.

    A manifest records files created by this app. On a later export, stale
    app-managed files are removed, but unrelated files in the output folder are
    preserved. An untracked filename collision aborts instead of overwriting.
    """

    working_path = Path(working_directory).expanduser().resolve()
    manga_path = volume.manga_path.resolve()
    if manga_path.parent != working_path:
        raise ExportError("The selected manga is not directly inside the working directory.")
    if volume.manga_name != manga_path.name:
        raise ExportError("The selected manga metadata does not match its folder name.")

    known_pages = {page.relative_path: page for page in volume.pages}
    unknown = sorted(set(selected_paths) - known_pages.keys())
    if unknown:
        raise ExportError("The selection contains pages that are no longer in this volume.")

    pages = [page for page in volume.pages if page.relative_path in selected_paths]
    if not pages:
        raise ExportError("Select at least one page before exporting.")

    desired: dict[str, PageRef] = {}
    for page in pages:
        if page.output_filename in desired:
            raise ExportError(f"Two selected pages would use '{page.output_filename}'.")
        if not page.source_path.is_file():
            raise ExportError(f"Source page no longer exists: {page.relative_path}")
        if not page.source_path.resolve().is_relative_to(manga_path):
            raise ExportError(f"Source page is outside the manga folder: {page.relative_path}")
        desired[page.output_filename] = page

    metadata_directory = working_path / ".pocket-manga-editor"
    manifest_path = (
        metadata_directory
        / "exports"
        / volume.manga_name
        / f"Vol.{volume.number:02d}.json"
    )
    for metadata_path in (
        metadata_directory,
        metadata_directory / "exports",
        manifest_path.parent,
    ):
        if metadata_path.exists() and not metadata_path.resolve().is_relative_to(working_path):
            raise ExportError("The app metadata folder points outside the working directory.")
    previous_files = _load_manifest(manifest_path, volume)
    output_directory = output_directory_for(volume)
    if output_directory.exists() and output_directory.is_symlink():
        raise ExportError("The output volume folder cannot be a symbolic link.")
    if output_directory.parent.exists() and output_directory.parent.is_symlink():
        raise ExportError("The manga Output folder cannot be a symbolic link.")
    for output_path in (output_directory.parent, output_directory):
        if output_path.exists() and not output_path.resolve().is_relative_to(manga_path):
            raise ExportError("The output folder points outside the selected manga folder.")
    try:
        output_directory.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Could not create the output location: {exc}") from exc

    for filename, entry in previous_files.items():
        target = output_directory / filename
        if target.exists():
            try:
                changed = (
                    target.is_symlink()
                    or not target.is_file()
                    or _sha256(target) != entry["sha256"]
                )
            except OSError as exc:
                raise ExportError(f"Could not verify previous export '{target}': {exc}") from exc
            if changed:
                raise ExportConflict(
                    f"'{target}' has changed since Pocket Manga Editor exported it. "
                    "It will not be overwritten or removed. Move or rename the edited "
                    "file, then export again."
                )

    adopted_existing: set[str] = set()
    for filename in desired:
        target = output_directory / filename
        if target.exists() and filename not in previous_files:
            # This can be the residue of an interrupted export whose manifest
            # could not be committed. Adopt it only when it is byte-identical
            # to the currently selected source; never overwrite unknown data.
            try:
                identical = (
                    not target.is_symlink()
                    and target.is_file()
                    and _sha256(target) == _sha256(desired[filename].source_path)
                )
            except OSError as exc:
                raise ExportError(f"Could not verify existing output '{target}': {exc}") from exc
            if not identical:
                raise ExportConflict(
                    f"'{target}' already exists but was not created by Pocket Manga Editor. "
                    "Move or rename it, then export again."
                )
            adopted_existing.add(filename)

    try:
        staging_directory = Path(
            tempfile.mkdtemp(prefix=".pme-export-", dir=output_directory.parent)
        )
    except OSError as exc:
        raise ExportError(f"Could not create a temporary export folder: {exc}") from exc
    new_directory = staging_directory / "new"
    backup_directory = staging_directory / "backup"
    installed_targets: list[Path] = []
    backups: dict[Path, Path] = {}
    phase = "prepare selected pages"
    stale_filenames = set(previous_files) - set(desired)
    removed_count = sum(
        1 for filename in stale_filenames if (output_directory / filename).is_file()
    )

    try:
        new_directory.mkdir()
        backup_directory.mkdir()
        for filename, page in desired.items():
            shutil.copy2(page.source_path, new_directory / filename)

        fingerprints = {
            filename: _sha256(new_directory / filename) for filename in desired
        }
        payload = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "manga": volume.manga_name,
            "volume": volume.number,
            "files": {
                filename: {
                    "source": page.relative_path,
                    "sha256": fingerprints[filename],
                }
                for filename, page in sorted(desired.items())
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        phase = "update the output folder"
        output_directory.mkdir(parents=True, exist_ok=True)
        for filename in sorted(set(desired) | stale_filenames):
            if filename in adopted_existing:
                continue
            target = output_directory / filename
            if target.exists():
                backup = backup_directory / filename
                os.replace(target, backup)
                backups[target] = backup
            if filename in desired:
                os.replace(new_directory / filename, target)
                installed_targets.append(target)

        phase = "save the export manifest"
        atomic_write_json(manifest_path, payload)
    except BaseException as exc:
        rollback_errors = _rollback_export(installed_targets, backups)
        if isinstance(exc, OSError):
            detail = f"Could not {phase}: {exc}"
            if rollback_errors:
                detail += " The output rollback was incomplete: " + "; ".join(
                    rollback_errors
                )
            raise ExportError(detail) from exc
        raise
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)

    return ExportResult(output_directory, len(desired), removed_count)


def _load_manifest(manifest_path: Path, volume: VolumeRef) -> dict[str, dict[str, str]]:
    if not manifest_path.exists():
        return {}

    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"The previous export manifest could not be read safely: {exc}"
        ) from exc

    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != EXPORT_SCHEMA_VERSION
        or payload.get("manga") != volume.manga_name
        or payload.get("volume") != volume.number
        or not isinstance(payload.get("files"), dict)
    ):
        raise ExportError("The previous export manifest is invalid or belongs to another volume.")

    files: dict[str, dict[str, str]] = {}
    for filename, entry in payload["files"].items():
        filename_match = (
            OUTPUT_FILENAME_PATTERN.fullmatch(filename)
            if isinstance(filename, str)
            else None
        )
        if not filename_match or not isinstance(entry, dict):
            raise ExportError("The previous export manifest contains an unsafe file entry.")
        source = entry.get("source")
        fingerprint = entry.get("sha256")
        if not isinstance(source, str) or not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ):
            raise ExportError("The previous export manifest contains an invalid file entry.")

        matching_page = next(
            (page for page in volume.pages if page.relative_path == source), None
        )
        if matching_page is None or matching_page.output_filename != filename:
            raise ExportError("The previous export manifest does not match this volume.")
        files[filename] = {"source": source, "sha256": fingerprint}
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rollback_export(
    installed_targets: list[Path], backups: dict[Path, Path]
) -> list[str]:
    """Best-effort restoration of the output state preceding an export."""

    errors: list[str] = []
    for target in reversed(installed_targets):
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"could not remove '{target}': {exc}")

    for target, backup in reversed(tuple(backups.items())):
        try:
            os.replace(backup, target)
        except OSError as exc:
            errors.append(f"could not restore '{target}': {exc}")
    return errors
