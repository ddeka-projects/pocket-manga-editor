from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from pocket_manga_editor import __version__
from pocket_manga_editor.completion import (
    COMMITTED_MARKER_FILENAME,
    TRANSACTION_DIRECTORY_PREFIX,
    TRANSACTION_MARKER_FILENAME,
    TRANSACTION_SCHEMA_VERSION,
    CompletionBusyError,
    CompletionChangedError,
    CompletionError,
    CompletionRecoveryError,
    analyze_completion,
    complete_manga,
    recover_interrupted_completions,
)
from pocket_manga_editor.exporter import ExportError, ExportRecoveryError, export_manga
from pocket_manga_editor.library_lock import library_mutation_lock
from pocket_manga_editor.scanner import scan_working_directory
from pocket_manga_editor.storage import EditingStore, ReadingStore, atomic_write_json
from pocket_manga_editor.workspace import manga_workspace_paths


def _hold_library_lock_in_child(
    root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with library_mutation_lock(root):
        ready.set()
        release.wait(10)


class CompletionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_source_image(
        self,
        folder: str = "Part 1 - Dawn",
        image: str = "page 1.JPG",
        *,
        manga: str = "Series",
        contents: bytes | None = None,
    ) -> Path:
        path = self.root / manga / folder / image
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents or f"{manga}:{folder}:{image}".encode())
        return path

    def manga(self, name: str = "Series"):
        return next(
            manga
            for manga in scan_working_directory(self.root).mangas
            if manga.name == name
        )

    def workspace(self, name: str = "Series"):
        return manga_workspace_paths(self.root, name)

    def export(
        self,
        selections: dict[str, tuple[str, ...]] | None = None,
        *,
        manga_name: str = "Series",
    ):
        manga = self.manga(manga_name)
        store = EditingStore(self.root)
        if selections is None:
            first_folder = manga.folders[0]
            selections = {first_folder.name: (first_folder.images[0].name,)}
        by_name = {folder.name: folder for folder in manga.folders}
        for folder_name, images in selections.items():
            folder = by_name[folder_name]
            store.save_folder(
                manga,
                folder_name,
                folder.images[0].name,
                images,
            )
        export_manga(self.root, manga)
        return self.manga(manga_name)

    def create_transaction(
        self,
        *,
        manga_name: str = "Series",
        reading_present: bool | None = None,
        editing_present: bool | None = None,
        batch_name: str = "batch-0001",
    ) -> tuple[Path, dict[str, object]]:
        workspace = self.workspace(manga_name)
        workspace.transactions.mkdir(parents=True, exist_ok=True)
        transaction_id = str(uuid.uuid4())
        transaction = workspace.transactions / (
            TRANSACTION_DIRECTORY_PREFIX + transaction_id
        )
        transaction.mkdir()
        marker = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "manga": manga_name,
            "batch": batch_name,
            "created_at": "2026-08-18T00:00:00+00:00",
            "app_version": __version__,
            "present": {
                "source": True,
                "output": True,
                "reading": (
                    workspace.reading.exists()
                    if reading_present is None
                    else reading_present
                ),
                "editing": (
                    workspace.editing.exists()
                    if editing_present is None
                    else editing_present
                ),
            },
            "snapshot_token": "0" * 64,
        }
        atomic_write_json(transaction / TRANSACTION_MARKER_FILENAME, marker)
        return transaction, marker

    def stage_transaction(
        self, transaction: Path, *, install_batch: bool = False
    ) -> None:
        workspace = manga_workspace_paths(self.root, transaction.parent.parent.name)
        for original, staged in (
            (workspace.output, transaction / "output"),
            (workspace.reading, transaction / "reading.json"),
            (workspace.editing, transaction / "editing.json"),
            (self.root / workspace.workspace.name, transaction / "source"),
        ):
            if os.path.lexists(original):
                os.rename(original, staged)
        if install_batch:
            marker = json.loads(
                (transaction / TRANSACTION_MARKER_FILENAME).read_text(encoding="utf-8")
            )
            workspace.completed.mkdir()
            os.rename(transaction / "output", workspace.completed / marker["batch"])

    def mark_committed(self, transaction: Path, marker: dict[str, object]) -> None:
        atomic_write_json(
            transaction / COMMITTED_MARKER_FILENAME,
            {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "transaction_id": marker["transaction_id"],
            },
        )


class CompletionPreviewTests(CompletionFixture):
    def test_preview_uses_live_folders_without_requiring_output_coverage(self) -> None:
        self.add_source_image("Chapter Eleven", "scan-a.jpg")
        self.add_source_image("Part III - Ch 21", "14.png")
        self.export({"Chapter Eleven": ("scan-a.jpg",)})

        preview = analyze_completion(self.root, self.manga())

        self.assertEqual(preview.source_folder_count, 2)
        self.assertEqual(preview.exported_folder_count, 1)
        self.assertEqual(preview.total_image_count, 1)
        self.assertEqual(preview.output_folders[0].name, "Chapter Eleven")
        self.assertEqual(preview.output_folders[0].image_files, ("scan-a.jpg",))
        self.assertEqual(preview.destination_batch.name, "batch-0001")
        self.assertEqual(preview.existing_batches, ())

    def test_refuses_missing_empty_or_unmanaged_output(self) -> None:
        self.add_source_image()
        manga = self.manga()

        with self.assertRaisesRegex(CompletionError, "no valid app-managed"):
            analyze_completion(self.root, manga)

        untracked = self.workspace().output / "Part 1 - Dawn" / "untracked.jpg"
        untracked.parent.mkdir(parents=True)
        untracked.write_bytes(b"not in the editing manifest")
        with self.assertRaisesRegex(CompletionError, "no valid app-managed"):
            analyze_completion(self.root, manga)

    def test_refuses_malformed_editing_metadata(self) -> None:
        self.add_source_image()
        workspace = self.workspace()
        workspace.workspace.mkdir(parents=True)
        workspace.editing.write_bytes(b"not json")

        with self.assertRaisesRegex(CompletionError, "could not be read safely"):
            analyze_completion(self.root, self.manga())

    def test_refuses_modified_manifest_backed_output(self) -> None:
        self.add_source_image()
        manga = self.export()
        managed = next(path for path in self.workspace().output.rglob("*") if path.is_file())
        managed.write_bytes(b"changed after export")

        with self.assertRaisesRegex(CompletionError, "changed after its last export"):
            analyze_completion(self.root, manga)

    def test_malformed_reading_metadata_does_not_hide_valid_editing_output(self) -> None:
        self.add_source_image()
        manga = self.export()
        self.workspace().reading.write_bytes(b"malformed reading state")

        preview = analyze_completion(self.root, manga)
        result = complete_manga(self.root, manga, preview)

        self.assertTrue(result.batch_directory.exists())
        self.assertFalse(self.workspace().reading.exists())

    def test_allocates_after_highest_existing_batch_and_preserves_unknown_items(self) -> None:
        self.add_source_image()
        self.export()
        completed = self.workspace().completed
        (completed / "batch-0002").mkdir(parents=True)
        (completed / "batch-0010").mkdir()
        (completed / "notes.txt").write_text("preserve", encoding="utf-8")

        preview = analyze_completion(self.root, self.manga())

        self.assertEqual(
            [batch.name for batch in preview.existing_batches],
            ["batch-0002", "batch-0010"],
        )
        self.assertEqual(preview.destination_batch.name, "batch-0011")
        self.assertEqual((completed / "notes.txt").read_text(), "preserve")

    def test_refuses_symlink_inside_tree_that_will_be_deleted_or_moved(self) -> None:
        source = self.add_source_image()
        self.export()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        link = source.parent / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")

        with self.assertRaisesRegex(CompletionError, "symbolic link"):
            analyze_completion(self.root, self.manga())
        self.assertEqual(outside.read_bytes(), b"outside")


class CompleteMangaTests(CompletionFixture):
    def test_completion_moves_full_output_and_removes_only_active_state(self) -> None:
        self.add_source_image("Chapter Eleven", "scan-a.jpg")
        self.add_source_image("Unselected", "keep-in-source.png")
        manga = self.export({"Chapter Eleven": ("scan-a.jpg",)})
        ReadingStore(self.root).set_position(
            manga, "Chapter Eleven", "scan-a.jpg"
        )
        workspace = self.workspace()
        untracked = workspace.output / "notes.txt"
        untracked.write_text("whole output tree", encoding="utf-8")
        preview = analyze_completion(self.root, manga)

        result = complete_manga(self.root, manga, preview)

        self.assertEqual(result.batch_name, "batch-0001")
        self.assertEqual(result.batch_directory, workspace.completed / "batch-0001")
        self.assertEqual(result.total_image_count, 1)
        self.assertTrue((result.batch_directory / "notes.txt").is_file())
        self.assertFalse((self.root / "Series").exists())
        self.assertFalse(workspace.output.exists())
        self.assertFalse(workspace.reading.exists())
        self.assertFalse(workspace.editing.exists())
        self.assertTrue(workspace.workspace.is_dir())
        self.assertTrue(workspace.completed.is_dir())
        self.assertFalse((self.root / ".pocket-manga-editor" / "completed").exists())
        self.assertFalse(any(self.root.rglob("completion-log.json")))
        self.assertFalse(any(workspace.transactions.glob("completion-*")))

    def test_completion_deletes_a_read_only_source_tree_without_recovery_residue(
        self,
    ) -> None:
        source = self.add_source_image(contents=b"read only")
        manga = self.export()
        workspace = self.workspace()
        source.chmod(stat.S_IRUSR)
        source.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)

        try:
            result = complete_manga(
                self.root, manga, analyze_completion(self.root, manga)
            )

            self.assertEqual(result.cleanup_warnings, ())
            self.assertFalse((self.root / "Series").exists())
            self.assertEqual(list(workspace.transactions.iterdir()), [])
        finally:
            for candidate in (
                self.root / "Series",
                *(workspace.transactions.glob("completion-*/source")),
            ):
                if candidate.exists():
                    candidate.chmod(
                        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                    )
                    for child in candidate.rglob("*"):
                        if child.is_dir():
                            child.chmod(
                                stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                            )
                        else:
                            child.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_repeated_completion_creates_next_batch_without_touching_prior(self) -> None:
        self.add_source_image("Old material", "one.jpg", contents=b"old")
        old_manga = self.export({"Old material": ("one.jpg",)})
        first = complete_manga(
            self.root, old_manga, analyze_completion(self.root, old_manga)
        )
        first_file = next(path for path in first.batch_directory.rglob("*") if path.is_file())
        first_digest = hashlib.sha256(first_file.read_bytes()).hexdigest()

        self.add_source_image("New release", "two.png", contents=b"new")
        new_manga = self.export({"New release": ("two.png",)})
        preview = analyze_completion(self.root, new_manga)
        self.assertEqual(preview.destination_batch.name, "batch-0002")
        self.assertEqual([item.name for item in preview.existing_batches], ["batch-0001"])

        second = complete_manga(self.root, new_manga, preview)

        self.assertEqual(second.batch_name, "batch-0002")
        self.assertEqual(hashlib.sha256(first_file.read_bytes()).hexdigest(), first_digest)
        self.assertTrue(next(path for path in second.batch_directory.rglob("*") if path.is_file()))

    def test_archiving_all_prior_batches_reuses_first_available_name(self) -> None:
        self.add_source_image()
        manga = self.export()
        first = complete_manga(self.root, manga, analyze_completion(self.root, manga))
        archive = self.root / "archive"
        os.rename(first.batch_directory, archive)

        self.add_source_image("Later", "new.jpg")
        manga = self.export({"Later": ("new.jpg",)})
        preview = analyze_completion(self.root, manga)

        self.assertEqual(preview.destination_batch.name, "batch-0001")
        self.assertTrue(next(path for path in archive.rglob("*") if path.is_file()))

    def test_stale_preview_is_rejected_without_mutation(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        source.write_bytes(b"changed after confirmation")

        with self.assertRaises(CompletionChangedError):
            complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertTrue(self.workspace().output.exists())
        self.assertFalse(self.workspace().completed.exists())

    def test_source_change_during_staging_is_detected_and_rolled_back(self) -> None:
        source = self.add_source_image(contents=b"old-page")
        manga = self.export()
        ReadingStore(self.root).set_position(
            manga, manga.folders[0].name, source.name
        )
        preview = analyze_completion(self.root, manga)
        workspace = self.workspace()
        editing_before = workspace.editing.read_bytes()
        reading_before = workspace.reading.read_bytes()
        output_before = {
            path.relative_to(workspace.output): path.read_bytes()
            for path in workspace.output.rglob("*")
            if path.is_file()
        }
        source_information = source.stat(follow_symlinks=False)
        from pocket_manga_editor import completion

        real_rename = completion._rename_managed
        changed = False

        def change_source_then_stage(original: Path, destination: Path) -> None:
            nonlocal changed
            if not changed and Path(original) == preview.source_directory:
                source.write_bytes(b"new-page")
                os.utime(
                    source,
                    ns=(
                        source_information.st_atime_ns,
                        source_information.st_mtime_ns,
                    ),
                )
                changed = True
            real_rename(original, destination)

        with patch(
            "pocket_manga_editor.completion._rename_managed",
            side_effect=change_source_then_stage,
        ):
            with self.assertRaisesRegex(
                CompletionChangedError, "revalidate the staged manga data"
            ):
                complete_manga(self.root, manga, preview)

        self.assertTrue(changed)
        self.assertEqual(source.read_bytes(), b"new-page")
        self.assertEqual(workspace.editing.read_bytes(), editing_before)
        self.assertEqual(workspace.reading.read_bytes(), reading_before)
        self.assertEqual(
            {
                path.relative_to(workspace.output): path.read_bytes()
                for path in workspace.output.rglob("*")
                if path.is_file()
            },
            output_before,
        )
        self.assertFalse(
            workspace.completed.exists() and any(workspace.completed.iterdir())
        )
        self.assertFalse(any(workspace.transactions.glob("completion-*")))

    def test_new_batch_after_confirmation_cannot_be_overwritten(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        claimed = self.workspace().completed / "batch-0001"
        claimed.mkdir(parents=True)
        sentinel = claimed / "sentinel.txt"
        sentinel.write_bytes(b"existing history")

        with self.assertRaises(CompletionChangedError):
            complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertEqual(sentinel.read_bytes(), b"existing history")

    def test_export_recovery_failure_blocks_completion_without_mutation(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)

        with patch(
            "pocket_manga_editor.completion.recover_interrupted_exports_locked",
            side_effect=ExportRecoveryError("ambiguous export journal"),
        ):
            with self.assertRaisesRegex(CompletionRecoveryError, "export recovery"):
                complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertTrue(self.workspace().output.exists())

    def test_precommit_commit_marker_failure_rolls_everything_back(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        ReadingStore(self.root).set_position(manga, manga.folders[0].name, source.name)
        preview = analyze_completion(self.root, manga)
        real_atomic_write = atomic_write_json

        def fail_commit(path: Path, payload: dict) -> None:
            if path.name == COMMITTED_MARKER_FILENAME:
                raise OSError("simulated commit failure")
            real_atomic_write(path, payload)

        with patch(
            "pocket_manga_editor.completion.atomic_write_json",
            side_effect=fail_commit,
        ):
            with self.assertRaisesRegex(CompletionError, "commit the completion"):
                complete_manga(self.root, manga, preview)

        workspace = self.workspace()
        self.assertTrue(source.exists())
        self.assertTrue(workspace.output.exists())
        self.assertTrue(workspace.reading.exists())
        self.assertTrue(workspace.editing.exists())
        self.assertFalse(workspace.completed.exists() and any(workspace.completed.iterdir()))
        self.assertFalse(any(workspace.transactions.glob("completion-*")))

    def test_exception_after_durable_marker_is_reported_as_committed(self) -> None:
        self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        real_atomic_write = atomic_write_json

        def write_then_fail(path: Path, payload: dict) -> None:
            real_atomic_write(path, payload)
            if path.name == COMMITTED_MARKER_FILENAME:
                raise OSError("raised after atomic replace")

        with patch(
            "pocket_manga_editor.completion.atomic_write_json",
            side_effect=write_then_fail,
        ):
            result = complete_manga(self.root, manga, preview)

        self.assertTrue(result.batch_directory.exists())
        self.assertFalse((self.root / "Series").exists())
        self.assertTrue(result.cleanup_warnings)

    def test_commit_directory_sync_failure_defers_destructive_cleanup(self) -> None:
        self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        from pocket_manga_editor import completion

        real_sync = completion._fsync_directory

        def fail_committed_transaction_sync(path: Path) -> None:
            if (
                path.name.startswith(TRANSACTION_DIRECTORY_PREFIX)
                and (path / COMMITTED_MARKER_FILENAME).exists()
            ):
                raise OSError("simulated directory sync failure")
            real_sync(path)

        with patch(
            "pocket_manga_editor.completion._fsync_directory",
            side_effect=fail_committed_transaction_sync,
        ):
            result = complete_manga(self.root, manga, preview)

        transactions = tuple(
            self.workspace().transactions.glob(f"{TRANSACTION_DIRECTORY_PREFIX}*")
        )
        self.assertTrue(result.batch_directory.exists())
        self.assertTrue(result.cleanup_warnings)
        self.assertFalse((self.root / "Series").exists())
        self.assertEqual(len(transactions), 1)
        self.assertTrue((transactions[0] / "source").exists())

        recovery = recover_interrupted_completions(self.root)
        self.assertEqual(recovery.cleaned_count, 1)
        self.assertFalse(transactions[0].exists())

    def test_interruption_between_rename_and_memory_tracking_rolls_back_from_disk(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        from pocket_manga_editor import completion

        real_rename = completion._rename_managed
        raised = False

        def rename_then_fail(original: Path, destination: Path) -> None:
            nonlocal raised
            real_rename(original, destination)
            if not raised and destination.name == "output":
                raised = True
                raise OSError("interrupted immediately after rename")

        with patch(
            "pocket_manga_editor.completion._rename_managed",
            side_effect=rename_then_fail,
        ):
            with self.assertRaisesRegex(CompletionError, "stage the active"):
                complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertTrue(self.workspace().output.exists())

    def test_committed_cleanup_failure_returns_success_and_is_recoverable(self) -> None:
        self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        from pocket_manga_editor import completion

        real_remove = completion._remove_managed_path

        def fail_source(path: Path, label: str) -> None:
            if label == "staged source manga":
                raise OSError("simulated cleanup failure")
            real_remove(path, label)

        with patch(
            "pocket_manga_editor.completion._remove_managed_path",
            side_effect=fail_source,
        ):
            result = complete_manga(self.root, manga, preview)

        self.assertTrue(result.batch_directory.exists())
        self.assertTrue(result.cleanup_warnings)
        self.assertFalse((self.root / "Series").exists())
        recovery = recover_interrupted_completions(self.root)
        self.assertEqual(recovery.cleaned_count, 1)
        self.assertFalse(any(self.workspace().transactions.glob("completion-*")))

    def test_other_manga_workspace_is_untouched(self) -> None:
        self.add_source_image()
        self.add_source_image("Other folder", "other.jpg", manga="Other")
        series = self.export()
        other = self.export({"Other folder": ("other.jpg",)}, manga_name="Other")
        other_workspace = self.workspace("Other")
        before = other_workspace.editing.read_bytes()

        complete_manga(self.root, series, analyze_completion(self.root, series))

        self.assertTrue(other.path.exists())
        self.assertTrue(other_workspace.output.exists())
        self.assertEqual(other_workspace.editing.read_bytes(), before)

    def test_stale_save_and_export_cannot_recreate_active_state(self) -> None:
        self.add_source_image()
        manga = self.export()
        folder = manga.folders[0]
        complete_manga(self.root, manga, analyze_completion(self.root, manga))

        with self.assertRaises(OSError):
            EditingStore(self.root).save_folder(
                manga,
                folder.name,
                folder.images[0].name,
                (folder.images[0].name,),
            )
        with self.assertRaises(ExportError):
            export_manga(self.root, manga)
        self.assertFalse(self.workspace().editing.exists())
        self.assertFalse(self.workspace().output.exists())


class CompletionCrashRecoveryTests(CompletionFixture):
    def test_precommit_recovery_restores_staged_active_data(self) -> None:
        source = self.add_source_image()
        manga = self.export()
        ReadingStore(self.root).set_position(manga, manga.folders[0].name, source.name)
        transaction, _marker = self.create_transaction()
        self.stage_transaction(transaction)

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.rolled_back_count, 1)
        self.assertEqual(result.cleaned_count, 0)
        self.assertTrue(source.exists())
        self.assertTrue(self.workspace().output.exists())
        self.assertTrue(self.workspace().reading.exists())
        self.assertTrue(self.workspace().editing.exists())
        self.assertFalse(transaction.exists())

    def test_precommit_recovery_moves_installed_batch_back_to_active_output(self) -> None:
        self.add_source_image()
        self.export()
        transaction, _marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.rolled_back_count, 1)
        self.assertTrue(self.workspace().output.exists())
        self.assertFalse((self.workspace().completed / "batch-0001").exists())
        self.assertTrue((self.root / "Series").exists())

    def test_postcommit_recovery_finishes_cleanup(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        self.mark_committed(transaction, marker)

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.rolled_back_count, 0)
        self.assertEqual(result.cleaned_count, 1)
        self.assertFalse((self.root / "Series").exists())
        self.assertFalse(self.workspace().editing.exists())
        self.assertTrue((self.workspace().completed / "batch-0001").exists())
        self.assertFalse(transaction.exists())

    def test_postcommit_cleanup_ignores_an_unsafe_later_active_metadata_leaf(
        self,
    ) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        self.mark_committed(transaction, marker)
        outside = self.root / "outside-reading.json"
        outside.write_text("preserve", encoding="utf-8")
        try:
            self.workspace().reading.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.cleaned_count, 1)
        self.assertTrue(self.workspace().reading.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(transaction.exists())

    def test_retired_cleanup_ignores_unrelated_unsafe_active_metadata(self) -> None:
        workspace = self.workspace()
        workspace.transactions.mkdir(parents=True)
        retired = workspace.transactions / f".retired-completion-{uuid.uuid4()}"
        retired.mkdir()
        (retired / COMMITTED_MARKER_FILENAME).write_text("{}", encoding="utf-8")
        outside = self.root / "outside-editing.json"
        outside.write_text("preserve", encoding="utf-8")
        try:
            workspace.editing.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")

        recover_interrupted_completions(self.root)

        self.assertFalse(retired.exists())
        self.assertTrue(workspace.editing.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_postcommit_recovery_does_not_touch_new_active_same_name_data(self) -> None:
        self.add_source_image("Old", "old.jpg")
        self.export({"Old": ("old.jpg",)})
        transaction, marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        self.mark_committed(transaction, marker)
        archived = self.root / "archived-batch"
        os.rename(self.workspace().completed / "batch-0001", archived)

        new_source = self.add_source_image("New", "new.png")
        new_manga = self.export({"New": ("new.png",)})
        active_editing = self.workspace().editing.read_bytes()
        active_output = next(
            path for path in self.workspace().output.rglob("*") if path.is_file()
        ).read_bytes()

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.cleaned_count, 1)
        self.assertTrue(result.warnings)
        self.assertTrue(new_source.exists())
        self.assertEqual(self.workspace().editing.read_bytes(), active_editing)
        self.assertEqual(
            next(path for path in self.workspace().output.rglob("*") if path.is_file()).read_bytes(),
            active_output,
        )
        self.assertTrue(new_manga.path.exists())
        self.assertTrue(archived.exists())

    def test_keyboard_interrupt_after_commit_is_recovered_as_committed(self) -> None:
        self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        real_atomic_write = atomic_write_json

        def write_then_interrupt(path: Path, payload: dict) -> None:
            real_atomic_write(path, payload)
            if path.name == COMMITTED_MARKER_FILENAME:
                raise KeyboardInterrupt

        with patch(
            "pocket_manga_editor.completion.atomic_write_json",
            side_effect=write_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                complete_manga(self.root, manga, preview)

        self.assertTrue((self.workspace().completed / "batch-0001").exists())
        self.assertFalse((self.root / "Series").exists())
        result = recover_interrupted_completions(self.root)
        self.assertEqual(result.cleaned_count, 1)
        self.assertFalse(any(self.workspace().transactions.glob("completion-*")))

    def test_discards_only_safe_markerless_transaction(self) -> None:
        workspace = self.workspace()
        workspace.transactions.mkdir(parents=True)
        transaction = workspace.transactions / (
            TRANSACTION_DIRECTORY_PREFIX + str(uuid.uuid4())
        )
        transaction.mkdir()
        (transaction / ".transaction.json.interrupted.tmp").write_bytes(b"partial")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.recovered_count, 0)
        self.assertFalse(transaction.exists())

    def test_markerless_transaction_with_payload_fails_closed(self) -> None:
        workspace = self.workspace()
        workspace.transactions.mkdir(parents=True)
        transaction = workspace.transactions / (
            TRANSACTION_DIRECTORY_PREFIX + str(uuid.uuid4())
        )
        transaction.mkdir()
        (transaction / "source").mkdir()

        with self.assertRaisesRegex(CompletionRecoveryError, "marker is missing"):
            recover_interrupted_completions(self.root)

        self.assertTrue((transaction / "source").exists())

    def test_intent_marker_rejects_boolean_schema_version(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        marker["schema_version"] = True
        atomic_write_json(transaction / TRANSACTION_MARKER_FILENAME, marker)

        with self.assertRaisesRegex(CompletionRecoveryError, "invalid format"):
            recover_interrupted_completions(self.root)

        self.assertTrue((self.root / "Series").exists())
        self.assertTrue(self.workspace().output.exists())
        self.assertTrue(transaction.exists())

    def test_intent_marker_rejects_extra_keys(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        marker["unexpected"] = "must not be ignored"
        atomic_write_json(transaction / TRANSACTION_MARKER_FILENAME, marker)

        with self.assertRaisesRegex(CompletionRecoveryError, "invalid format"):
            recover_interrupted_completions(self.root)

        self.assertTrue((self.root / "Series").exists())
        self.assertTrue(self.workspace().output.exists())
        self.assertTrue(transaction.exists())

    def test_intent_marker_strictly_validates_diagnostic_fields(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        marker_path = transaction / TRANSACTION_MARKER_FILENAME
        invalid_values = (
            ("created_at", "2026-08-18T00:00:00"),
            ("app_version", ""),
            ("snapshot_token", "g" * 64),
            ("transaction_id", "{" + str(marker["transaction_id"]) + "}"),
        )

        for field, value in invalid_values:
            with self.subTest(field=field):
                invalid_marker = dict(marker)
                invalid_marker[field] = value
                atomic_write_json(marker_path, invalid_marker)
                with self.assertRaises(CompletionRecoveryError):
                    recover_interrupted_completions(self.root)

        self.assertTrue((self.root / "Series").exists())
        self.assertTrue(self.workspace().output.exists())
        self.assertTrue(transaction.exists())

    def test_recovery_rejects_symlinked_workspace(self) -> None:
        metadata = self.root / ".pocket-manga-editor"
        metadata.mkdir()
        outside = self.root / "outside-workspace"
        outside.mkdir()
        link = metadata / "Series"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")

        with self.assertRaisesRegex(CompletionRecoveryError, "symbolic link"):
            recover_interrupted_completions(self.root)

        self.assertTrue(outside.exists())

    def test_precommit_conflict_fails_closed_without_deleting_either_copy(self) -> None:
        self.add_source_image()
        self.export()
        transaction, _marker = self.create_transaction()
        (transaction / "output").mkdir()

        with self.assertRaisesRegex(CompletionRecoveryError, "unambiguously"):
            recover_interrupted_completions(self.root)

        self.assertTrue(self.workspace().output.exists())
        self.assertTrue((transaction / "output").exists())

    def test_precommit_recovery_rejects_unrecorded_active_metadata(self) -> None:
        self.add_source_image()
        self.export()
        workspace = self.workspace()
        transaction, _marker = self.create_transaction(reading_present=False)
        self.stage_transaction(transaction)
        workspace.reading.write_text("unexpected active state", encoding="utf-8")

        with self.assertRaisesRegex(CompletionRecoveryError, "unrecorded reading"):
            recover_interrupted_completions(self.root)

        self.assertTrue(workspace.reading.exists())
        self.assertTrue((self.root / "Series").exists())
        self.assertTrue(transaction.exists())

    def test_invalid_commit_marker_never_triggers_rollback(self) -> None:
        self.add_source_image()
        self.export()
        transaction, _marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        (transaction / COMMITTED_MARKER_FILENAME).write_bytes(b"invalid")

        with self.assertRaisesRegex(CompletionRecoveryError, "Commit marker"):
            recover_interrupted_completions(self.root)

        self.assertFalse((self.root / "Series").exists())
        self.assertTrue((self.workspace().completed / "batch-0001").exists())
        self.assertTrue((transaction / "source").exists())

    def test_commit_marker_rejects_boolean_schema_without_cleanup(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        atomic_write_json(
            transaction / COMMITTED_MARKER_FILENAME,
            {
                "schema_version": True,
                "transaction_id": marker["transaction_id"],
            },
        )

        with self.assertRaisesRegex(CompletionRecoveryError, "Commit marker is invalid"):
            recover_interrupted_completions(self.root)

        self.assertFalse((self.root / "Series").exists())
        self.assertTrue((self.workspace().completed / "batch-0001").exists())
        self.assertTrue((transaction / "source").exists())

    def test_commit_marker_rejects_extra_keys_without_cleanup(self) -> None:
        self.add_source_image()
        self.export()
        transaction, marker = self.create_transaction()
        self.stage_transaction(transaction, install_batch=True)
        atomic_write_json(
            transaction / COMMITTED_MARKER_FILENAME,
            {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "transaction_id": marker["transaction_id"],
                "unexpected": "must not be ignored",
            },
        )

        with self.assertRaisesRegex(CompletionRecoveryError, "Commit marker is invalid"):
            recover_interrupted_completions(self.root)

        self.assertFalse((self.root / "Series").exists())
        self.assertTrue((self.workspace().completed / "batch-0001").exists())
        self.assertTrue((transaction / "source").exists())


class CompletionLockTests(CompletionFixture):
    def test_recovery_refuses_immediately_when_lock_is_busy(self) -> None:
        with library_mutation_lock(self.root):
            with self.assertRaisesRegex(CompletionBusyError, "already in progress"):
                recover_interrupted_completions(self.root)

    def test_real_second_process_excludes_completion(self) -> None:
        self.add_source_image()
        manga = self.export()
        preview = analyze_completion(self.root, manga)
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_library_lock_in_child,
            args=(str(self.root), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "child did not acquire the mutation lock")
            with self.assertRaisesRegex(CompletionBusyError, "already in progress"):
                complete_manga(self.root, manga, preview)
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue((self.root / "Series").exists())


if __name__ == "__main__":
    unittest.main()
