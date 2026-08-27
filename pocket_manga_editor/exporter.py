"""Crash-safe, whole-manga export of the current sparse selections.

Every export builds a new output tree from source.  The previous output is
never used as input and editing metadata is never changed by an export.  A
small, output-only journal exists solely to restore the previous directory if
the two directory renames are interrupted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Mapping

from .filesystem_ops import remove_managed_path, rename_no_replace
from .library_lock import LibraryBusyError, LibraryLockError, library_mutation_lock
from .models import FolderRef, ImageRef, MangaRef
from .path_safety import is_link_or_reparse
from .scanner import natural_name_key
from .storage import EditingStore, atomic_write_json
from .workspace import (
    MangaWorkspacePaths,
    WorkspaceError,
    manga_workspace_paths,
    validate_export_workspace,
    validate_live_manga,
    validate_output_workspace,
)


EXPORT_TRANSACTION_SCHEMA_VERSION = 2
_EXPORT_TRANSACTION_PREFIX = "export-"
_JOURNAL_NAME = "transaction.json"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_WINDOWS_FILE_SYNC = os.name == "nt"


class ExportError(RuntimeError):
    """Raised when a manga export cannot be completed safely."""


class NothingSelectedError(ExportError):
    """Raised when export is requested without any selected images."""


class ExportConflict(ExportError):
    """Raised when source or output changes during an export transaction."""


class ExportBusyError(ExportError):
    """Raised when another library mutation currently owns the global lock."""


class ExportRecoveryError(ExportError):
    """Raised when interrupted export state cannot be reconciled safely."""

    state_may_have_changed = True


class ExportConfirmationRequired(ExportError):
    """Raised before replacing output containing unrecognized entries."""

    def __init__(self, preview: "ExportPreview") -> None:
        self.preview = preview
        super().__init__(
            "Existing output contains entries that do not match the current source. "
            "Confirm that the entire output directory may be replaced."
        )


@dataclass(frozen=True, slots=True)
class ExportPreview:
    output_directory: Path
    selected_folder_count: int
    selected_image_count: int
    output_exists: bool
    unrecognized_entries: tuple[str, ...]

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.unrecognized_entries)


@dataclass(frozen=True, slots=True)
class MangaExportResult:
    output_directory: Path
    folder_count: int
    image_count: int
    warnings: tuple[str, ...] = ()


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
    relative: Path
    source_digest: str | None = None


def exported_image_name(folder_name: str, image_name: str) -> str:
    """Return the deterministic output filename for one source image."""

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


def inspect_export(
    working_directory: str | Path, manga: MangaRef
) -> ExportPreview:
    """Describe a prospective full replacement without changing output."""

    try:
        with library_mutation_lock(working_directory) as root:
            recover_interrupted_exports_locked(root)
            return _inspect_export_locked(root, manga)
    except LibraryBusyError as exc:
        raise ExportBusyError(str(exc)) from exc
    except (LibraryLockError, WorkspaceError) as exc:
        raise ExportError(str(exc)) from exc


def export_manga(
    working_directory: str | Path,
    manga: MangaRef,
    *,
    confirm_unrecognized_output: bool = False,
) -> MangaExportResult:
    """Replace manga output with exactly the images currently selected."""

    try:
        with library_mutation_lock(working_directory) as root:
            recover_interrupted_exports_locked(root)
            return _export_manga_locked(
                root,
                manga,
                confirm_unrecognized_output=confirm_unrecognized_output,
            )
    except LibraryBusyError as exc:
        raise ExportBusyError(str(exc)) from exc
    except (LibraryLockError, WorkspaceError) as exc:
        raise ExportError(str(exc)) from exc


def recover_interrupted_exports(
    working_directory: str | Path,
) -> ExportRecoveryResult:
    """Reconcile every durable output-only export journal."""

    try:
        with library_mutation_lock(working_directory) as root:
            return recover_interrupted_exports_locked(root)
    except LibraryBusyError as exc:
        raise ExportBusyError(str(exc)) from exc
    except (LibraryLockError, WorkspaceError) as exc:
        raise ExportRecoveryError(str(exc)) from exc


def _inspect_export_locked(root: Path, manga: MangaRef) -> ExportPreview:
    workspace = manga_workspace_paths(root, manga.name)
    editing = EditingStore(root)._load_locked(root, manga)
    desired = _selected_images(workspace, manga, editing.folders)
    output_exists = os.path.lexists(workspace.output)

    # Zero-selection export is a direct refusal.  In particular, do not ask
    # the user to reason about old output that this operation will not touch.
    if not desired:
        return ExportPreview(workspace.output, 0, 0, output_exists, ())

    validate_live_manga(root, manga)
    validate_export_workspace(workspace)
    unrecognized = _unrecognized_output_entries(workspace, manga)
    return ExportPreview(
        workspace.output,
        len({item.folder.name for item in desired}),
        len(desired),
        output_exists,
        unrecognized,
    )


def _export_manga_locked(
    root: Path,
    manga: MangaRef,
    *,
    confirm_unrecognized_output: bool,
) -> MangaExportResult:
    preview = _inspect_export_locked(root, manga)
    if preview.selected_image_count == 0:
        raise NothingSelectedError("Nothing selected.")
    if preview.unrecognized_entries and not confirm_unrecognized_output:
        raise ExportConfirmationRequired(preview)

    workspace = manga_workspace_paths(root, manga.name)
    validate_export_workspace(workspace)
    editing = EditingStore(root)._load_locked(root, manga)
    desired = list(_selected_images(workspace, manga, editing.folders))
    if not desired:
        raise NothingSelectedError("Nothing selected.")

    try:
        workspace.workspace.mkdir(parents=False, exist_ok=True)
        workspace.transactions.mkdir(parents=False, exist_ok=True)
        _fsync_directory(workspace.workspace)
    except OSError as exc:
        raise ExportError(
            f"Could not create the export transaction workspace: {exc}"
        ) from exc
    workspace = validate_export_workspace(manga_workspace_paths(root, manga.name))

    try:
        transaction = Path(
            tempfile.mkdtemp(
                prefix=_EXPORT_TRANSACTION_PREFIX, dir=workspace.transactions
            )
        )
    except OSError as exc:
        raise ExportError(f"Could not create an export transaction: {exc}") from exc

    new_output = transaction / "new-output"
    old_output = transaction / "old-output"
    journal_path = transaction / _JOURNAL_NAME
    journal: dict[str, object] | None = None
    committed = False

    try:
        # Snapshot active output before the potentially long copy.  The same
        # digest is checked again immediately before the first rename, so a
        # manual filesystem change cannot slip past the preview/confirmation.
        had_output = os.path.lexists(workspace.output)
        old_output_digest = _tree_digest(workspace.output) if had_output else None

        new_output.mkdir()
        copied: list[_DesiredImage] = []
        for item in desired:
            target = new_output / item.relative
            digest = _copy_source_image(item.folder, item.image, target)
            copied.append(replace(item, source_digest=digest))
        desired = copied
        _fsync_tree(new_output)
        new_output_digest = _tree_digest(new_output)

        journal = {
            "schema_version": EXPORT_TRANSACTION_SCHEMA_VERSION,
            "kind": "manga-export",
            "transaction_id": transaction.name,
            "manga": manga.name,
            "phase": "prepared",
            "had_output": had_output,
            "old_output_digest": old_output_digest,
            "new_output_digest": new_output_digest,
        }
        atomic_write_json(journal_path, journal)
        _fsync_directory(transaction)
        _fsync_directory(workspace.transactions)

        _revalidate_sources(desired)
        if not _tree_matches(workspace.output, old_output_digest):
            raise ExportConflict("Manga output changed while export was being prepared.")

        if had_output:
            _rename_no_replace(workspace.output, old_output)
            if not _tree_matches(old_output, old_output_digest):
                raise ExportConflict(
                    "Manga output changed immediately before it was staged."
                )
        _rename_no_replace(new_output, workspace.output)
        if not _tree_matches(workspace.output, new_output_digest):
            raise ExportConflict("New manga output changed while it was installed.")
        _fsync_directory(workspace.workspace)
        _fsync_directory(transaction)

        journal["phase"] = "committed"
        atomic_write_json(journal_path, journal)
        committed = True

        result = MangaExportResult(
            workspace.output,
            preview.selected_folder_count,
            preview.selected_image_count,
        )
        warnings: list[str] = []
        try:
            _fsync_directory(transaction)
            _cleanup_transaction(transaction, workspace.transactions)
        except (OSError, ExportError) as exc:
            warnings.append(f"Export committed, but temporary cleanup is pending: {exc}")
        return replace(result, warnings=tuple(warnings))
    except BaseException as exc:
        if committed:
            if isinstance(exc, Exception):
                return MangaExportResult(
                    workspace.output,
                    preview.selected_folder_count,
                    preview.selected_image_count,
                    (f"Export committed, but temporary cleanup is pending: {exc}",),
                )
            raise

        rollback_errors: list[str] = []
        if journal is not None:
            rollback_errors = _rollback_prepared(workspace, transaction, journal)
        if not rollback_errors:
            try:
                _cleanup_transaction(transaction, workspace.transactions)
            except (OSError, ExportError) as cleanup_exc:
                rollback_errors.append(
                    f"could not remove export staging: {cleanup_exc}"
                )
        if rollback_errors:
            raise ExportRecoveryError(
                "Export failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, ExportError):
            raise
        if isinstance(exc, OSError):
            raise ExportError(f"Could not commit the manga export: {exc}") from exc
        raise


def _selected_images(
    workspace: MangaWorkspacePaths,
    manga: MangaRef,
    folder_states: Mapping[str, object],
) -> tuple[_DesiredImage, ...]:
    desired: list[_DesiredImage] = []
    seen_targets: dict[str, tuple[str, str]] = {}
    seen_folders: dict[str, str] = {}
    for folder in manga.folders:
        state = folder_states.get(folder.name)
        selected = getattr(state, "selected_images", frozenset())
        if not selected:
            continue
        _record_folder_collision(seen_folders, folder.name)
        for image in folder.images:
            if image.name not in selected:
                continue
            relative = Path(folder.name) / exported_image_name(
                folder.name, image.name
            )
            _validate_destination_path(workspace, relative)
            _record_collision(seen_targets, relative, (folder.name, image.name))
            desired.append(_DesiredImage(folder, image, relative))
    return tuple(desired)


def _unrecognized_output_entries(
    workspace: MangaWorkspacePaths, manga: MangaRef
) -> tuple[str, ...]:
    if not os.path.lexists(workspace.output):
        return ()
    entries = _validate_safe_tree(workspace.output)

    allowed_folders: dict[str, str] = {}
    allowed_files: dict[str, Path] = {}
    for folder in manga.folders:
        _record_folder_collision(allowed_folders, folder.name)
        for image in folder.images:
            relative = Path(folder.name) / exported_image_name(
                folder.name, image.name
            )
            key = _path_key(relative)
            previous = allowed_files.get(key)
            if previous is not None and previous != relative:
                raise ExportConflict(
                    f"Source output paths '{previous}' and '{relative}' collide."
                )
            allowed_files[key] = relative

    unrecognized: list[str] = []
    for relative in entries.values():
        target = workspace.output / relative
        information = target.stat(follow_symlinks=False)
        if stat.S_ISDIR(information.st_mode):
            recognized = (
                len(relative.parts) == 1
                and allowed_folders.get(relative.name.casefold()) == relative.name
            )
        else:
            recognized = (
                len(relative.parts) == 2
                and allowed_files.get(_path_key(relative)) == relative
            )
        if not recognized:
            suffix = "/" if stat.S_ISDIR(information.st_mode) else ""
            unrecognized.append(relative.as_posix() + suffix)
    return tuple(sorted(unrecognized, key=natural_name_key))


def recover_interrupted_exports_locked(root: Path) -> ExportRecoveryResult:
    """Reconcile export journals while the caller owns the library lock."""

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
        raise ExportRecoveryError(
            f"Could not inspect export transactions: {exc}"
        ) from exc

    committed_count = 0
    rolled_back_count = 0
    discarded_count = 0
    for candidate in workspace_candidates:
        if candidate.name == ".library-mutation.lock":
            continue
        if is_link_or_reparse(candidate):
            raise ExportRecoveryError(f"Manga workspace '{candidate}' cannot be a link.")
        if not candidate.is_dir():
            continue
        try:
            workspace = manga_workspace_paths(root, candidate.name)
        except WorkspaceError as exc:
            raise ExportRecoveryError(str(exc)) from exc
        if not os.path.lexists(workspace.transactions):
            continue
        if is_link_or_reparse(workspace.transactions) or not workspace.transactions.is_dir():
            raise ExportRecoveryError(
                f"Export transaction workspace for '{candidate.name}' is unsafe."
            )
        try:
            entries = sorted(workspace.transactions.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise ExportRecoveryError(
                f"Could not inspect export staging for '{candidate.name}': {exc}"
            ) from exc

        for transaction in entries:
            if not transaction.name.startswith(_EXPORT_TRANSACTION_PREFIX):
                raise ExportRecoveryError(
                    f"Transaction workspace contains unsupported data: '{transaction}'."
                )
            _require_transaction_directory(transaction, workspace.transactions)
            journal_path = transaction / _JOURNAL_NAME
            if not os.path.lexists(journal_path):
                _discard_markerless_transaction(transaction)
                discarded_count += 1
                continue
            journal = _load_journal(journal_path, candidate.name, transaction.name)
            if journal["phase"] == "committed":
                if not _tree_matches(workspace.output, journal["new_output_digest"]):
                    raise ExportRecoveryError(
                        f"Committed export '{transaction.name}' output cannot be verified."
                    )
                _cleanup_transaction(transaction, workspace.transactions)
                committed_count += 1
                continue

            errors = _rollback_prepared(workspace, transaction, journal)
            if errors:
                raise ExportRecoveryError(
                    "Interrupted export rollback was incomplete: " + "; ".join(errors)
                )
            _cleanup_transaction(transaction, workspace.transactions)
            rolled_back_count += 1

    return ExportRecoveryResult(committed_count, rolled_back_count, discarded_count)


def _rollback_prepared(
    workspace: MangaWorkspacePaths,
    transaction: Path,
    journal: Mapping[str, object],
) -> list[str]:
    errors: list[str] = []
    old_output = transaction / "old-output"
    new_output = transaction / "new-output"
    expected_old = journal.get("old_output_digest")
    expected_new = journal.get("new_output_digest")
    had_output = bool(journal.get("had_output"))

    try:
        old_staged = os.path.lexists(old_output)
        active_matches_old = _tree_matches(workspace.output, expected_old)
        active_matches_new = _tree_matches(workspace.output, expected_new)

        if old_staged:
            if not had_output or not _tree_matches(old_output, expected_old):
                raise OSError("staged original output does not match its journal")
            if os.path.lexists(workspace.output):
                if not active_matches_new:
                    raise OSError("active output changed before rollback")
                if os.path.lexists(new_output):
                    raise OSError("both staged and active new output are present")
                _rename_no_replace(workspace.output, new_output)
            _rename_no_replace(old_output, workspace.output)
        elif had_output:
            if not active_matches_old:
                raise OSError("the original active output no longer matches its journal")
        elif os.path.lexists(workspace.output):
            if not active_matches_new:
                raise OSError("unexpected active output prevents rollback")
            if os.path.lexists(new_output):
                raise OSError("both staged and active new output are present")
            _rename_no_replace(workspace.output, new_output)

        # Once the original output has been verified/restored (or absence has
        # been restored for a first export), ``new-output`` is only disposable
        # staging.  It may be incomplete after an interrupted cleanup; making
        # recovery depend on its old digest would strand an otherwise complete
        # rollback forever.  Cleanup still validates the tree and refuses
        # links, reparse points, mounts, and special files before deleting it.
        _fsync_directory(workspace.workspace)
        _fsync_directory(transaction)
    except (OSError, ExportError) as exc:
        errors.append(f"could not restore output: {exc}")
    return errors


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
        "had_output",
        "old_output_digest",
        "new_output_digest",
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
        or not isinstance(payload.get("had_output"), bool)
        or not _optional_digest(payload.get("old_output_digest"))
        or not _required_digest(payload.get("new_output_digest"))
    ):
        raise ExportRecoveryError(f"Export journal '{path}' has an invalid format.")
    if payload["had_output"] != (payload["old_output_digest"] is not None):
        raise ExportRecoveryError(
            f"Export journal '{path}' has inconsistent output state."
        )
    return payload


def _discard_markerless_transaction(transaction: Path) -> None:
    try:
        entries = tuple(transaction.iterdir())
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not inspect markerless export staging: {exc}"
        ) from exc
    for entry in entries:
        if entry.name == "old-output":
            raise ExportRecoveryError(
                f"Markerless export staging '{transaction}' may contain active data."
            )
        if entry.name != "new-output" and not _is_journal_temporary(entry):
            raise ExportRecoveryError(
                f"Markerless export staging '{transaction}' contains unknown data."
            )
        if entry.name == "new-output":
            _validate_safe_tree(entry)
        elif is_link_or_reparse(entry) or not entry.is_file():
            raise ExportRecoveryError(f"Export staging marker is unsafe: '{entry}'.")
    _cleanup_transaction(transaction, transaction.parent)


def _cleanup_transaction(transaction: Path, transaction_root: Path) -> None:
    """Delete payload first and the recovery marker last."""

    _require_transaction_directory(transaction, transaction_root)
    try:
        entries = tuple(transaction.iterdir())
    except OSError as exc:
        raise ExportRecoveryError(
            f"Could not inspect export cleanup staging: {exc}"
        ) from exc
    allowed = {"new-output", "old-output", _JOURNAL_NAME}
    for entry in entries:
        if entry.name not in allowed and not _is_journal_temporary(entry):
            raise ExportRecoveryError(
                f"Export staging '{transaction}' contains unknown cleanup data."
            )
        if entry.name in {"new-output", "old-output"}:
            _validate_safe_tree(entry)
        elif is_link_or_reparse(entry) or not entry.is_file():
            raise ExportRecoveryError(
                f"Export cleanup marker is not a safe file: '{entry}'."
            )

    for name in ("new-output", "old-output"):
        payload = transaction / name
        if os.path.lexists(payload):
            remove_managed_path(payload)
    for entry in tuple(transaction.iterdir()):
        if entry.name == _JOURNAL_NAME or _is_journal_temporary(entry):
            entry.unlink()
    transaction.rmdir()
    _fsync_directory(transaction_root)


def _is_journal_temporary(path: Path) -> bool:
    return path.name.startswith(f".{_JOURNAL_NAME}.") and path.name.endswith(".tmp")


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
                    raise ExportError(f"Managed output cannot contain links: '{path}'.")
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


def _tree_matches(path: Path, expected_digest: object) -> bool:
    if expected_digest is None:
        return not os.path.lexists(path)
    if not _required_digest(expected_digest) or not os.path.lexists(path):
        return False
    try:
        return _tree_digest(path) == expected_digest
    except (OSError, ExportError):
        return False


def _fsync_tree(path: Path) -> None:
    entries = _validate_safe_tree(path)
    directories = [path]
    for relative in entries.values():
        target = path / relative
        information = target.stat(follow_symlinks=False)
        if stat.S_ISDIR(information.st_mode):
            directories.append(target)
        else:
            _fsync_file(target)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _fsync_file(path: Path) -> None:
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


def _copy_source_image(folder: FolderRef, image: ImageRef, destination: Path) -> str:
    source = Path(image.path)
    source_descriptor = -1
    temporary_descriptor = -1
    temporary: Path | None = None
    try:
        resolved_folder = Path(folder.path).resolve(strict=True)
        if is_link_or_reparse(source):
            raise OSError("source is a link or reparse point")
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
        source_descriptor = os.open(resolved_source, flags)
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise OSError("source changed before it could be copied")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".copying", dir=destination.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        with os.fdopen(source_descriptor, "rb") as source_handle:
            source_descriptor = -1
            with os.fdopen(temporary_descriptor, "wb") as destination_handle:
                temporary_descriptor = -1
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            opened_after = os.fstat(source_handle.fileno())
        after = source.stat(follow_symlinks=False)
        if (
            not os.path.samestat(opened, opened_after)
            or not os.path.samestat(opened_after, after)
            or opened.st_size != opened_after.st_size
            or opened.st_mtime_ns != opened_after.st_mtime_ns
        ):
            raise OSError("source changed while it was copied")
        os.replace(temporary, destination)
        return digest.hexdigest()
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ExportError(
            f"Could not safely copy source image '{folder.name}/{image.name}': {exc}"
        ) from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)


def _revalidate_sources(desired: list[_DesiredImage]) -> None:
    for item in desired:
        if _sha256_source(item.folder, item.image) != item.source_digest:
            raise ExportConflict(
                f"Source image '{item.folder.name}/{item.image.name}' changed while "
                "the manga export was being prepared."
            )


def _sha256_source(folder: FolderRef, image: ImageRef) -> str:
    source = Path(image.path)
    descriptor = -1
    try:
        resolved_folder = Path(folder.path).resolve(strict=True)
        if is_link_or_reparse(source):
            raise OSError("source is a link or reparse point")
        before = source.stat(follow_symlinks=False)
        resolved = source.resolve(strict=True)
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
        if not stat.S_ISREG(opened_before.st_mode) or not os.path.samestat(
            before, opened_before
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


def _validate_destination_path(workspace: MangaWorkspacePaths, relative: Path) -> None:
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
    path_max = _path_limit(base, "PC_PATH_MAX", 32767 if os.name == "nt" else 4096)
    if len(os.fsencode(destination)) > path_max:
        raise ExportError(f"Output path is too long: '{destination}'.")


def _path_limit(base: Path, name: str, default: int) -> int:
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
    try:
        rename_no_replace(source, destination)
    except OSError as exc:
        if os.path.lexists(destination) or exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ExportConflict(
                f"Export destination appeared during the transaction: '{destination}'."
            ) from exc
        raise
    if os.path.lexists(source) or not os.path.lexists(destination):
        raise ExportRecoveryError(
            f"Could not verify installed export destination '{destination}'."
        )


def _record_collision(
    seen: dict[str, tuple[str, str]], relative: Path, owner: tuple[str, str]
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
        raise ExportRecoveryError(
            f"Could not resolve export staging '{path}': {exc}"
        ) from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
