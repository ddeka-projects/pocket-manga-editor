"""Crash-safe completion into immutable, per-manga output batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from . import __version__
from .exporter import (
    ExportError,
    ExportRecoveryError,
    recover_interrupted_exports_locked,
    verify_managed_output,
)
from .filesystem_ops import (
    prepare_managed_path,
    remove_managed_path,
    rename_no_replace,
)
from .library_lock import LibraryBusyError, LibraryLockError, library_mutation_lock
from .models import MangaRef
from .path_safety import is_link_or_reparse
from .scanner import ScanError, scan_working_directory
from .storage import EditingStateError, EditingStore, atomic_write_json
from .workspace import (
    MangaWorkspacePaths,
    WorkspaceError,
    manga_workspace_paths,
    validate_completed_workspace,
    validate_completion_workspace,
    validate_live_manga,
    validate_transaction_workspace,
)


TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_DIRECTORY_PREFIX = "completion-"
TRANSACTION_MARKER_FILENAME = "transaction.json"
COMMITTED_MARKER_FILENAME = "committed.json"
_RETIRED_TRANSACTION_PREFIX = ".retired-completion-"
_BATCH_PATTERN = re.compile(r"^batch-(?P<number>\d{4,})$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INTENT_KEYS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "manga",
        "batch",
        "created_at",
        "app_version",
        "present",
        "snapshot_token",
    }
)
_COMMIT_KEYS = frozenset({"schema_version", "transaction_id"})

_TreeSnapshot = tuple[tuple[str, str, int, int, str | None], ...]
_BatchNamespace = tuple[tuple[str, str, int, int], ...]


class CompletionError(RuntimeError):
    """Raised when a manga cannot be completed safely."""


class CompletionChangedError(CompletionError):
    """Raised when the filesystem changed after confirmation was shown."""


class CompletionBusyError(CompletionError):
    """Raised when another process currently owns the library mutation lock."""


class CompletionRecoveryError(CompletionError):
    """Raised when an interrupted transaction cannot be reconciled safely."""

    state_may_have_changed = True


@dataclass(frozen=True, slots=True)
class ManagedOutputFolderSummary:
    """Manifest-backed exported images in one exact source folder."""

    name: str
    image_files: tuple[str, ...]

    @property
    def image_count(self) -> int:
        return len(self.image_files)


@dataclass(frozen=True, slots=True)
class CompletionBatchSummary:
    """One durable batch discovered directly from the completed directory."""

    name: str
    directory: Path


@dataclass(frozen=True, slots=True)
class CompletionPreview:
    """Validated, read-only description shown before destructive confirmation."""

    manga_name: str
    source_directory: Path
    workspace_directory: Path
    output_directory: Path
    destination_batch: Path
    source_folder_count: int
    output_folders: tuple[ManagedOutputFolderSummary, ...]
    existing_batches: tuple[CompletionBatchSummary, ...]
    snapshot_token: str

    @property
    def exported_folder_count(self) -> int:
        return len(self.output_folders)

    @property
    def total_image_count(self) -> int:
        return sum(folder.image_count for folder in self.output_folders)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """The immutable batch created by a successful completion."""

    batch_directory: Path
    batch_name: str
    output_folders: tuple[ManagedOutputFolderSummary, ...]
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
    workspace: MangaWorkspacePaths
    source: Path
    target_batch: Path


@dataclass(frozen=True, slots=True)
class _TransactionIntent:
    transaction_id: str
    manga_name: str
    batch_name: str
    reading_present: bool
    editing_present: bool
    snapshot_token: str


def analyze_completion(
    working_directory: str | Path, manga: MangaRef
) -> CompletionPreview:
    """Validate and describe a completion without changing any files."""

    try:
        workspace = manga_workspace_paths(working_directory, manga.name)
        with library_mutation_lock(workspace.root):
            return _analyze_completion_locked(workspace.root, manga)
    except LibraryBusyError as exc:
        raise CompletionBusyError(str(exc)) from exc
    except LibraryLockError as exc:
        raise CompletionError(str(exc)) from exc
    except WorkspaceError as exc:
        raise CompletionError(str(exc)) from exc


def _analyze_completion_locked(root: Path, manga: MangaRef) -> CompletionPreview:
    """Analyze while the caller owns the global library mutation lock."""

    try:
        workspace = validate_completion_workspace(
            manga_workspace_paths(root, manga.name)
        )
        source = validate_live_manga(workspace.root, manga)
        live_manga = _rescan_manga(workspace.root, manga.name)
        validate_live_manga(workspace.root, live_manga)
        editing = EditingStore(workspace.root)._load_locked(workspace.root, live_manga)
        inventory = verify_managed_output(workspace, editing)
    except (WorkspaceError, ScanError, EditingStateError, ExportError, OSError) as exc:
        if isinstance(exc, CompletionError):
            raise
        raise CompletionError(str(exc)) from exc

    if inventory.image_count < 1:
        raise CompletionError(
            f"'{workspace.output}' contains no valid app-managed exported images. "
            "Export at least one selected image before completing this manga."
        )

    try:
        if (
            source.stat(follow_symlinks=False).st_dev
            != workspace.workspace.stat(follow_symlinks=False).st_dev
        ):
            raise CompletionError(
                "The source manga and its metadata workspace are on different "
                "filesystems, so completion cannot stage them atomically."
            )
    except CompletionError:
        raise
    except OSError as exc:
        raise CompletionError(
            f"Could not verify the completion staging filesystem: {exc}"
        ) from exc

    source_tree = _snapshot_tree(source, "source manga")
    output_tree = _snapshot_tree(workspace.output, "manga output")
    reading_file = _snapshot_optional_file(workspace.reading, "reading metadata")
    editing_file = _snapshot_optional_file(workspace.editing, "editing metadata")
    existing_batches, destination_batch, batch_namespace = _batch_inventory(workspace)

    output_folders = tuple(
        ManagedOutputFolderSummary(folder.folder_name, tuple(folder.image_names))
        for folder in inventory.folders
    )
    snapshot_token = _completion_snapshot_token(
        manga_name=manga.name,
        source_tree=source_tree,
        source_folder_count=len(live_manga.folders),
        output_tree=output_tree,
        output_folders=output_folders,
        reading_file=reading_file,
        editing_file=editing_file,
        batch_namespace=batch_namespace,
        destination_batch_name=destination_batch.name,
    )

    return CompletionPreview(
        manga_name=manga.name,
        source_directory=source,
        workspace_directory=workspace.workspace,
        output_directory=workspace.output,
        destination_batch=destination_batch,
        source_folder_count=len(live_manga.folders),
        output_folders=output_folders,
        existing_batches=existing_batches,
        snapshot_token=snapshot_token,
    )


def complete_manga(
    working_directory: str | Path,
    manga: MangaRef,
    expected_preview: CompletionPreview,
) -> CompletionResult:
    """Finalize active output after revalidating the confirmed preview."""

    try:
        workspace = manga_workspace_paths(working_directory, manga.name)
        with library_mutation_lock(workspace.root):
            return _complete_manga_locked(workspace.root, manga, expected_preview)
    except LibraryBusyError as exc:
        raise CompletionBusyError(str(exc)) from exc
    except LibraryLockError as exc:
        raise CompletionError(str(exc)) from exc
    except WorkspaceError as exc:
        raise CompletionError(str(exc)) from exc


def recover_interrupted_completions(
    working_directory: str | Path,
) -> CompletionRecoveryResult:
    """Roll back precommit journals and finish postcommit cleanup."""

    try:
        with library_mutation_lock(working_directory) as root:
            return _recover_interrupted_completions_locked(root)
    except LibraryBusyError as exc:
        raise CompletionBusyError(str(exc)) from exc
    except LibraryLockError as exc:
        raise CompletionError(str(exc)) from exc


def _complete_manga_locked(
    root: Path, manga: MangaRef, expected_preview: CompletionPreview
) -> CompletionResult:
    try:
        recover_interrupted_exports_locked(root)
    except ExportRecoveryError as exc:
        raise CompletionRecoveryError(
            f"Interrupted export recovery must finish before completion: {exc}"
        ) from exc
    except ExportError as exc:
        raise CompletionError(
            f"Interrupted export recovery must finish before completion: {exc}"
        ) from exc

    workspace = validate_transaction_workspace(
        manga_workspace_paths(root, manga.name)
    )
    active_transactions, _retired = _completion_transaction_directories(workspace)
    if active_transactions:
        raise CompletionRecoveryError(
            f"Interrupted completion data already exists for '{manga.name}'. "
            "Run completion recovery before trying again."
        )

    current = _analyze_completion_locked(root, manga)
    if not _same_preview_identity(expected_preview, current):
        raise CompletionChangedError(
            "The manga source, output, active metadata, or completed batches changed "
            "after confirmation was opened. Review the completion details again."
        )

    paths = _CompletionPaths(
        workspace=workspace,
        source=current.source_directory,
        target_batch=current.destination_batch,
    )
    transaction_id = str(uuid.uuid4())
    intent = _TransactionIntent(
        transaction_id=transaction_id,
        manga_name=manga.name,
        batch_name=paths.target_batch.name,
        reading_present=_lexists(workspace.reading),
        editing_present=_lexists(workspace.editing),
        snapshot_token=current.snapshot_token,
    )
    transaction = workspace.transactions / (
        TRANSACTION_DIRECTORY_PREFIX + transaction_id
    )
    phase = "create completion recovery data"
    committed_warning: str | None = None

    try:
        workspace.workspace.mkdir(parents=True, exist_ok=True)
        workspace.transactions.mkdir(parents=True, exist_ok=True)
        _fsync_directory(workspace.workspace)
        transaction.mkdir()
        _fsync_directory(workspace.transactions)

        phase = "save the completion recovery marker"
        atomic_write_json(
            transaction / TRANSACTION_MARKER_FILENAME,
            _intent_payload(intent),
        )
        _fsync_directory(transaction)

        phase = "stage the active manga data"
        for original, staged in (
            (workspace.output, transaction / "output"),
            (workspace.reading, transaction / "reading.json"),
            (workspace.editing, transaction / "editing.json"),
            (paths.source, transaction / "source"),
        ):
            if _lexists(original):
                _rename_managed(original, staged)

        phase = "revalidate the staged manga data"
        _verify_staged_preview(paths.workspace, transaction, current)

        phase = "install the completed batch"
        if _lexists(paths.target_batch):
            raise CompletionChangedError(
                f"The destination batch '{paths.target_batch.name}' now exists."
            )
        workspace.completed.mkdir(parents=True, exist_ok=True)
        _fsync_directory(workspace.workspace)
        _rename_managed(transaction / "output", paths.target_batch)

        phase = "commit the completion"
        atomic_write_json(
            transaction / COMMITTED_MARKER_FILENAME,
            {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "transaction_id": transaction_id,
            },
        )
        try:
            _fsync_directory(transaction)
        except OSError as exc:
            committed_warning = f"Could not sync the commit marker directory: {exc}"
    except BaseException as exc:
        try:
            commit_state = _commit_state(transaction, transaction_id)
        except CompletionRecoveryError as marker_error:
            raise CompletionRecoveryError(
                f"Could not determine whether completion committed while trying to "
                f"{phase}: {marker_error}. Recovery data remains in '{transaction}'."
            ) from exc

        if commit_state:
            if not isinstance(exc, Exception):
                raise
            committed_warning = f"Could not {phase} after the completion committed: {exc}"
        else:
            marker_path = transaction / TRANSACTION_MARKER_FILENAME
            if not _lexists(transaction):
                rollback_errors = []
            elif _lexists(marker_path):
                try:
                    disk_intent = _load_transaction_intent(root, transaction)
                except CompletionRecoveryError as marker_error:
                    raise CompletionRecoveryError(
                        f"Could not {phase}: {exc}. The recovery marker is unsafe: "
                        f"{marker_error}. Recovery data remains in '{transaction}'."
                    ) from exc
                rollback_errors = _rollback_uncommitted(
                    paths.workspace, transaction, disk_intent
                )
            else:
                rollback_errors = _discard_markerless_transaction(transaction)

            if not isinstance(exc, Exception):
                raise
            detail = f"Could not {phase}: {exc}"
            if rollback_errors:
                detail += ". Rollback was incomplete: " + "; ".join(rollback_errors)
                detail += f". Recovery data remains in '{transaction}'."
                raise CompletionRecoveryError(detail) from exc
            if isinstance(exc, CompletionChangedError):
                raise CompletionChangedError(detail) from exc
            raise CompletionError(detail) from exc

    cleanup_warnings: list[str] = []
    if committed_warning:
        cleanup_warnings.append(committed_warning)
    cleanup_warnings.extend(_finish_committed(paths.workspace, transaction, intent))
    return CompletionResult(
        batch_directory=paths.target_batch,
        batch_name=paths.target_batch.name,
        output_folders=current.output_folders,
        total_image_count=current.total_image_count,
        cleanup_warnings=tuple(cleanup_warnings),
    )


def _recover_interrupted_completions_locked(root: Path) -> CompletionRecoveryResult:
    metadata = root / ".pocket-manga-editor"
    if not _lexists(metadata):
        return CompletionRecoveryResult(0, 0)
    if is_link_or_reparse(metadata) or not metadata.is_dir():
        raise CompletionRecoveryError("The app metadata path is not a safe directory.")

    discovered: list[tuple[MangaWorkspacePaths, Path]] = []
    retired: list[Path] = []
    try:
        workspace_candidates = sorted(
            metadata.iterdir(), key=lambda path: (path.name.casefold(), path.name)
        )
    except OSError as exc:
        raise CompletionRecoveryError(
            f"Could not inspect completion recovery workspaces: {exc}"
        ) from exc

    for candidate in workspace_candidates:
        if candidate.name == ".library-mutation.lock":
            continue
        if is_link_or_reparse(candidate):
            raise CompletionRecoveryError(
                f"Manga workspace '{candidate}' cannot be a symbolic link or junction."
            )
        if not candidate.is_dir():
            continue
        try:
            workspace = validate_transaction_workspace(
                manga_workspace_paths(root, candidate.name)
            )
        except WorkspaceError as exc:
            raise CompletionRecoveryError(str(exc)) from exc
        active_items, retired_items = _completion_transaction_directories(workspace)
        discovered.extend((workspace, item) for item in active_items)
        retired.extend(retired_items)

    warnings: list[str] = []
    for terminal in retired:
        warnings.extend(_remove_retired_transaction(terminal))

    parsed: list[tuple[MangaWorkspacePaths, Path, _TransactionIntent]] = []
    seen_ids: set[str] = set()
    seen_targets: set[Path] = set()
    for workspace, transaction in discovered:
        marker = transaction / TRANSACTION_MARKER_FILENAME
        if not _lexists(marker):
            errors = _discard_markerless_transaction(transaction)
            if errors:
                raise CompletionRecoveryError("; ".join(errors))
            continue
        intent = _load_transaction_intent(root, transaction)
        if intent.transaction_id in seen_ids:
            raise CompletionRecoveryError(
                f"Duplicate completion transaction ID: {intent.transaction_id}"
            )
        target = workspace.completed / intent.batch_name
        if target in seen_targets:
            raise CompletionRecoveryError(
                f"Multiple completion transactions claim '{target}'."
            )
        seen_ids.add(intent.transaction_id)
        seen_targets.add(target)
        parsed.append((workspace, transaction, intent))

    rolled_back = 0
    cleaned = 0
    for workspace, transaction, intent in parsed:
        if _commit_state(transaction, intent.transaction_id):
            try:
                validate_completed_workspace(workspace)
            except WorkspaceError as exc:
                raise CompletionRecoveryError(str(exc)) from exc
            warnings.extend(_finish_committed(workspace, transaction, intent))
            if not _lexists(transaction):
                cleaned += 1
            continue

        try:
            validate_completion_workspace(workspace)
        except WorkspaceError as exc:
            raise CompletionRecoveryError(str(exc)) from exc
        errors = _rollback_uncommitted(workspace, transaction, intent)
        if errors:
            raise CompletionRecoveryError(
                f"Could not roll back transaction {intent.transaction_id}: "
                + "; ".join(errors)
            )
        rolled_back += 1

    return CompletionRecoveryResult(rolled_back, cleaned, tuple(warnings))


def _rescan_manga(root: Path, manga_name: str) -> MangaRef:
    result = scan_working_directory(root)
    for manga in result.mangas:
        if manga.name == manga_name:
            return manga
    raise CompletionError(
        f"The source manga '{manga_name}' no longer contains a discoverable image folder."
    )


def _batch_inventory(
    workspace: MangaWorkspacePaths,
) -> tuple[
    tuple[CompletionBatchSummary, ...],
    Path,
    _BatchNamespace,
]:
    if not _lexists(workspace.completed):
        return (), workspace.completed / "batch-0001", ()

    summaries: list[tuple[int, CompletionBatchSummary]] = []
    namespace: list[tuple[str, str, int, int]] = []
    occupied_casefold: set[str] = set()
    highest = 0
    try:
        entries = sorted(
            workspace.completed.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
        for entry in entries:
            information = entry.stat(follow_symlinks=False)
            kind = (
                "directory"
                if stat.S_ISDIR(information.st_mode)
                else "file"
                if stat.S_ISREG(information.st_mode)
                else "special"
            )
            namespace.append(
                (entry.name, kind, information.st_size, information.st_mtime_ns)
            )
            occupied_casefold.add(entry.name.casefold())
            number = _batch_number(entry.name)
            if number is None:
                continue
            if is_link_or_reparse(entry) or not stat.S_ISDIR(information.st_mode):
                raise CompletionError(
                    f"Completed batch path is not a safe directory: {entry}"
                )
            highest = max(highest, number)
            summaries.append((number, CompletionBatchSummary(entry.name, entry)))
    except CompletionError:
        raise
    except OSError as exc:
        raise CompletionError(f"Could not inspect completed batches: {exc}") from exc

    next_number = highest + 1
    while True:
        name = f"batch-{next_number:04d}"
        if name.casefold() not in occupied_casefold:
            break
        next_number += 1
    summaries.sort(key=lambda item: (item[0], item[1].name))
    return (
        tuple(summary for _number, summary in summaries),
        workspace.completed / name,
        tuple(namespace),
    )


def _same_preview_identity(
    expected: CompletionPreview, current: CompletionPreview
) -> bool:
    return (
        isinstance(expected, CompletionPreview)
        and expected.manga_name == current.manga_name
        and expected.source_directory == current.source_directory
        and expected.workspace_directory == current.workspace_directory
        and expected.output_directory == current.output_directory
        and expected.destination_batch == current.destination_batch
        and expected.snapshot_token == current.snapshot_token
    )


def _completion_snapshot_token(
    *,
    manga_name: str,
    source_tree: _TreeSnapshot,
    source_folder_count: int,
    output_tree: _TreeSnapshot,
    output_folders: tuple[ManagedOutputFolderSummary, ...],
    reading_file: tuple[int, int, str] | None,
    editing_file: tuple[int, int, str] | None,
    batch_namespace: _BatchNamespace,
    destination_batch_name: str,
) -> str:
    payload = {
        "manga": manga_name,
        "source": source_tree,
        "source_folder_count": source_folder_count,
        "output": output_tree,
        "managed_output": [
            (folder.name, folder.image_files) for folder in output_folders
        ],
        "reading": reading_file,
        "editing": editing_file,
        "batch_namespace": batch_namespace,
        "destination_batch": destination_batch_name,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_staged_preview(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    preview: CompletionPreview,
) -> None:
    """Prove the staged destructive payload still matches the accepted preview."""

    source_tree = _snapshot_tree(transaction / "source", "staged source manga")
    output_tree = _snapshot_tree(transaction / "output", "staged manga output")
    reading_file = _snapshot_optional_file(
        transaction / "reading.json", "staged reading metadata"
    )
    editing_file = _snapshot_optional_file(
        transaction / "editing.json", "staged editing metadata"
    )
    _existing, destination_batch, batch_namespace = _batch_inventory(workspace)
    staged_token = _completion_snapshot_token(
        manga_name=preview.manga_name,
        source_tree=source_tree,
        source_folder_count=preview.source_folder_count,
        output_tree=output_tree,
        output_folders=preview.output_folders,
        reading_file=reading_file,
        editing_file=editing_file,
        batch_namespace=batch_namespace,
        destination_batch_name=destination_batch.name,
    )
    if staged_token != preview.snapshot_token:
        raise CompletionChangedError(
            "The manga source, output, active metadata, or completed batches changed "
            "while completion data was being staged."
        )


def _intent_payload(intent: _TransactionIntent) -> dict[str, Any]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": intent.transaction_id,
        "manga": intent.manga_name,
        "batch": intent.batch_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": __version__,
        "present": {
            "source": True,
            "output": True,
            "reading": intent.reading_present,
            "editing": intent.editing_present,
        },
        "snapshot_token": intent.snapshot_token,
    }


def _load_transaction_intent(root: Path, transaction: Path) -> _TransactionIntent:
    marker = transaction / TRANSACTION_MARKER_FILENAME
    if is_link_or_reparse(marker) or not marker.is_file():
        raise CompletionRecoveryError(
            f"Completion recovery marker is missing or unsafe: {marker}"
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompletionRecoveryError(
            f"Completion recovery marker could not be read: {marker}: {exc}"
        ) from exc

    present = payload.get("present") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != _INTENT_KEYS
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != TRANSACTION_SCHEMA_VERSION
        or not isinstance(payload.get("transaction_id"), str)
        or not isinstance(payload.get("manga"), str)
        or not isinstance(payload.get("batch"), str)
        or not _valid_created_at(payload.get("created_at"))
        or not _valid_app_version(payload.get("app_version"))
        or not isinstance(payload.get("snapshot_token"), str)
        or _DIGEST_PATTERN.fullmatch(payload["snapshot_token"]) is None
        or not isinstance(present, dict)
        or set(present) != {"source", "output", "reading", "editing"}
        or present.get("source") is not True
        or present.get("output") is not True
        or type(present.get("reading")) is not bool
        or type(present.get("editing")) is not bool
    ):
        raise CompletionRecoveryError(
            f"Completion recovery marker has an invalid format: {marker}"
        )
    try:
        transaction_uuid = uuid.UUID(payload["transaction_id"])
    except (ValueError, AttributeError) as exc:
        raise CompletionRecoveryError(
            f"Completion recovery marker has an invalid transaction ID: {marker}"
        ) from exc
    if payload["transaction_id"] != str(transaction_uuid):
        raise CompletionRecoveryError(
            f"Completion recovery marker has a non-canonical transaction ID: {marker}"
        )
    expected_name = TRANSACTION_DIRECTORY_PREFIX + str(transaction_uuid)
    if transaction.name != expected_name:
        raise CompletionRecoveryError(
            f"Completion recovery directory does not match its marker: {transaction}"
        )
    if _batch_number(payload["batch"]) is None:
        raise CompletionRecoveryError(
            f"Completion recovery marker has an unsafe batch name: {marker}"
        )
    try:
        workspace = manga_workspace_paths(root, payload["manga"])
    except WorkspaceError as exc:
        raise CompletionRecoveryError(str(exc)) from exc
    if transaction.parent != workspace.transactions:
        raise CompletionRecoveryError(
            f"Completion recovery marker belongs to another manga: {marker}"
        )
    return _TransactionIntent(
        transaction_id=payload["transaction_id"],
        manga_name=payload["manga"],
        batch_name=payload["batch"],
        reading_present=present["reading"],
        editing_present=present["editing"],
        snapshot_token=payload["snapshot_token"],
    )


def _commit_state(transaction: Path, transaction_id: str) -> bool:
    marker = transaction / COMMITTED_MARKER_FILENAME
    if not _lexists(marker):
        return False
    if is_link_or_reparse(marker) or not marker.is_file():
        raise CompletionRecoveryError(f"Commit marker is unsafe: {marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompletionRecoveryError(f"Commit marker is unreadable: {marker}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _COMMIT_KEYS
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != TRANSACTION_SCHEMA_VERSION
        or not isinstance(payload.get("transaction_id"), str)
        or payload.get("transaction_id") != transaction_id
    ):
        raise CompletionRecoveryError(f"Commit marker is invalid: {marker}")
    return True


def _valid_created_at(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        created_at.tzinfo is not None
        and created_at.utcoffset() == timezone.utc.utcoffset(created_at)
        and created_at.isoformat() == value
    )


def _valid_app_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value == value.strip()
        and all(
            character.isprintable() and not character.isspace()
            for character in value
        )
    )


def _rollback_uncommitted(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    intent: _TransactionIntent,
) -> list[str]:
    errors: list[str] = []
    source = workspace.root / intent.manga_name
    target = workspace.completed / intent.batch_name

    output_locations = [
        candidate
        for candidate in (workspace.output, transaction / "output", target)
        if _lexists(candidate)
    ]
    if len(output_locations) != 1:
        errors.append(
            "output cannot be restored unambiguously (found "
            + ", ".join(str(path) for path in output_locations)
            + ")"
        )
    else:
        try:
            output_location = output_locations[0]
            _snapshot_tree(output_location, "completion output being restored")
            if output_location != workspace.output:
                _rename_managed(output_location, workspace.output)
        except (CompletionError, OSError) as exc:
            errors.append(f"could not restore output: {exc}")

    for label, original, staged, originally_present in (
        ("source", source, transaction / "source", True),
        (
            "reading metadata",
            workspace.reading,
            transaction / "reading.json",
            intent.reading_present,
        ),
        (
            "editing metadata",
            workspace.editing,
            transaction / "editing.json",
            intent.editing_present,
        ),
    ):
        original_exists = _lexists(original)
        staged_exists = _lexists(staged)
        if originally_present:
            if original_exists == staged_exists:
                errors.append(
                    f"{label} cannot be restored unambiguously; expected exactly one copy"
                )
                continue
            carrier = staged if staged_exists else original
            if staged_exists:
                try:
                    if label == "source":
                        _snapshot_tree(carrier, "source manga being restored")
                    else:
                        _validate_regular_file(carrier, label)
                    _rename_managed(staged, original)
                except (CompletionError, OSError) as exc:
                    errors.append(f"could not restore {label}: {exc}")
            else:
                try:
                    if label == "source":
                        _snapshot_tree(carrier, "source manga being restored")
                    else:
                        _validate_regular_file(carrier, label)
                except (CompletionError, OSError) as exc:
                    errors.append(f"could not validate restored {label}: {exc}")
        elif original_exists or staged_exists:
            errors.append(f"transaction contains unrecorded {label}")

    if errors:
        return errors
    errors.extend(_retire_transaction(transaction))
    return errors


def _finish_committed(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    intent: _TransactionIntent,
) -> list[str]:
    """Finish only transaction-owned residue; never touch active paths."""

    warnings: list[str] = []
    cleanup_incomplete = False
    target = workspace.completed / intent.batch_name
    staged_output = transaction / "output"
    if _lexists(staged_output):
        if _lexists(target):
            raise CompletionRecoveryError(
                f"Committed transaction has conflicting output copies at "
                f"'{staged_output}' and '{target}'."
            )
        try:
            workspace.completed.mkdir(parents=True, exist_ok=True)
            _fsync_directory(workspace.workspace)
            _validate_directory(staged_output, "staged completion output")
            _rename_managed(staged_output, target)
        except (CompletionError, OSError) as exc:
            warnings.append(f"Could not finish installing '{target.name}': {exc}")
            return warnings
    elif not _lexists(target):
        warnings.append(
            f"Completed batch '{intent.batch_name}' is no longer in the workspace; "
            "it may have been archived or removed."
        )

    # A visible commit marker is the logical commit point, but destructive
    # cleanup must not begin until its directory entry is durable. If the
    # original post-write sync failed, leave every staged payload intact for a
    # later recovery attempt rather than risk a power loss exposing a partial
    # precommit tree with no durable marker.
    try:
        _fsync_directory(transaction)
    except OSError as exc:
        warnings.append(f"Could not sync committed recovery data: {exc}")
        return warnings

    for staged, label in (
        (transaction / "source", "staged source manga"),
        (transaction / "reading.json", "staged reading metadata"),
        (transaction / "editing.json", "staged editing metadata"),
    ):
        if not _lexists(staged):
            continue
        try:
            _remove_managed_path(staged, label)
        except (CompletionError, OSError) as exc:
            warnings.append(f"Could not remove {label}: {exc}")
            cleanup_incomplete = True

    if cleanup_incomplete or any(
        _lexists(transaction / name)
        for name in ("output", "source", "reading.json", "editing.json")
    ):
        return warnings
    warnings.extend(_retire_transaction(transaction))
    return warnings


def _completion_transaction_directories(
    workspace: MangaWorkspacePaths,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    if not _lexists(workspace.transactions):
        return (), ()
    active: list[Path] = []
    retired: list[Path] = []
    try:
        children = sorted(
            workspace.transactions.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise CompletionRecoveryError(
            f"Could not inspect '{workspace.transactions}': {exc}"
        ) from exc
    for child in children:
        if child.name.startswith(TRANSACTION_DIRECTORY_PREFIX):
            if is_link_or_reparse(child) or not child.is_dir():
                raise CompletionRecoveryError(
                    f"Completion transaction is not a safe directory: {child}"
                )
            active.append(child)
        elif child.name.startswith(_RETIRED_TRANSACTION_PREFIX):
            if is_link_or_reparse(child) or not child.is_dir():
                raise CompletionRecoveryError(
                    f"Retired completion transaction is unsafe: {child}"
                )
            retired.append(child)
    return tuple(active), tuple(retired)


def _discard_markerless_transaction(transaction: Path) -> list[str]:
    try:
        children = tuple(transaction.iterdir())
    except OSError as exc:
        return [f"Could not inspect markerless completion transaction: {exc}"]
    safe = all(
        not is_link_or_reparse(child)
        and child.is_file()
        and child.name.startswith(f".{TRANSACTION_MARKER_FILENAME}.")
        and child.name.endswith(".tmp")
        for child in children
    )
    if not safe:
        return [
            f"Completion recovery marker is missing and '{transaction}' contains data"
        ]
    return _retire_transaction(transaction)


def _retire_transaction(transaction: Path) -> list[str]:
    """Atomically make a payload-free journal safe for partial marker deletion."""

    retired = transaction.parent / (
        _RETIRED_TRANSACTION_PREFIX
        + transaction.name.removeprefix(TRANSACTION_DIRECTORY_PREFIX)
    )
    try:
        rename_no_replace(transaction, retired)
        _fsync_directory(retired.parent)
    except OSError as exc:
        return [f"Could not retire completion recovery data: {exc}"]
    return _remove_retired_transaction(retired)


def _remove_retired_transaction(retired: Path) -> list[str]:
    warnings: list[str] = []
    try:
        children = tuple(retired.iterdir())
        for child in children:
            marker_name = child.name in {
                TRANSACTION_MARKER_FILENAME,
                COMMITTED_MARKER_FILENAME,
            }
            temporary_name = (
                child.name.startswith(".transaction.json.")
                or child.name.startswith(".committed.json.")
            ) and child.name.endswith(".tmp")
            if (
                is_link_or_reparse(child)
                or not child.is_file()
                or not (marker_name or temporary_name)
            ):
                raise OSError(f"unexpected recovery residue: {child}")
        remove_managed_path(retired)
        _fsync_directory(retired.parent)
    except OSError as exc:
        warnings.append(f"Could not remove retired completion recovery data: {exc}")
    return warnings


def _rename_managed(source: Path, destination: Path) -> None:
    if not _lexists(source):
        raise OSError(f"managed source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        rename_no_replace(source, destination)
    except OSError as exc:
        if _lexists(destination):
            raise OSError(
                f"managed destination already exists: {destination}"
            ) from exc
        raise
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _remove_managed_path(path: Path, label: str) -> None:
    if is_link_or_reparse(path):
        raise OSError(f"{label} is a symbolic link or junction")
    information = prepare_managed_path(path)
    if stat.S_ISDIR(information.st_mode):
        _snapshot_tree(path, label)
    elif stat.S_ISREG(information.st_mode):
        _validate_regular_file(path, label)
    else:
        raise OSError(f"{label} is not a regular file or directory")
    remove_managed_path(path)
    _fsync_directory(path.parent)


def _snapshot_tree(root: Path, label: str) -> _TreeSnapshot:
    """Return a content-aware snapshot while rejecting unsafe entries."""

    _validate_directory(root, label)
    try:
        resolved_root = root.resolve(strict=True)
        root_information = root.stat(follow_symlinks=False)
        root_device = root_information.st_dev
        entries: list[tuple[str, str, int, int, str | None]] = [
            (
                ".",
                "directory",
                root_information.st_size,
                root_information.st_mtime_ns,
                None,
            )
        ]

        def visit(directory: Path) -> None:
            children = sorted(
                directory.iterdir(), key=lambda path: (path.name.casefold(), path.name)
            )
            for child in children:
                if is_link_or_reparse(child):
                    raise CompletionError(
                        f"The {label} contains a symbolic link or junction: "
                        f"{child.relative_to(root).as_posix()}"
                    )
                information = child.stat(follow_symlinks=False)
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(resolved_root):
                    raise CompletionError(
                        f"The {label} contains a path outside its root: {child}"
                    )
                if information.st_dev != root_device:
                    raise CompletionError(
                        f"The {label} crosses a mounted filesystem boundary: {child}"
                    )
                relative = child.relative_to(root).as_posix()
                if stat.S_ISDIR(information.st_mode):
                    kind = "directory"
                    digest = None
                elif stat.S_ISREG(information.st_mode):
                    kind = "file"
                    digest = _stable_file_digest(child, information, label)
                else:
                    raise CompletionError(
                        f"The {label} contains an unsupported special file: {relative}"
                    )
                entries.append(
                    (
                        relative,
                        kind,
                        information.st_size,
                        information.st_mtime_ns,
                        digest,
                    )
                )
                if kind == "directory":
                    visit(child)

        visit(root)
    except CompletionError:
        raise
    except OSError as exc:
        raise CompletionError(f"Could not inspect the {label}: {exc}") from exc
    return tuple(entries)


def _stable_file_digest(
    path: Path, expected: os.stat_result, tree_label: str
) -> str:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_before = os.fstat(descriptor)
        if (
            opened_before.st_dev != expected.st_dev
            or opened_before.st_ino != expected.st_ino
            or not stat.S_ISREG(opened_before.st_mode)
        ):
            raise CompletionChangedError(
                f"A file in the {tree_label} changed while it was opened: {path}"
            )
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if is_link_or_reparse(path):
        raise CompletionChangedError(
            f"A file in the {tree_label} became a link while it was read: {path}"
        )
    path_after = path.stat(follow_symlinks=False)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (
        any(getattr(opened_before, field) != getattr(opened_after, field) for field in stable_fields)
        or any(getattr(opened_after, field) != getattr(path_after, field) for field in stable_fields)
        or byte_count != opened_after.st_size
    ):
        raise CompletionChangedError(
            f"A file in the {tree_label} changed while it was read: {path}"
        )
    return digest.hexdigest()


def _snapshot_optional_file(path: Path, label: str) -> tuple[int, int, str] | None:
    if not _lexists(path):
        return None
    if is_link_or_reparse(path):
        raise CompletionError(f"The {label} cannot be a symbolic link or junction.")
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise CompletionError(f"The {label} is not a regular file.")
        raw = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except CompletionError:
        raise
    except OSError as exc:
        raise CompletionError(f"Could not inspect the {label}: {exc}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(raw) != after.st_size
    ):
        raise CompletionChangedError(f"The {label} changed while it was inspected.")
    return after.st_size, after.st_mtime_ns, hashlib.sha256(raw).hexdigest()


def _validate_directory(path: Path, label: str) -> None:
    if not _lexists(path) or is_link_or_reparse(path):
        raise CompletionError(f"The {label} is missing or is a link: {path}")
    try:
        information = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CompletionError(f"Could not inspect the {label}: {exc}") from exc
    if not stat.S_ISDIR(information.st_mode):
        raise CompletionError(f"The {label} is not a directory: {path}")


def _validate_regular_file(path: Path, label: str) -> None:
    if not _lexists(path) or is_link_or_reparse(path):
        raise CompletionError(f"The {label} is missing or is a link: {path}")
    try:
        information = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CompletionError(f"Could not inspect the {label}: {exc}") from exc
    if not stat.S_ISREG(information.st_mode):
        raise CompletionError(f"The {label} is not a regular file: {path}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows cannot fsync directories
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _batch_number(name: str) -> int | None:
    match = _BATCH_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        number = int(match.group("number"))
    except ValueError:
        return None
    if number < 1 or name != f"batch-{number:04d}":
        return None
    return number


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
