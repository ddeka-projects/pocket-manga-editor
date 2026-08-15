"""Finalize one manga batch and retain an append-only completion history.

Completion is intentionally separate from the Qt interface.  The preview API
does all read-only validation needed by confirmation dialogs, while
``complete_manga`` verifies that the preview is still current before moving or
deleting anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any
import uuid

from . import __version__
from .library_lock import LibraryBusyError, LibraryLockError, library_mutation_lock
from .models import MangaRef
from .scanner import CHAPTER_PATTERN, VOLUME_PATTERN
from .storage import atomic_write_json


COMPLETION_LOG_SCHEMA_VERSION = 1
COMPLETION_LOG_FILENAME = "completion-log.json"
_TRANSACTION_MARKER_FILENAME = "transaction.json"
_TRANSACTION_SCHEMA_VERSION = 1
_STAGING_PREFIX = ".pme-completion-"
_OUTPUT_VOLUME_PATTERN = re.compile(r"^Vol\.(?P<volume>\d+(?:\.\d+)?)$")
_IMAGE_SUFFIXES = frozenset({".jpg", ".png"})


class CompletionError(RuntimeError):
    """Raised when a manga cannot be completed safely."""


class CompletionChangedError(CompletionError):
    """Raised when the filesystem changed after the user reviewed a preview."""


class CompletionBusyError(CompletionError):
    """Raised when another window is currently mutating this library."""


class CompletionRecoveryError(CompletionError):
    """Raised when a failed pre-commit transaction could not be fully restored."""

    state_may_have_changed = True


@dataclass(frozen=True, slots=True)
class OutputVolumeSummary:
    """Images currently present in one output volume folder."""

    name: str
    image_files: tuple[str, ...]

    @property
    def image_count(self) -> int:
        return len(self.image_files)


@dataclass(frozen=True, slots=True)
class PriorCompletionSummary:
    """Small history item suitable for a completion confirmation dialog."""

    completed_at: str
    source_volumes: tuple[str, ...]
    output_volumes: tuple[str, ...]
    image_count: int


@dataclass(frozen=True, slots=True)
class CompletionPreview:
    """Validated, read-only description of a proposed completion."""

    manga_name: str
    source_directory: Path
    output_directory: Path
    completed_directory: Path
    source_volumes: tuple[str, ...]
    source_folders: tuple[str, ...]
    output_volumes: tuple[OutputVolumeSummary, ...]
    missing_volumes: tuple[str, ...]
    unexpected_volumes: tuple[str, ...]
    previous_completions: tuple[PriorCompletionSummary, ...]
    snapshot_token: str

    @property
    def total_image_count(self) -> int:
        return sum(volume.image_count for volume in self.output_volumes)

    @property
    def has_volume_mismatch(self) -> bool:
        return bool(self.missing_volumes or self.unexpected_volumes)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Paths and counts produced by a successful completion."""

    completed_directory: Path
    log_path: Path
    completed_at: str
    output_volumes: tuple[OutputVolumeSummary, ...]
    total_image_count: int
    cleanup_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletionRecoveryResult:
    """Summary of interrupted completion transactions repaired at startup."""

    rolled_back_count: int
    cleaned_count: int
    warnings: tuple[str, ...] = ()

    @property
    def recovered_count(self) -> int:
        return self.rolled_back_count + self.cleaned_count


@dataclass(frozen=True, slots=True)
class _CompletionPaths:
    root: Path
    metadata: Path
    source: Path
    output: Path
    selections: Path
    exports: Path
    completed_root: Path
    completed: Path
    log: Path


def analyze_completion(
    working_directory: str | Path, manga: MangaRef
) -> CompletionPreview:
    """Validate and describe a manga completion without changing any files."""

    paths = _completion_paths(working_directory, manga)
    source_tree, source_volumes, source_folders = _validate_source(paths, manga)
    output_tree, output_volumes = _validate_output(paths)
    _validate_optional_managed_tree(paths.selections, "selection metadata")
    _validate_optional_managed_tree(paths.exports, "export metadata")

    log_payload, log_bytes = _load_log(paths.log)
    previous = tuple(
        _prior_summary(entry)
        for entry in log_payload["completions"]
        if entry["manga"] == manga.name
    )

    missing, unexpected = _compare_volumes(source_volumes, output_volumes)
    total_images = sum(volume.image_count for volume in output_volumes)
    if total_images == 0:
        raise CompletionError(
            f"'{paths.output}' contains no exported JPG or PNG pages. "
            "Export at least one page before completing this manga."
        )

    token_payload = {
        "manga": manga.name,
        "source": source_tree,
        "source_volumes": source_volumes,
        "source_folders": source_folders,
        "output": output_tree,
        "output_volumes": [
            (volume.name, volume.image_files) for volume in output_volumes
        ],
        "missing": missing,
        "unexpected": unexpected,
        "log": hashlib.sha256(log_bytes).hexdigest() if log_bytes is not None else None,
    }
    snapshot_token = hashlib.sha256(
        json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return CompletionPreview(
        manga_name=manga.name,
        source_directory=paths.source,
        output_directory=paths.output,
        completed_directory=paths.completed,
        source_volumes=source_volumes,
        source_folders=source_folders,
        output_volumes=output_volumes,
        missing_volumes=missing,
        unexpected_volumes=unexpected,
        previous_completions=previous,
        snapshot_token=snapshot_token,
    )


def recover_interrupted_completions(
    working_directory: str | Path,
) -> CompletionRecoveryResult:
    """Recover durable completion transactions left by a crash or forced exit.

    Recovery is safe to call before every library scan.  It serializes against
    active completions, rolls uncommitted transactions back, and finishes
    cleanup for transactions whose IDs are already present in the log.
    """

    root = _resolve_working_directory(working_directory)
    metadata = root / ".pocket-manga-editor"
    _validate_managed_parent(metadata, root, "app metadata folder")
    if not _lexists(metadata):
        return CompletionRecoveryResult(0, 0)

    try:
        with library_mutation_lock(root):
            return _recover_interrupted_completions_locked(root, metadata)
    except LibraryBusyError as exc:
        raise CompletionBusyError(str(exc)) from exc
    except LibraryLockError as exc:
        raise CompletionError(str(exc)) from exc


def complete_manga(
    working_directory: str | Path,
    manga: MangaRef,
    expected_preview: CompletionPreview,
    *,
    allow_volume_mismatch: bool = False,
) -> CompletionResult:
    """Finalize a manga after revalidating a previously displayed preview.

    The source, output, selections, and export manifests are first renamed into
    a same-filesystem staging directory.  Until the atomic log update succeeds,
    every rename can be reversed.  Once the history entry is committed, the
    staged source and metadata are permanently removed.
    """

    paths = _completion_paths(working_directory, manga)
    try:
        with library_mutation_lock(paths.root):
            return _complete_manga_locked(
                paths.root,
                manga,
                expected_preview,
                allow_volume_mismatch=allow_volume_mismatch,
            )
    except LibraryBusyError as exc:
        raise CompletionBusyError(str(exc)) from exc
    except LibraryLockError as exc:
        raise CompletionError(str(exc)) from exc


def _complete_manga_locked(
    working_directory: Path,
    manga: MangaRef,
    expected_preview: CompletionPreview,
    *,
    allow_volume_mismatch: bool,
) -> CompletionResult:
    """Complete a manga while the process-wide completion lock is held."""

    current = analyze_completion(working_directory, manga)
    if not _same_preview_identity(expected_preview, current):
        raise CompletionChangedError(
            "The manga source, output, or completion history changed after the "
            "confirmation was opened. Review the completion details again."
        )
    if current.has_volume_mismatch and not allow_volume_mismatch:
        raise CompletionError(
            "The source and output volumes do not match. Explicit confirmation "
            "is required to complete this manga anyway."
        )

    paths = _completion_paths(working_directory, manga)
    log_payload, _log_bytes = _load_log(paths.log)

    completed_at = datetime.now(timezone.utc).isoformat()
    transaction_id = str(uuid.uuid4())
    new_entry = _completion_entry(current, completed_at, transaction_id)
    updated_log = {
        "schema_version": COMPLETION_LOG_SCHEMA_VERSION,
        "completions": [*log_payload["completions"], new_entry],
    }

    try:
        paths.metadata.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=paths.metadata)
        )
    except OSError as exc:
        raise CompletionError(f"Could not create completion staging: {exc}") from exc

    moved: list[tuple[Path, Path]] = []
    installed = False
    committed = False
    rollback_errors: list[str] = []
    commit_warnings: list[str] = []
    phase = "save the completion recovery marker"
    preserve_staging = False

    try:
        marker = {
            "schema_version": _TRANSACTION_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "manga": manga.name,
            "created_at": completed_at,
            "app_version": __version__,
            "present": {
                "source": _lexists(paths.source),
                "output": _lexists(paths.output),
                "selections": _lexists(paths.selections),
                "exports": _lexists(paths.exports),
            },
        }
        atomic_write_json(staging / _TRANSACTION_MARKER_FILENAME, marker)
        _fsync_directory(staging)
        _fsync_directory(paths.metadata)

        phase = "stage the manga files"
        for original, stage_name in (
            (paths.output, "output"),
            (paths.selections, "selections"),
            (paths.exports, "exports"),
            # Move the source last so an abrupt exit early in staging leaves the
            # manga discoverable until startup recovery runs.
            (paths.source, "source"),
        ):
            if _lexists(original):
                staged_path = staging / stage_name
                os.replace(original, staged_path)
                moved.append((original, staged_path))
                _fsync_directory(original.parent)
                _fsync_directory(staging)

        phase = "install the completed output"
        staged_output = staging / "output"
        paths.completed_root.mkdir(parents=True, exist_ok=True)
        os.replace(staged_output, paths.completed)
        installed = True
        _fsync_directory(staging)
        _fsync_directory(paths.completed_root)

        phase = "save the completion history"
        atomic_write_json(paths.log, updated_log)
        # The visible atomic log replace is the logical commit point. From here
        # onward no exception or interrupt may roll source/output back without
        # also removing that durable transaction entry.
        committed = True
        try:
            _fsync_directory(paths.log.parent)
        except OSError as exc:
            # The atomic replace is already visible and must not be rolled back
            # independently of its transaction ID. Report reduced durability as
            # a committed cleanup warning instead.
            commit_warnings.append(
                f"Could not sync the completion history directory: {exc}"
            )
    except BaseException as exc:
        if not committed:
            rollback_errors = _rollback_completion(paths, moved, installed)
            preserve_staging = bool(rollback_errors)
        if isinstance(exc, Exception):
            detail = f"Could not {phase}: {exc}"
            if rollback_errors:
                detail += " Rollback was incomplete: " + "; ".join(rollback_errors)
                detail += f". Recovery data remains in '{staging}'."
                raise CompletionRecoveryError(detail) from exc
            raise CompletionError(detail) from exc
        raise
    finally:
        if not committed and not preserve_staging and _lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)

    # The completion is irrevocably committed once the log replace succeeds.
    # Cleanup failures therefore remain successful completions: callers must
    # detach/rescan, while these warnings can tell the user about residue.
    cleanup_warnings: list[str] = list(commit_warnings)
    staged_output = staging / "output"
    for _original, staged_path in moved:
        if staged_path == staged_output or not _lexists(staged_path):
            continue
        try:
            _remove_managed_path(staged_path)
        except OSError as exc:
            cleanup_warnings.append(f"Could not remove staged '{staged_path.name}': {exc}")
    if _lexists(staging):
        try:
            shutil.rmtree(staging)
        except OSError as exc:
            cleanup_warnings.append(f"Could not remove completion staging: {exc}")

    return CompletionResult(
        completed_directory=paths.completed,
        log_path=paths.log,
        completed_at=completed_at,
        output_volumes=current.output_volumes,
        total_image_count=current.total_image_count,
        cleanup_warnings=tuple(cleanup_warnings),
    )


def _completion_paths(
    working_directory: str | Path, manga: MangaRef
) -> _CompletionPaths:
    root = _resolve_working_directory(working_directory)

    name = manga.name
    if not _is_safe_manga_name(name):
        raise CompletionError("The manga name cannot be used as a safe folder name.")
    if name.casefold() == COMPLETION_LOG_FILENAME.casefold():
        raise CompletionError(
            f"'{name}' is reserved for the completion history and cannot be completed."
        )

    source = root / name
    manga_path = Path(manga.path).expanduser()
    if manga_path.is_symlink() or not _lexists(manga_path):
        raise CompletionError("The source manga folder is missing or is a symbolic link.")
    try:
        resolved_manga = manga_path.resolve(strict=True)
    except OSError as exc:
        raise CompletionError(f"The source manga folder could not be resolved: {exc}") from exc
    if resolved_manga != source or resolved_manga.parent != root:
        raise CompletionError(
            "The source manga must be the matching folder directly inside the "
            "working directory."
        )

    metadata = root / ".pocket-manga-editor"
    completed_root = metadata / "completed"
    paths = _CompletionPaths(
        root=root,
        metadata=metadata,
        source=source,
        output=metadata / "output" / name,
        selections=metadata / "selections" / name,
        exports=metadata / "exports" / name,
        completed_root=completed_root,
        completed=completed_root / name,
        log=completed_root / COMPLETION_LOG_FILENAME,
    )

    for candidate, label in (
        (metadata, "app metadata folder"),
        (metadata / "output", "output root"),
        (metadata / "selections", "selection metadata root"),
        (metadata / "exports", "export metadata root"),
        (completed_root, "completed root"),
    ):
        _validate_managed_parent(candidate, root, label)

    if _lexists(paths.completed):
        if paths.completed.is_symlink():
            suffix = " (symbolic links are not allowed)"
        else:
            suffix = ""
        raise CompletionError(
            f"A completed item already exists at '{paths.completed}'{suffix}. "
            "Move it elsewhere before completing another batch with this name."
        )
    if _lexists(paths.log) and paths.log.is_symlink():
        raise CompletionError("The completion history cannot be a symbolic link.")
    return paths


def _resolve_working_directory(working_directory: str | Path) -> Path:
    raw_root = Path(working_directory).expanduser()
    if raw_root.is_symlink():
        raise CompletionError("The working directory cannot be a symbolic link.")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise CompletionError(f"The working directory could not be resolved: {exc}") from exc
    if not root.is_dir():
        raise CompletionError(f"The working directory is not a folder: {root}")
    return root


def _is_safe_manga_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
    )


def _validate_managed_parent(path: Path, root: Path, label: str) -> None:
    if not path.resolve(strict=False).is_relative_to(root):
        raise CompletionError(f"The {label} points outside the working directory.")
    if _lexists(path):
        if path.is_symlink():
            raise CompletionError(f"The {label} cannot be a symbolic link.")
        if not path.is_dir():
            raise CompletionError(f"The {label} is not a folder.")


def _validate_source(
    paths: _CompletionPaths, manga: MangaRef
) -> tuple[
    tuple[tuple[str, str, int, int], ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    if not paths.source.is_dir() or paths.source.is_symlink():
        raise CompletionError("The source manga is missing or is not a safe folder.")
    names_by_identity: dict[str, str] = {}
    for volume in manga.volumes:
        if volume.manga_name != manga.name or volume.manga_path.resolve() != paths.source:
            raise CompletionError("The source volume metadata belongs to another manga.")

    # MangaRef intentionally omits empty, malformed, ambiguous, and unreadable
    # volume folders and can become stale after a scan. It is therefore used
    # only to validate ownership; the live immediate Vol.* directories are the
    # complete source-volume inventory used for comparison and logging.
    try:
        children = sorted(
            paths.source.iterdir(), key=lambda path: (path.name.casefold(), path.name)
        )
    except OSError as exc:
        raise CompletionError(f"Could not inventory source volume folders: {exc}") from exc

    source_folders: list[str] = []
    unmatched_folders: list[str] = []
    for child in children:
        try:
            child_stat = child.lstat()
        except OSError as exc:
            raise CompletionError(f"Could not inspect source item '{child}': {exc}") from exc
        if not stat.S_ISDIR(child_stat.st_mode):
            continue
        if not child.name.casefold().startswith("vol."):
            continue
        source_folders.append(child.name)
        match = CHAPTER_PATTERN.fullmatch(child.name) or VOLUME_PATTERN.fullmatch(child.name)
        if match is None:
            unmatched_folders.append(child.name)
            continue
        identity = _decimal_identity(match.group("volume"))
        if identity is None:
            unmatched_folders.append(child.name)
            continue
        names_by_identity.setdefault(identity, f"Vol.{match.group('volume')}")

    ordered_identities = sorted(
        names_by_identity,
        key=lambda identity: (Decimal(identity), names_by_identity[identity].casefold()),
    )
    source_volumes = tuple(names_by_identity[identity] for identity in ordered_identities)
    source_volumes += tuple(
        sorted(unmatched_folders, key=lambda value: (value.casefold(), value))
    )
    return (
        _tree_snapshot(paths.source, "source manga"),
        source_volumes,
        tuple(source_folders),
    )


def _validate_output(
    paths: _CompletionPaths,
) -> tuple[tuple[tuple[str, str, int, int], ...], tuple[OutputVolumeSummary, ...]]:
    if not _lexists(paths.output):
        raise CompletionError(
            f"There is no exported output for '{paths.output.name}'."
        )
    if paths.output.is_symlink() or not paths.output.is_dir():
        raise CompletionError("The manga output is not a safe folder.")
    tree = _tree_snapshot(paths.output, "manga output")

    try:
        children = sorted(
            paths.output.iterdir(), key=lambda path: (path.name.casefold(), path.name)
        )
    except OSError as exc:
        raise CompletionError(f"Could not inspect the manga output: {exc}") from exc

    volumes: list[OutputVolumeSummary] = []
    for child in children:
        if child.is_symlink():
            raise CompletionError("Output folders and files cannot be symbolic links.")
        if not child.is_dir():
            continue
        try:
            images = tuple(
                sorted(
                    (
                        page.name
                        for page in child.iterdir()
                        if page.is_file()
                        and not page.is_symlink()
                        and page.suffix.casefold() in _IMAGE_SUFFIXES
                    ),
                    key=lambda name: (name.casefold(), name),
                )
            )
        except OSError as exc:
            raise CompletionError(f"Could not inspect output volume '{child}': {exc}") from exc
        volumes.append(OutputVolumeSummary(child.name, images))
    return tree, tuple(volumes)


def _validate_optional_managed_tree(path: Path, label: str) -> None:
    if not _lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        raise CompletionError(f"The {label} is not a safe folder.")
    _tree_snapshot(path, label)


def _tree_snapshot(
    root: Path, label: str
) -> tuple[tuple[str, str, int, int], ...]:
    """Return a deterministic stat snapshot while rejecting every symlink."""

    entries: list[tuple[str, str, int, int]] = []
    try:
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode):
            raise CompletionError(f"The {label} cannot be a symbolic link.")
        entries.append((".", "directory", root_stat.st_size, root_stat.st_mtime_ns))

        def raise_walk_error(error: OSError) -> None:
            raise error

        for current_text, directory_names, filenames in os.walk(
            root, followlinks=False, onerror=raise_walk_error
        ):
            current = Path(current_text)
            directory_names.sort(key=lambda value: (value.casefold(), value))
            filenames.sort(key=lambda value: (value.casefold(), value))
            for name, kind in (
                *((name, "directory") for name in directory_names),
                *((name, "file") for name in filenames),
            ):
                child = current / name
                child_stat = child.lstat()
                if stat.S_ISLNK(child_stat.st_mode):
                    relative = child.relative_to(root).as_posix()
                    raise CompletionError(
                        f"The {label} contains a symbolic link: {relative}"
                    )
                actual_kind = "directory" if stat.S_ISDIR(child_stat.st_mode) else kind
                entries.append(
                    (
                        child.relative_to(root).as_posix(),
                        actual_kind,
                        child_stat.st_size,
                        child_stat.st_mtime_ns,
                    )
                )
    except CompletionError:
        raise
    except OSError as exc:
        raise CompletionError(f"Could not inspect the {label}: {exc}") from exc
    entries.sort(key=lambda item: (item[0].casefold(), item[0]))
    return tuple(entries)


def _compare_volumes(
    source_names: tuple[str, ...], output: tuple[OutputVolumeSummary, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Match canonical numeric identities while preserving original labels."""

    remaining_by_identity: dict[str, list[OutputVolumeSummary]] = {}
    unexpected: list[str] = []
    for volume in output:
        identity = _output_volume_identity(volume.name)
        if identity is None or volume.image_count == 0:
            unexpected.append(volume.name)
            continue
        remaining_by_identity.setdefault(identity, []).append(volume)

    missing: list[str] = []
    for source_name in source_names:
        identity = _output_volume_identity(source_name)
        candidates = remaining_by_identity.get(identity or "", [])
        if not candidates:
            missing.append(source_name)
            continue
        exact_index = next(
            (index for index, candidate in enumerate(candidates) if candidate.name == source_name),
            0,
        )
        candidates.pop(exact_index)

    for candidates in remaining_by_identity.values():
        unexpected.extend(volume.name for volume in candidates)
    unexpected.sort(key=lambda value: (value.casefold(), value))
    return tuple(missing), tuple(unexpected)


def _output_volume_identity(name: str) -> str | None:
    match = _OUTPUT_VOLUME_PATTERN.fullmatch(name)
    if match is None:
        return None
    return _decimal_identity(match.group("volume"))


def _decimal_identity(value: str) -> str | None:
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        return None


def _load_log(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not _lexists(path):
        return {
            "schema_version": COMPLETION_LOG_SCHEMA_VERSION,
            "completions": [],
        }, None
    if path.is_symlink() or not path.is_file():
        raise CompletionError("The completion history is not a safe regular file.")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompletionError(f"The completion history could not be read safely: {exc}") from exc

    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != COMPLETION_LOG_SCHEMA_VERSION
        or not isinstance(payload.get("completions"), list)
    ):
        raise CompletionError("The completion history uses an invalid or unsupported format.")
    for entry in payload["completions"]:
        _validate_log_entry(entry)
    return payload, raw


def _validate_log_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        raise CompletionError("The completion history contains an invalid entry.")
    if not all(
        isinstance(entry.get(field), str)
        for field in ("transaction_id", "manga", "completed_at", "app_version")
    ):
        raise CompletionError("The completion history contains an invalid entry.")
    try:
        uuid.UUID(entry["transaction_id"])
    except (ValueError, AttributeError):
        raise CompletionError("The completion history contains an invalid transaction ID.")
    source = entry.get("source")
    output = entry.get("output")
    volume_check = entry.get("volume_check")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("relative_path"), str)
        or not _string_list(source.get("volumes"))
        or not _string_list(source.get("folders"))
        or not isinstance(output, dict)
        or not isinstance(output.get("directory"), str)
        or not isinstance(output.get("total_image_count"), int)
        or isinstance(output.get("total_image_count"), bool)
        or output["total_image_count"] < 0
        or not isinstance(output.get("volumes"), list)
        or not isinstance(volume_check, dict)
        or not _string_list(volume_check.get("missing_from_output"))
        or not _string_list(volume_check.get("unexpected_in_output"))
    ):
        raise CompletionError("The completion history contains an invalid entry.")

    counted = 0
    for volume in output["volumes"]:
        if (
            not isinstance(volume, dict)
            or not isinstance(volume.get("name"), str)
            or not _string_list(volume.get("images"))
            or not isinstance(volume.get("image_count"), int)
            or isinstance(volume.get("image_count"), bool)
            or volume["image_count"] != len(volume["images"])
        ):
            raise CompletionError("The completion history contains an invalid volume entry.")
        counted += volume["image_count"]
    if counted != output["total_image_count"]:
        raise CompletionError("The completion history contains inconsistent image counts.")


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _prior_summary(entry: dict[str, Any]) -> PriorCompletionSummary:
    return PriorCompletionSummary(
        completed_at=entry["completed_at"],
        source_volumes=tuple(entry["source"]["volumes"]),
        output_volumes=tuple(volume["name"] for volume in entry["output"]["volumes"]),
        image_count=entry["output"]["total_image_count"],
    )


def _completion_entry(
    preview: CompletionPreview, completed_at: str, transaction_id: str
) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "manga": preview.manga_name,
        "completed_at": completed_at,
        "app_version": __version__,
        "source": {
            "relative_path": preview.source_directory.name,
            "volumes": list(preview.source_volumes),
            "folders": list(preview.source_folders),
        },
        "output": {
            "directory": preview.completed_directory.name,
            "volumes": [
                {
                    "name": volume.name,
                    "image_count": volume.image_count,
                    "images": list(volume.image_files),
                }
                for volume in preview.output_volumes
            ],
            "total_image_count": preview.total_image_count,
        },
        "volume_check": {
            "missing_from_output": list(preview.missing_volumes),
            "unexpected_in_output": list(preview.unexpected_volumes),
        },
    }


def _same_preview_identity(
    expected: CompletionPreview, current: CompletionPreview
) -> bool:
    return (
        isinstance(expected, CompletionPreview)
        and expected.manga_name == current.manga_name
        and expected.source_directory == current.source_directory
        and expected.output_directory == current.output_directory
        and expected.completed_directory == current.completed_directory
        and expected.snapshot_token == current.snapshot_token
    )


def _recover_interrupted_completions_locked(
    root: Path, metadata: Path
) -> CompletionRecoveryResult:
    completed_root = metadata / "completed"
    try:
        staging_items = sorted(
            (
                child
                for child in metadata.iterdir()
                if child.name.startswith(_STAGING_PREFIX)
            ),
            key=lambda path: path.name,
        )
    except OSError as exc:
        raise CompletionRecoveryError(
            f"Could not inspect completion recovery data: {exc}"
        ) from exc
    if not staging_items:
        return CompletionRecoveryResult(0, 0)

    marked_staging: list[Path] = []
    for staging in staging_items:
        if staging.is_symlink() or not staging.is_dir():
            raise CompletionRecoveryError(
                f"Completion recovery item is not a safe folder: {staging}"
            )
        marker_path = staging / _TRANSACTION_MARKER_FILENAME
        if _lexists(marker_path):
            marked_staging.append(staging)
            continue
        # A crash before the durable marker was installed cannot have moved any
        # managed path. Empty staging or atomic marker-temp residue is safe to
        # drop; any other content fails closed.
        try:
            residue = tuple(staging.iterdir())
        except OSError as exc:
            raise CompletionRecoveryError(
                f"Could not inspect markerless recovery data: {staging}: {exc}"
            ) from exc
        safe_residue = all(
            not child.is_symlink()
            and child.is_file()
            and child.name.startswith(f".{_TRANSACTION_MARKER_FILENAME}.")
            and child.name.endswith(".tmp")
            for child in residue
        )
        if not safe_residue:
            raise CompletionRecoveryError(
                f"Completion recovery marker is missing: {marker_path}"
            )
        try:
            shutil.rmtree(staging)
        except OSError as exc:
            raise CompletionRecoveryError(
                f"Could not remove unused completion recovery data: {staging}: {exc}"
            ) from exc

    if not marked_staging:
        return CompletionRecoveryResult(0, 0)

    for candidate, label in (
        (metadata / "output", "output root"),
        (metadata / "selections", "selection metadata root"),
        (metadata / "exports", "export metadata root"),
        (completed_root, "completed root"),
    ):
        try:
            _validate_managed_parent(candidate, root, label)
        except CompletionError as exc:
            raise CompletionRecoveryError(str(exc)) from exc
    try:
        log_payload, _raw = _load_log(completed_root / COMPLETION_LOG_FILENAME)
    except CompletionError as exc:
        raise CompletionRecoveryError(str(exc)) from exc
    committed_ids = {
        entry["transaction_id"] for entry in log_payload["completions"]
    }

    rolled_back = 0
    cleaned = 0
    seen_ids: set[str] = set()
    for staging in marked_staging:
        marker = _load_transaction_marker(staging)
        transaction_id = marker["transaction_id"]
        if transaction_id in seen_ids:
            raise CompletionRecoveryError(
                f"Duplicate completion transaction ID in recovery data: {transaction_id}"
            )
        seen_ids.add(transaction_id)
        paths = _recovery_paths(root, metadata, marker["manga"])
        _validate_recovery_transaction_paths(staging, paths)

        try:
            if transaction_id in committed_ids:
                _finish_committed_recovery(staging, paths)
                cleaned += 1
            else:
                _rollback_interrupted_recovery(staging, paths, marker["present"])
                rolled_back += 1
        except CompletionRecoveryError:
            raise
        except OSError as exc:
            raise CompletionRecoveryError(
                f"Could not recover transaction {transaction_id}: {exc}"
            ) from exc

    return CompletionRecoveryResult(rolled_back, cleaned)


def _load_transaction_marker(staging: Path) -> dict[str, Any]:
    marker_path = staging / _TRANSACTION_MARKER_FILENAME
    if not _lexists(marker_path) or marker_path.is_symlink() or not marker_path.is_file():
        raise CompletionRecoveryError(
            f"Completion recovery marker is missing or unsafe: {marker_path}"
        )
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompletionRecoveryError(
            f"Completion recovery marker could not be read: {marker_path}: {exc}"
        ) from exc
    present = payload.get("present") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _TRANSACTION_SCHEMA_VERSION
        or not isinstance(payload.get("transaction_id"), str)
        or not isinstance(payload.get("manga"), str)
        or not isinstance(present, dict)
        or set(present) != {"source", "output", "selections", "exports"}
        or any(not isinstance(value, bool) for value in present.values())
    ):
        raise CompletionRecoveryError(
            f"Completion recovery marker is invalid: {marker_path}"
        )
    try:
        uuid.UUID(payload["transaction_id"])
    except (ValueError, AttributeError) as exc:
        raise CompletionRecoveryError(
            f"Completion recovery marker has an invalid transaction ID: {marker_path}"
        ) from exc
    if (
        not _is_safe_manga_name(payload["manga"])
        or payload["manga"].casefold() == COMPLETION_LOG_FILENAME.casefold()
    ):
        raise CompletionRecoveryError(
            f"Completion recovery marker has an unsafe manga name: {marker_path}"
        )
    return payload


def _recovery_paths(root: Path, metadata: Path, manga_name: str) -> _CompletionPaths:
    completed_root = metadata / "completed"
    return _CompletionPaths(
        root=root,
        metadata=metadata,
        source=root / manga_name,
        output=metadata / "output" / manga_name,
        selections=metadata / "selections" / manga_name,
        exports=metadata / "exports" / manga_name,
        completed_root=completed_root,
        completed=completed_root / manga_name,
        log=completed_root / COMPLETION_LOG_FILENAME,
    )


def _validate_recovery_transaction_paths(
    staging: Path, paths: _CompletionPaths
) -> None:
    for candidate in (
        paths.source,
        paths.output,
        paths.selections,
        paths.exports,
        paths.completed,
        staging / "source",
        staging / "output",
        staging / "selections",
        staging / "exports",
    ):
        if not _lexists(candidate):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise CompletionRecoveryError(
                f"Completion recovery path is not a safe folder: {candidate}"
            )


def _rollback_interrupted_recovery(
    staging: Path, paths: _CompletionPaths, present: dict[str, bool]
) -> None:
    staged_output = staging / "output"
    output_locations = tuple(
        path
        for path in (paths.output, staged_output, paths.completed)
        if _lexists(path)
    )
    if present["output"]:
        if len(output_locations) != 1:
            raise CompletionRecoveryError(
                "Interrupted completion output cannot be restored unambiguously: "
                + ", ".join(str(path) for path in output_locations)
            )
        output_location = output_locations[0]
        if output_location != paths.output:
            paths.output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(output_location, paths.output)
    elif staged_output in output_locations or paths.completed in output_locations:
        raise CompletionRecoveryError(
            "Interrupted completion contains output that was not recorded by its marker."
        )

    for name, original in (
        ("selections", paths.selections),
        ("exports", paths.exports),
        ("source", paths.source),
    ):
        staged = staging / name
        original_exists = _lexists(original)
        staged_exists = _lexists(staged)
        if present[name]:
            if original_exists and staged_exists:
                raise CompletionRecoveryError(
                    f"Interrupted completion has conflicting '{name}' copies."
                )
            if not original_exists and not staged_exists:
                raise CompletionRecoveryError(
                    f"Interrupted completion is missing its '{name}' data."
                )
            if staged_exists:
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, original)
        elif staged_exists:
            raise CompletionRecoveryError(
                f"Interrupted completion contains unrecorded '{name}' data."
            )

    shutil.rmtree(staging)


def _finish_committed_recovery(staging: Path, paths: _CompletionPaths) -> None:
    staged_output = staging / "output"
    if _lexists(staged_output):
        if _lexists(paths.completed):
            raise CompletionRecoveryError(
                "A committed completion has conflicting completed output copies."
            )
        paths.completed_root.mkdir(parents=True, exist_ok=True)
        os.replace(staged_output, paths.completed)
        _fsync_directory(paths.completed_root)
    # With no staged output, the committed output was already installed. It may
    # since have been moved by the user, and active output may belong to a newer
    # batch; neither is recovery data and neither should block cleanup.
    shutil.rmtree(staging)


def _fsync_directory(path: Path) -> None:
    """Persist a marker rename on platforms that support directory fsync."""

    if os.name == "nt":  # pragma: no cover - Windows cannot open directories this way
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_completion(
    paths: _CompletionPaths,
    moved: list[tuple[Path, Path]],
    installed: bool,
) -> list[str]:
    errors: list[str] = []
    if installed and _lexists(paths.completed):
        try:
            os.replace(paths.completed, paths.output)
        except OSError as exc:
            errors.append(f"could not restore output: {exc}")

    for original, staged in reversed(moved):
        if original == paths.output and installed:
            continue
        if not _lexists(staged):
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, original)
        except OSError as exc:
            errors.append(f"could not restore '{original}': {exc}")
    return errors


def _remove_managed_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
