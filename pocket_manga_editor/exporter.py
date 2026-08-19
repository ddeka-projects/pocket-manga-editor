"""Transactional, whole-manga synchronization of selected source images."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Mapping

from .library_lock import (
    LibraryBusyError,
    LibraryLockError,
    library_mutation_lock,
)
from .filesystem_ops import remove_managed_path, rename_no_replace
from .models import FolderRef, ImageRef, MangaRef
from .path_safety import is_link_or_reparse
from .scanner import natural_name_key
from .storage import (
    EditingSnapshot,
    EditingStore,
    ExportedImageState,
    FolderExportState,
    _editing_payload,
    atomic_write_json,
)
from .workspace import (
    MangaWorkspacePaths,
    WorkspaceError,
    manga_workspace_paths,
    validate_export_workspace,
    validate_live_manga,
    validate_output_workspace,
)


EXPORT_TRANSACTION_SCHEMA_VERSION = 1
_EXPORT_TRANSACTION_PREFIX = "export-"
_RETIRED_EXPORT_TRANSACTION_PREFIX = ".retired-export-"
_JOURNAL_NAME = "transaction.json"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_WINDOWS_FILE_SYNC = os.name == "nt"


class ExportError(RuntimeError):
    """Raised when a manga export cannot be completed safely."""


class ExportConflict(ExportError):
    """Raised rather than overwriting output not owned by this application."""


class ExportBusyError(ExportError):
    """Raised when another library mutation currently owns the global lock."""


class ExportRecoveryError(ExportError):
    """Raised when interrupted export state cannot be reconciled safely."""

    state_may_have_changed = True


@dataclass(frozen=True, slots=True)
class FolderExportResult:
    folder_name: str
    copied_count: int
    retained_count: int
    removed_count: int


@dataclass(frozen=True, slots=True)
class MangaExportResult:
    output_directory: Path
    folders: tuple[FolderExportResult, ...]
    copied_count: int
    retained_count: int
    removed_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagedOutputFolder:
    folder_name: str
    image_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedOutputInventory:
    output_directory: Path
    folders: tuple[ManagedOutputFolder, ...]
    image_count: int


@dataclass(frozen=True, slots=True)
class ExportRecoveryResult:
    committed_count: int
    rolled_back_count: int
    discarded_count: int

    @property
    def recovered_count(self) -> int:
        return self.committed_count + self.rolled_back_count + self.discarded_count


@dataclass(frozen=True, slots=True)
class _DesiredImage:
    folder: FolderRef
    image: ImageRef
    output_name: str
    source_digest: str
    previous: ExportedImageState | None
    previous_file_exists: bool


@dataclass(frozen=True, slots=True)
class _ExportPlan:
    desired: Mapping[str, Mapping[str, _DesiredImage]]
    previous: Mapping[str, FolderExportState]
    previous_existing: frozenset[tuple[str, str]]


def exported_image_name(folder_name: str, image_name: str) -> str:
    """Return the exact output filename for one folder/image identity."""

    if not _safe_component(folder_name) or not _safe_component(image_name):
        raise ExportError("Folder and image names must be safe exact path components.")
    if Path(image_name).suffix.casefold() not in {".jpg", ".png"}:
        raise ExportError(f"Image '{image_name}' is not a supported JPG or PNG file.")
    return f"{folder_name}__{image_name}"


def manga_output_directory(
    working_directory: str | Path, manga: MangaRef
) -> Path:
    validate_live_manga(working_directory, manga)
    workspace = manga_workspace_paths(working_directory, manga.name)
    return validate_output_workspace(workspace).output


def verify_managed_output(
    workspace: MangaWorkspacePaths, editing: EditingSnapshot
) -> ManagedOutputInventory:
    """Strictly verify manifest-backed output while the caller holds the lock."""

    try:
        paths = validate_export_workspace(workspace)
    except WorkspaceError as exc:
        raise ExportError(str(exc)) from exc
    if workspace.output.exists():
        _validate_safe_tree(workspace.output)

    seen_targets: dict[str, tuple[str, str]] = {}
    seen_folders: dict[str, str] = {}
    folders: list[ManagedOutputFolder] = []
    image_count = 0
    for folder_name in sorted(editing.exports, key=natural_name_key):
        _record_folder_collision(seen_folders, folder_name)
        export = editing.exports[folder_name]
        image_names: list[str] = []
        for image_name in sorted(export.files, key=natural_name_key):
            entry = export.files[image_name]
            expected = exported_image_name(folder_name, image_name)
            if entry.output_name != expected:
                raise ExportError(
                    f"Export metadata for '{folder_name}/{image_name}' has an unexpected output name."
                )
            relative = Path(folder_name) / expected
            _record_collision(seen_targets, relative, (folder_name, image_name))
            target = workspace.output / relative
            _require_regular_file(target, workspace.output / folder_name)
            try:
                digest = _sha256(target)
            except OSError as exc:
                raise ExportError(f"Could not verify managed output '{target}': {exc}") from exc
            if digest != entry.digest:
                raise ExportConflict(
                    f"Managed output '{target}' changed after its last export."
                )
            image_names.append(image_name)
            image_count += 1
        if image_names:
            folders.append(ManagedOutputFolder(folder_name, tuple(image_names)))
    return ManagedOutputInventory(workspace.output, tuple(folders), image_count)


def export_manga(
    working_directory: str | Path, manga: MangaRef
) -> MangaExportResult:
    """Synchronize all current manga selections as one atomic logical export."""

    try:
        with library_mutation_lock(working_directory) as root:
            recover_interrupted_exports_locked(root)
            return _export_manga_locked(root, manga)
    except LibraryBusyError as exc:
        raise ExportBusyError(str(exc)) from exc
    except (LibraryLockError, WorkspaceError) as exc:
        raise ExportError(str(exc)) from exc


def recover_interrupted_exports(
    working_directory: str | Path,
) -> ExportRecoveryResult:
    """Reconcile every durable export journal under the global mutation lock."""

    try:
        with library_mutation_lock(working_directory) as root:
            return recover_interrupted_exports_locked(root)
    except LibraryBusyError as exc:
        raise ExportBusyError(str(exc)) from exc
    except (LibraryLockError, WorkspaceError) as exc:
        raise ExportRecoveryError(str(exc)) from exc


def _export_manga_locked(root: Path, manga: MangaRef) -> MangaExportResult:
    validate_live_manga(root, manga)
    workspace = manga_workspace_paths(root, manga.name)
    validate_export_workspace(workspace)
    store = EditingStore(root)
    editing = store._load_locked(root, manga)
    plan = _build_plan(workspace, manga, editing)
    if not plan.desired and not plan.previous:
        raise ExportError("Select at least one image before exporting this manga.")

    try:
        workspace.workspace.mkdir(parents=False, exist_ok=True)
        workspace.transactions.mkdir(parents=False, exist_ok=True)
        _fsync_directory(workspace.workspace)
    except OSError as exc:
        raise ExportError(f"Could not create the export transaction workspace: {exc}") from exc
    workspace = validate_export_workspace(manga_workspace_paths(root, manga.name))

    try:
        transaction = Path(
            tempfile.mkdtemp(prefix=_EXPORT_TRANSACTION_PREFIX, dir=workspace.transactions)
        )
    except OSError as exc:
        raise ExportError(f"Could not create an export transaction: {exc}") from exc

    new_output = transaction / "new-output"
    old_output = transaction / "old-output"
    new_editing = transaction / "new-editing.json"
    old_editing = transaction / "old-editing.json"
    journal_path = transaction / _JOURNAL_NAME
    journal: dict[str, object] | None = None
    result: MangaExportResult | None = None
    committed = False
    old_output_moved = False
    new_output_installed = False

    try:
        had_output = workspace.output.exists()
        old_output_digest = _tree_digest(workspace.output) if had_output else None
        if workspace.output.exists():
            shutil.copytree(workspace.output, new_output, symlinks=True)
        else:
            new_output.mkdir()
        _validate_safe_tree(new_output)
        staged_baseline_digest = _tree_digest(new_output)
        if had_output:
            if staged_baseline_digest != old_output_digest:
                raise ExportConflict(
                    "Manga output changed while its export snapshot was being copied."
                )
        elif any(new_output.iterdir()):
            raise ExportConflict(
                "Manga output appeared while its export snapshot was being copied."
            )
        _verify_staged_managed_output(new_output, plan)

        updated_exports, folder_results = _apply_plan(new_output, plan)
        _fsync_tree(new_output)
        updated_editing = replace(
            editing, exports=updated_exports, warnings=()
        )
        atomic_write_json(new_editing, _editing_payload(manga, updated_editing))
        new_editing_digest = _sha256(new_editing)

        had_editing = workspace.editing.exists()
        old_editing_digest: str | None = None
        if had_editing:
            shutil.copy2(workspace.editing, old_editing)
            _fsync_file(old_editing)
            old_editing_digest = _sha256(old_editing)

        new_output_has_entries = any(new_output.iterdir())
        new_output_digest = (
            _tree_digest(new_output) if new_output_has_entries else None
        )
        journal = {
            "schema_version": EXPORT_TRANSACTION_SCHEMA_VERSION,
            "kind": "manga-export",
            "transaction_id": transaction.name,
            "manga": manga.name,
            "phase": "prepared",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "replace_output": True,
            "replace_editing": True,
            "had_output": had_output,
            "new_output_present": new_output_has_entries,
            "old_output_digest": old_output_digest,
            "new_output_digest": new_output_digest,
            "had_editing": had_editing,
            "old_editing_digest": old_editing_digest,
            "new_editing_digest": new_editing_digest,
        }
        atomic_write_json(journal_path, journal)
        _fsync_directory(transaction)
        _fsync_directory(workspace.transactions)

        _revalidate_desired_sources(plan)
        _verify_precommit_state(workspace, journal)
        if had_output:
            os.replace(workspace.output, old_output)
            old_output_moved = True
            if not _tree_matches(old_output, old_output_digest):
                raise ExportConflict(
                    "Manga output changed immediately before it was staged."
                )
        if new_output_has_entries:
            _rename_no_replace(new_output, workspace.output)
            new_output_installed = True
            if not _tree_matches(workspace.output, new_output_digest):
                raise ExportConflict(
                    "New manga output changed while it was being installed."
                )
        _fsync_directory(workspace.workspace)
        _fsync_directory(transaction)

        os.replace(new_editing, workspace.editing)
        committed = True
        result = _export_result(workspace.output, folder_results)

        warnings: list[str] = []
        try:
            _fsync_directory(workspace.workspace)
        except OSError as exc:
            warnings.append(f"Could not sync the committed manga workspace: {exc}")
        try:
            journal["phase"] = "committed"
            atomic_write_json(journal_path, journal)
            _fsync_directory(transaction)
        except OSError as exc:
            warnings.append(f"Could not mark export cleanup state: {exc}")
        try:
            _retire_export_transaction(transaction, workspace.transactions)
        except (OSError, ExportError) as exc:
            warnings.append(f"Could not remove committed export staging: {exc}")
        if warnings:
            result = replace(result, warnings=tuple(warnings))
        return result
    except BaseException as exc:
        if journal is not None:
            current_editing_digest = _file_digest(workspace.editing)
            if (
                journal.get("new_editing_digest")
                != journal.get("old_editing_digest")
                and current_editing_digest == journal.get("new_editing_digest")
            ):
                committed = True
        if committed:
            if result is None:
                folder_results = folder_results if "folder_results" in locals() else ()
                result = _export_result(workspace.output, folder_results)
            warning = f"Export committed but cleanup was interrupted: {exc}"
            if isinstance(exc, Exception):
                return replace(result, warnings=(*result.warnings, warning))
            raise

        rollback_errors: list[str] = []
        active_output_changed = (
            old_output_moved
            or new_output_installed
            or os.path.lexists(old_output)
            or (
                journal is not None
                and bool(journal.get("new_output_present"))
                and not os.path.lexists(new_output)
            )
        )
        if journal is not None and active_output_changed:
            rollback_errors = _rollback_transaction(
                workspace,
                transaction,
                journal,
                old_output_moved=old_output_moved,
                new_output_installed=new_output_installed,
                trust_staged_original=True,
            )
        if not rollback_errors:
            try:
                _retire_export_transaction(transaction, workspace.transactions)
            except (OSError, ExportError) as cleanup_exc:
                rollback_errors.append(f"could not remove export staging: {cleanup_exc}")
        if rollback_errors:
            raise ExportRecoveryError(
                f"Export failed and rollback was incomplete: {'; '.join(rollback_errors)}"
            ) from exc
        if isinstance(exc, ExportError):
            raise
        if isinstance(exc, OSError):
            raise ExportError(f"Could not commit the manga export: {exc}") from exc
        raise


def _build_plan(
    workspace: MangaWorkspacePaths,
    manga: MangaRef,
    editing: EditingSnapshot,
) -> _ExportPlan:
    if workspace.output.exists():
        existing_paths = _validate_safe_tree(workspace.output)
    else:
        existing_paths = {}
    prior_existing: set[tuple[str, str]] = set()
    seen_targets: dict[str, tuple[str, str]] = {}
    seen_folders: dict[str, str] = {}

    for relative in existing_paths.values():
        if relative.parts:
            _record_folder_collision(seen_folders, relative.parts[0])

    for folder_name, export in editing.exports.items():
        _record_folder_collision(seen_folders, folder_name)
        for image_name, entry in export.files.items():
            expected = exported_image_name(folder_name, image_name)
            if entry.output_name != expected:
                raise ExportError(
                    f"Export metadata for '{folder_name}/{image_name}' has an unexpected output name."
                )
            relative = Path(folder_name) / expected
            _validate_destination_path(workspace, relative)
            _record_collision(seen_targets, relative, (folder_name, image_name))
            target = workspace.output / relative
            if os.path.lexists(target):
                _require_regular_file(target, workspace.output / folder_name)
                if _sha256(target) != entry.digest:
                    raise ExportConflict(
                        f"Managed output '{target}' changed after its last export."
                    )
                prior_existing.add((folder_name, image_name))

    desired: dict[str, dict[str, _DesiredImage]] = {}
    for folder in manga.folders:
        state = editing.folders.get(folder.name)
        selected = state.selected_images if state is not None else frozenset()
        if not selected:
            continue
        _record_folder_collision(seen_folders, folder.name)
        desired_folder: dict[str, _DesiredImage] = {}
        previous_files = editing.exports.get(folder.name, FolderExportState({})).files
        for image in folder.images:
            if image.name not in selected:
                continue
            output_name = exported_image_name(folder.name, image.name)
            relative = Path(folder.name) / output_name
            _validate_destination_path(workspace, relative)
            owner = (folder.name, image.name)
            _record_collision(seen_targets, relative, owner)
            existing_relative = existing_paths.get(_path_key(relative))
            if existing_relative is not None and existing_relative != relative:
                raise ExportConflict(
                    f"Output path '{relative}' collides with existing '{existing_relative}'."
                )
            source_digest = _sha256_source(folder, image)
            previous = previous_files.get(image.name)
            target = workspace.output / relative
            if os.path.lexists(target) and previous is None:
                _require_regular_file(target, workspace.output / folder.name)
                raise ExportConflict(
                    f"Untracked output '{target}' would collide with a selected image."
                )
            desired_folder[image.name] = _DesiredImage(
                folder,
                image,
                output_name,
                source_digest,
                previous,
                owner in prior_existing,
            )
        desired[folder.name] = desired_folder
    return _ExportPlan(desired, dict(editing.exports), frozenset(prior_existing))


def _apply_plan(
    staged_output: Path, plan: _ExportPlan
) -> tuple[dict[str, FolderExportState], tuple[FolderExportResult, ...]]:
    new_exports: dict[str, FolderExportState] = {}
    results: list[FolderExportResult] = []
    folder_names = sorted(set(plan.previous) | set(plan.desired), key=natural_name_key)
    for folder_name in folder_names:
        previous_files = plan.previous.get(folder_name, FolderExportState({})).files
        desired_files = plan.desired.get(folder_name, {})
        copied = 0
        retained = 0
        removed = 0

        for image_name, entry in previous_files.items():
            if image_name in desired_files:
                continue
            target = staged_output / folder_name / entry.output_name
            if os.path.lexists(target):
                _require_regular_file(target, target.parent)
                target.unlink()
                if (folder_name, image_name) in plan.previous_existing:
                    removed += 1

        exported_files: dict[str, ExportedImageState] = {}
        for image_name in sorted(desired_files, key=natural_name_key):
            desired = desired_files[image_name]
            folder_path = staged_output / folder_name
            if os.path.lexists(folder_path) and not folder_path.is_dir():
                raise ExportConflict(
                    f"Output folder '{folder_path}' is occupied by a non-directory."
                )
            folder_path.mkdir(parents=True, exist_ok=True)
            target = folder_path / desired.output_name
            unchanged_managed_file = (
                desired.previous is not None
                and desired.previous_file_exists
                and desired.previous.digest == desired.source_digest
            )
            if unchanged_managed_file:
                _require_regular_file(target, folder_path)
                digest = _sha256(target)
                if digest != desired.source_digest:
                    raise ExportConflict(
                        f"Managed output '{target}' changed while the manga export "
                        "was being prepared."
                    )
            else:
                _copy_source_image(desired.folder, desired.image, target)
                digest = _sha256(target)
                if digest != desired.source_digest:
                    raise ExportConflict(
                        f"Source image '{folder_name}/{image_name}' changed while "
                        "the manga export was being prepared."
                    )
            exported_files[image_name] = ExportedImageState(
                desired.output_name, digest
            )
            if unchanged_managed_file:
                retained += 1
            else:
                copied += 1
        if exported_files:
            new_exports[folder_name] = FolderExportState(exported_files)

        folder_path = staged_output / folder_name
        if folder_path.exists():
            try:
                folder_path.rmdir()
            except OSError:
                pass
        results.append(FolderExportResult(folder_name, copied, retained, removed))
    return new_exports, tuple(results)


def _verify_staged_managed_output(staged_output: Path, plan: _ExportPlan) -> None:
    """Prove the copied baseline still matches every prior manifest decision."""

    for folder_name, export in plan.previous.items():
        for image_name, entry in export.files.items():
            target = staged_output / folder_name / entry.output_name
            was_present = (folder_name, image_name) in plan.previous_existing
            if not was_present:
                if os.path.lexists(target):
                    raise ExportConflict(
                        f"Managed output '{target}' appeared while export was being prepared."
                    )
                continue
            _require_regular_file(target, target.parent)
            try:
                digest = _sha256(target)
            except OSError as exc:
                raise ExportError(
                    f"Could not verify staged managed output '{target}': {exc}"
                ) from exc
            if digest != entry.digest:
                raise ExportConflict(
                    f"Managed output '{target}' changed while export was being prepared."
                )


def _revalidate_desired_sources(plan: _ExportPlan) -> None:
    """Recheck source bytes immediately before the first active-path rename."""

    for folder_name in sorted(plan.desired, key=natural_name_key):
        for image_name in sorted(plan.desired[folder_name], key=natural_name_key):
            desired = plan.desired[folder_name][image_name]
            if _sha256_source(desired.folder, desired.image) != desired.source_digest:
                raise ExportConflict(
                    f"Source image '{folder_name}/{image_name}' changed while "
                    "the manga export was being prepared."
                )


def _export_result(
    output_directory: Path, folders: tuple[FolderExportResult, ...]
) -> MangaExportResult:
    return MangaExportResult(
        output_directory,
        folders,
        sum(folder.copied_count for folder in folders),
        sum(folder.retained_count for folder in folders),
        sum(folder.removed_count for folder in folders),
    )


def recover_interrupted_exports_locked(root: Path) -> ExportRecoveryResult:
    """Reconcile export journals while the caller owns the library mutation lock."""

    metadata = root / ".pocket-manga-editor"
    if not os.path.lexists(metadata):
        return ExportRecoveryResult(0, 0, 0)
    if is_link_or_reparse(metadata) or not metadata.is_dir():
        raise ExportRecoveryError("The app metadata root is not a safe directory.")
    try:
        workspace_candidates = sorted(
            metadata.iterdir(), key=lambda path: natural_name_key(path.name)
        )
    except OSError as exc:
        raise ExportRecoveryError(f"Could not inspect export transactions: {exc}") from exc

    committed_count = 0
    rolled_back_count = 0
    discarded_count = 0
    for candidate in workspace_candidates:
        if candidate.name == ".library-mutation.lock":
            continue
        if is_link_or_reparse(candidate):
            raise ExportRecoveryError(
                f"Manga workspace '{candidate}' cannot be a symbolic link or junction."
            )
        if not candidate.is_dir():
            continue
        try:
            workspace = manga_workspace_paths(root, candidate.name)
        except WorkspaceError as exc:
            raise ExportRecoveryError(str(exc)) from exc
        if not os.path.lexists(workspace.transactions):
            continue
        if (
            is_link_or_reparse(workspace.transactions)
            or not workspace.transactions.is_dir()
        ):
            raise ExportRecoveryError(
                f"Export transaction workspace for '{candidate.name}' is unsafe."
            )
        try:
            transaction_entries = tuple(workspace.transactions.iterdir())
            transactions = sorted(
                (
                    path
                    for path in transaction_entries
                    if path.name.startswith(_EXPORT_TRANSACTION_PREFIX)
                ),
                key=lambda path: path.name,
            )
            retired_transactions = sorted(
                (
                    path
                    for path in transaction_entries
                    if path.name.startswith(_RETIRED_EXPORT_TRANSACTION_PREFIX)
                ),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise ExportRecoveryError(
                f"Could not inspect export staging for '{candidate.name}': {exc}"
            ) from exc

        for retired in retired_transactions:
            _require_transaction_directory(retired, workspace.transactions)
            _remove_retired_export_transaction(retired, workspace.transactions)
            discarded_count += 1

        if not transactions:
            continue
        try:
            validate_export_workspace(workspace)
        except WorkspaceError as exc:
            raise ExportRecoveryError(str(exc)) from exc
        for transaction in transactions:
            _require_transaction_directory(transaction, workspace.transactions)
            journal_path = transaction / _JOURNAL_NAME
            if not os.path.lexists(journal_path):
                _discard_markerless_transaction(transaction)
                discarded_count += 1
                continue
            journal = _load_journal(journal_path, candidate.name, transaction.name)
            current_editing_digest = _file_digest(workspace.editing)
            old_editing_digest = journal["old_editing_digest"]
            new_editing_digest = journal["new_editing_digest"]
            phase = journal["phase"]
            committed = phase == "committed" or (
                new_editing_digest != old_editing_digest
                and current_editing_digest == new_editing_digest
            )
            if committed:
                _finish_committed_transaction(workspace, transaction, journal)
                committed_count += 1
                continue
            if current_editing_digest != old_editing_digest:
                raise ExportRecoveryError(
                    f"Editing metadata changed during interrupted export "
                    f"'{transaction.name}'."
                )
            errors = _rollback_transaction(
                workspace,
                transaction,
                journal,
                old_output_moved=(transaction / "old-output").exists(),
                new_output_installed=_tree_matches(
                    workspace.output, journal["new_output_digest"]
                ),
            )
            if errors:
                raise ExportRecoveryError(
                    "Interrupted export rollback was incomplete: " + "; ".join(errors)
                )
            try:
                _retire_export_transaction(transaction, workspace.transactions)
            except (OSError, ExportError) as exc:
                raise ExportRecoveryError(
                    f"Could not remove recovered export staging: {exc}"
                ) from exc
            rolled_back_count += 1
    return ExportRecoveryResult(
        committed_count, rolled_back_count, discarded_count
    )


def _finish_committed_transaction(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    journal: Mapping[str, object],
) -> None:
    if _file_digest(workspace.editing) != journal["new_editing_digest"]:
        raise ExportRecoveryError(
            f"Committed export '{transaction.name}' does not match editing metadata."
        )
    expected_new = journal["new_output_digest"]
    if expected_new is None:
        if os.path.lexists(workspace.output):
            raise ExportRecoveryError(
                f"Committed export '{transaction.name}' unexpectedly has active output."
            )
    elif not _tree_matches(workspace.output, expected_new):
        staged = transaction / "new-output"
        if not os.path.lexists(workspace.output) and _tree_matches(staged, expected_new):
            _rename_no_replace(staged, workspace.output)
            _fsync_directory(workspace.workspace)
            _fsync_directory(transaction)
        else:
            raise ExportRecoveryError(
                f"Committed export '{transaction.name}' output cannot be verified."
            )
    try:
        _retire_export_transaction(transaction, workspace.transactions)
    except (OSError, ExportError) as exc:
        raise ExportRecoveryError(
            f"Could not finish committed export cleanup: {exc}"
        ) from exc


def _rollback_transaction(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    journal: Mapping[str, object],
    *,
    old_output_moved: bool,
    new_output_installed: bool,
    trust_staged_original: bool = False,
) -> list[str]:
    errors: list[str] = []
    old_output = transaction / "old-output"
    expected_old = journal.get("old_output_digest")
    expected_new = journal.get("new_output_digest")
    had_output = bool(journal.get("had_output"))

    try:
        if old_output_moved or os.path.lexists(old_output):
            if not trust_staged_original and not _tree_matches(
                old_output, expected_old
            ):
                raise OSError("the staged original output no longer matches its journal")
            if os.path.lexists(workspace.output):
                if not _tree_matches(workspace.output, expected_new):
                    raise OSError("active output changed before rollback")
                remove_managed_path(workspace.output)
            _rename_no_replace(old_output, workspace.output)
        elif had_output:
            if not _tree_matches(workspace.output, expected_old):
                raise OSError("the original active output no longer matches its journal")
        elif os.path.lexists(workspace.output):
            if not new_output_installed or not _tree_matches(
                workspace.output, expected_new
            ):
                raise OSError("unexpected active output prevents rollback")
            remove_managed_path(workspace.output)
    except OSError as exc:
        errors.append(f"could not restore output: {exc}")

    old_editing_digest = journal.get("old_editing_digest")
    new_editing_digest = journal.get("new_editing_digest")
    try:
        current_digest = _file_digest(workspace.editing)
        if current_digest == new_editing_digest:
            old_editing = transaction / "old-editing.json"
            if old_editing_digest is None:
                workspace.editing.unlink(missing_ok=True)
            elif _file_digest(old_editing) == old_editing_digest:
                os.replace(old_editing, workspace.editing)
            else:
                raise OSError("the staged original editing metadata is unavailable")
        elif current_digest != old_editing_digest:
            raise OSError("editing metadata changed before rollback")
    except OSError as exc:
        errors.append(f"could not restore editing metadata: {exc}")
    try:
        _fsync_directory(workspace.workspace)
    except OSError as exc:
        errors.append(f"could not sync rolled-back workspace: {exc}")
    return errors


def _verify_precommit_state(
    workspace: MangaWorkspacePaths, journal: Mapping[str, object]
) -> None:
    if _file_digest(workspace.editing) != journal["old_editing_digest"]:
        raise ExportConflict("Editing metadata changed while export was being prepared.")
    old_output_digest = journal["old_output_digest"]
    if not _tree_matches(workspace.output, old_output_digest):
        raise ExportConflict("Manga output changed while export was being prepared.")


def _load_journal(
    path: Path, manga_name: str, transaction_id: str
) -> dict[str, object]:
    if is_link_or_reparse(path) or not path.is_file():
        raise ExportRecoveryError(f"Export journal '{path}' is not a safe file.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportRecoveryError(f"Export journal '{path}' is unreadable: {exc}") from exc
    expected_keys = {
        "schema_version",
        "kind",
        "transaction_id",
        "manga",
        "phase",
        "created_at",
        "replace_output",
        "replace_editing",
        "had_output",
        "new_output_present",
        "old_output_digest",
        "new_output_digest",
        "had_editing",
        "old_editing_digest",
        "new_editing_digest",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload.get("schema_version") != EXPORT_TRANSACTION_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != "manga-export"
        or payload.get("transaction_id") != transaction_id
        or payload.get("manga") != manga_name
        or payload.get("phase") not in {"prepared", "committed"}
        or not isinstance(payload.get("created_at"), str)
        or payload.get("replace_output") is not True
        or payload.get("replace_editing") is not True
        or not isinstance(payload.get("had_output"), bool)
        or not isinstance(payload.get("new_output_present"), bool)
        or not isinstance(payload.get("had_editing"), bool)
        or not _optional_digest(payload.get("old_output_digest"))
        or not _optional_digest(payload.get("new_output_digest"))
        or not _optional_digest(payload.get("old_editing_digest"))
        or not _required_digest(payload.get("new_editing_digest"))
    ):
        raise ExportRecoveryError(f"Export journal '{path}' has an invalid format.")
    if payload["had_output"] != (payload["old_output_digest"] is not None):
        raise ExportRecoveryError(f"Export journal '{path}' has inconsistent output state.")
    if payload["new_output_present"] != (
        payload["new_output_digest"] is not None
    ):
        raise ExportRecoveryError(f"Export journal '{path}' has inconsistent new output state.")
    if payload["had_editing"] != (payload["old_editing_digest"] is not None):
        raise ExportRecoveryError(f"Export journal '{path}' has inconsistent editing state.")
    return payload


def _discard_markerless_transaction(transaction: Path) -> None:
    allowed = {"new-output", "new-editing.json"}
    try:
        entries = tuple(transaction.iterdir())
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not inspect markerless export staging: {exc}"
        ) from exc
    for entry in entries:
        if entry.name in {"old-output", "old-editing.json"}:
            raise ExportRecoveryError(
                f"Markerless export staging '{transaction}' may contain active data."
            )
        if entry.name not in allowed and not (
            entry.name.startswith(f".{_JOURNAL_NAME}.")
            and entry.name.endswith(".tmp")
        ):
            raise ExportRecoveryError(
                f"Markerless export staging '{transaction}' contains unknown data."
            )
    try:
        remove_managed_path(transaction)
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not discard markerless export staging: {exc}"
        ) from exc


def _retire_export_transaction(transaction: Path, transaction_root: Path) -> None:
    """Remove payload first, then retire/delete recoverability markers last."""

    _require_transaction_directory(transaction, transaction_root)
    try:
        entries = _validated_export_cleanup_entries(transaction, allow_payload=True)
        for name in (
            "new-output",
            "new-editing.json",
            "old-output",
            "old-editing.json",
        ):
            if name in entries:
                remove_managed_path(transaction / name)

        _validated_export_cleanup_entries(transaction, allow_payload=False)
        if not transaction.name.startswith(_EXPORT_TRANSACTION_PREFIX):
            raise ExportRecoveryError(
                f"Export staging has an invalid transaction name: '{transaction}'."
            )
        retired = transaction_root / (
            _RETIRED_EXPORT_TRANSACTION_PREFIX
            + transaction.name.removeprefix(_EXPORT_TRANSACTION_PREFIX)
        )
        rename_no_replace(transaction, retired)
        _fsync_directory(transaction_root)
        _remove_retired_export_transaction(retired, transaction_root)
    except ExportError:
        raise
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not safely retire export staging '{transaction.name}': {exc}"
        ) from exc


def _remove_retired_export_transaction(retired: Path, transaction_root: Path) -> None:
    """Delete a payload-free retired transaction; partial marker deletion is safe."""

    _require_transaction_directory(retired, transaction_root)
    _validated_export_cleanup_entries(retired, allow_payload=False)
    try:
        remove_managed_path(retired)
        _fsync_directory(transaction_root)
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not remove retired export staging '{retired.name}': {exc}"
        ) from exc


def _validated_export_cleanup_entries(
    transaction: Path, *, allow_payload: bool
) -> dict[str, Path]:
    payload_names = {
        "new-output",
        "new-editing.json",
        "old-output",
        "old-editing.json",
    }
    try:
        entries = tuple(transaction.iterdir())
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not inspect export cleanup staging: {exc}"
        ) from exc
    result: dict[str, Path] = {}
    for entry in entries:
        marker = entry.name == _JOURNAL_NAME or (
            entry.name.startswith(f".{_JOURNAL_NAME}.")
            and entry.name.endswith(".tmp")
        )
        if entry.name in payload_names:
            if not allow_payload:
                raise ExportRecoveryError(
                    f"Retired export staging '{transaction}' still contains payload data."
                )
        elif marker:
            if is_link_or_reparse(entry) or not entry.is_file():
                raise ExportRecoveryError(
                    f"Export cleanup marker is not a safe file: '{entry}'."
                )
        else:
            raise ExportRecoveryError(
                f"Export staging '{transaction}' contains unknown cleanup data."
            )
        if entry.name in result:
            raise ExportRecoveryError(
                f"Export staging '{transaction}' contains duplicate cleanup data."
            )
        result[entry.name] = entry
    return result


def _validate_safe_tree(root: Path) -> dict[str, Path]:
    if is_link_or_reparse(root) or not root.is_dir():
        raise ExportError(f"Managed directory '{root}' is not a safe directory.")
    try:
        root_device = root.stat(follow_symlinks=False).st_dev
        if root_device != root.parent.stat(follow_symlinks=False).st_dev:
            raise ExportError(
                f"Managed output is a mounted filesystem boundary: '{root}'."
            )
    except OSError as exc:
        raise ExportError(f"Could not inspect managed output '{root}': {exc}") from exc
    paths: dict[str, Path] = {}

    def fail(exc: OSError) -> None:
        raise exc

    try:
        for current_text, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False, onerror=fail
        ):
            current = Path(current_text)
            for name in (*directory_names, *file_names):
                path = current / name
                if is_link_or_reparse(path):
                    raise ExportError(
                        f"Managed output cannot contain links: '{path}'."
                    )
                information = path.stat(follow_symlinks=False)
                if information.st_dev != root_device:
                    raise ExportError(
                        f"Managed output crosses a mounted filesystem boundary: '{path}'."
                    )
                if not (
                    stat.S_ISDIR(information.st_mode)
                    or stat.S_ISREG(information.st_mode)
                ):
                    raise ExportError(
                        f"Managed output contains an unsupported entry: '{path}'."
                    )
                relative = path.relative_to(root)
                key = _path_key(relative)
                existing = paths.get(key)
                if existing is not None and existing != relative:
                    raise ExportConflict(
                        f"Output paths '{existing}' and '{relative}' collide."
                    )
                paths[key] = relative
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"Could not inspect managed output '{root}': {exc}") from exc
    return paths


def _tree_digest(path: Path) -> str:
    entries = _validate_safe_tree(path)
    digest = hashlib.sha256()
    for relative in sorted(entries.values(), key=lambda value: value.as_posix()):
        target = path / relative
        information = target.stat(follow_symlinks=False)
        kind = b"d" if stat.S_ISDIR(information.st_mode) else b"f"
        encoded = relative.as_posix().encode("utf-8", "surrogatepass")
        digest.update(kind)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        if kind == b"f":
            digest.update(bytes.fromhex(_sha256(target)))
    return digest.hexdigest()


def _fsync_tree(path: Path) -> None:
    """Make every staged file and directory entry durable before journal commit."""

    entries = _validate_safe_tree(path)
    directories = [path]
    for relative in entries.values():
        target = path / relative
        information = target.stat(follow_symlinks=False)
        if stat.S_ISDIR(information.st_mode):
            directories.append(target)
        else:
            _fsync_file(target)
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        _fsync_directory(directory)


def _fsync_file(path: Path) -> None:
    # On Windows os.fsync() calls the CRT _commit(), which rejects a
    # read-only descriptor with EBADF.  Staged copies may also retain a
    # read-only attribute, so make only the staged file temporarily writable
    # and restore its original mode after the flush.
    original_mode: int | None = None
    if _WINDOWS_FILE_SYNC:
        information = path.stat(follow_symlinks=False)
        current_mode = stat.S_IMODE(information.st_mode)
        if not current_mode & stat.S_IWRITE:
            original_mode = current_mode
            os.chmod(path, current_mode | stat.S_IWRITE)

    flags = os.O_RDWR if _WINDOWS_FILE_SYNC else os.O_RDONLY
    if _WINDOWS_FILE_SYNC and hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            if original_mode is not None:
                os.chmod(path, original_mode)


def _tree_matches(path: Path, expected_digest: object) -> bool:
    if expected_digest is None:
        return not os.path.lexists(path)
    if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
        return False
    if not os.path.lexists(path):
        return False
    try:
        return _tree_digest(path) == expected_digest
    except (OSError, ExportError):
        return False


def _require_regular_file(path: Path, parent: Path) -> None:
    if not os.path.lexists(path):
        raise ExportError(f"Managed output file is missing: '{path}'.")
    if is_link_or_reparse(path):
        raise ExportError(f"Managed output file cannot be a link: '{path}'.")
    try:
        information = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ExportError(f"Could not inspect output file '{path}': {exc}") from exc
    if not stat.S_ISREG(information.st_mode) or resolved.parent != resolved_parent:
        raise ExportError(f"Managed output is not a safe regular file: '{path}'.")


def _copy_source_image(folder: FolderRef, image: ImageRef, destination: Path) -> None:
    source = Path(image.path)
    descriptor = -1
    temporary_descriptor = -1
    temporary: Path | None = None
    try:
        resolved_folder = Path(folder.path).resolve(strict=True)
        if is_link_or_reparse(source):
            raise OSError("source is a link")
        before = source.stat(follow_symlinks=False)
        resolved_source = source.resolve(strict=True)
        if (
            not stat.S_ISREG(before.st_mode)
            or resolved_source.parent != resolved_folder
            or resolved_source.name != image.name
        ):
            raise OSError("source is no longer a safe direct image")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved_source, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise OSError("source changed before it could be copied")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".copying", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "rb") as source_handle:
                descriptor = -1
                with os.fdopen(temporary_descriptor, "wb") as destination_handle:
                    temporary_descriptor = -1
                    shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            after = source.stat(follow_symlinks=False)
            if not os.path.samestat(opened, after):
                raise OSError("source changed while it was copied")
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ExportError(
            f"Could not safely copy source image '{folder.name}/{image.name}': {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)


def _sha256_source(folder: FolderRef, image: ImageRef) -> str:
    source = Path(image.path)
    descriptor = -1
    try:
        resolved_folder = Path(folder.path).resolve(strict=True)
        if is_link_or_reparse(source):
            raise OSError("source is a link or reparse point")
        resolved = source.resolve(strict=True)
        before = source.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or resolved.parent != resolved_folder
            or resolved.name != image.name
        ):
            raise OSError("source is not a safe direct image")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or not os.path.samestat(before, opened_before)
        ):
            raise OSError("source changed before it could be hashed")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            opened_after = os.fstat(handle.fileno())
        after = source.stat(follow_symlinks=False)
        if (
            not os.path.samestat(opened_before, opened_after)
            or not os.path.samestat(opened_after, after)
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        ):
            raise OSError("source changed while it was hashed")
        return digest.hexdigest()
    except OSError as exc:
        raise ExportError(
            f"Could not verify source image '{folder.name}/{image.name}': {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digest(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    if is_link_or_reparse(path) or not path.is_file():
        raise ExportRecoveryError(f"Expected regular file is unsafe: '{path}'.")
    try:
        return _sha256(path)
    except OSError as exc:
        raise ExportRecoveryError(f"Could not digest '{path}': {exc}") from exc


def _validate_destination_path(
    workspace: MangaWorkspacePaths, relative: Path
) -> None:
    if len(relative.parts) != 2 or any(
        not _safe_component(component) for component in relative.parts
    ):
        raise ExportError(f"Unsafe output destination: '{relative}'.")
    base = workspace.workspace
    name_max = _path_limit(base, "PC_NAME_MAX", 255)
    for component in relative.parts:
        if len(os.fsencode(component)) > name_max:
            raise ExportError(
                f"Output path component is too long for this filesystem: '{component}'."
            )
    destination = workspace.output / relative
    path_max = _path_limit(
        base, "PC_PATH_MAX", 32767 if os.name == "nt" else 4096
    )
    if len(os.fsencode(destination)) > path_max:
        raise ExportError(f"Output path is too long: '{destination}'.")


def _path_limit(base: Path, name: str, default: int) -> int:
    """Read a POSIX path limit when supported, otherwise use a safe default."""

    pathconf = getattr(os, "pathconf", None)
    if pathconf is None:
        return default
    try:
        value = pathconf(base, name)
    except (OSError, TypeError, ValueError):
        return default
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Install a directory atomically without replacing a late destination."""

    try:
        rename_no_replace(source, destination)
    except OSError as exc:
        if os.path.lexists(destination) or exc.errno in {
            errno.EEXIST,
            errno.ENOTEMPTY,
        }:
            raise ExportConflict(
                f"Export destination appeared during the transaction: '{destination}'."
            ) from exc
        raise
    if os.path.lexists(source) or not os.path.lexists(destination):
        raise ExportRecoveryError(
            f"Could not verify installed export destination '{destination}'."
        )


def _record_collision(
    seen: dict[str, tuple[str, str]],
    relative: Path,
    owner: tuple[str, str],
) -> None:
    key = _path_key(relative)
    previous = seen.get(key)
    if previous is not None and previous != owner:
        raise ExportConflict(
            f"Images '{previous[0]}/{previous[1]}' and '{owner[0]}/{owner[1]}' "
            f"would collide at '{relative}'."
        )
    seen[key] = owner


def _record_folder_collision(seen: dict[str, str], folder_name: str) -> None:
    key = folder_name.casefold()
    previous = seen.get(key)
    if previous is not None and previous != folder_name:
        raise ExportConflict(
            f"Output folders '{previous}' and '{folder_name}' collide on "
            "case-insensitive filesystems."
        )
    seen[key] = folder_name


def _path_key(path: Path) -> str:
    return path.as_posix().casefold()


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and os.sep not in value
        and (os.altsep is None or os.altsep not in value)
        and "\x00" not in value
        and Path(value).name == value
    )


def _optional_digest(value: object) -> bool:
    return value is None or _required_digest(value)


def _required_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _require_transaction_directory(path: Path, parent: Path) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise ExportRecoveryError(f"Export staging '{path}' is not a safe directory.")
    try:
        if path.resolve(strict=True).parent != parent.resolve(strict=True):
            raise ExportRecoveryError(f"Export staging '{path}' escapes its workspace.")
    except OSError as exc:
        raise ExportRecoveryError(f"Could not resolve export staging '{path}': {exc}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # Windows does not support opening directories this way.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
