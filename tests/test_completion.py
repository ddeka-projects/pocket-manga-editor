from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from pocket_manga_editor import __version__
from pocket_manga_editor.completion import (
    COMPLETION_LOG_SCHEMA_VERSION,
    CompletionBusyError,
    CompletionChangedError,
    CompletionError,
    CompletionRecoveryError,
    analyze_completion,
    complete_manga,
    recover_interrupted_completions,
)
from pocket_manga_editor.exporter import ExportError, export_selected_pages
from pocket_manga_editor.scanner import scan_working_directory
from pocket_manga_editor.library_lock import library_mutation_lock
from pocket_manga_editor.storage import (
    SessionStore,
    atomic_write_json as real_atomic_write_json,
)


def _hold_library_lock_in_child(
    root: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
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

    @property
    def metadata(self) -> Path:
        return self.root / ".pocket-manga-editor"

    def add_source_page(
        self,
        volume_folder: str = "Vol. 01",
        page_name: str = "001.jpg",
        *,
        manga: str = "Series",
    ) -> Path:
        page = self.root / manga / volume_folder / page_name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(f"source:{volume_folder}:{page_name}".encode())
        return page

    def add_output_page(
        self,
        volume_folder: str = "Vol.01",
        page_name: str = "P001.jpg",
        *,
        manga: str = "Series",
    ) -> Path:
        page = self.metadata / "output" / manga / volume_folder / page_name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(f"output:{volume_folder}:{page_name}".encode())
        return page

    def manga(self, name: str = "Series"):
        return next(
            manga
            for manga in scan_working_directory(self.root).mangas
            if manga.name == name
        )


class CompletionPreviewTests(CompletionFixture):
    def test_inventories_scanner_omissions_and_uses_canonical_volume_identity(self) -> None:
        self.add_source_page("Vol. 01")
        (self.root / "Series" / "Vol. 02").mkdir()
        (self.root / "Series" / "Vol.badly named").mkdir()
        self.add_output_page("Vol.1")

        preview = analyze_completion(self.root, self.manga())

        self.assertEqual(preview.source_volumes, ("Vol.01", "Vol.02", "Vol.badly named"))
        self.assertEqual(
            preview.source_folders,
            ("Vol. 01", "Vol. 02", "Vol.badly named"),
        )
        self.assertEqual(preview.missing_volumes, ("Vol.02", "Vol.badly named"))
        self.assertEqual(preview.unexpected_volumes, ())
        self.assertTrue(preview.has_volume_mismatch)

    def test_flags_empty_and_invalid_output_volume_folders_as_unexpected(self) -> None:
        self.add_source_page()
        self.add_output_page()
        (self.metadata / "output" / "Series" / "Vol.9").mkdir()
        self.add_output_page("not-a-volume", "extra.png")

        preview = analyze_completion(self.root, self.manga())

        self.assertEqual(preview.missing_volumes, ())
        self.assertEqual(preview.unexpected_volumes, ("not-a-volume", "Vol.9"))
        self.assertEqual(preview.total_image_count, 2)

    def test_same_volume_count_with_the_wrong_identity_is_a_mismatch(self) -> None:
        self.add_source_page("Vol. 01")
        self.add_output_page("Vol.02")

        preview = analyze_completion(self.root, self.manga())

        self.assertEqual(preview.missing_volumes, ("Vol.01",))
        self.assertEqual(preview.unexpected_volumes, ("Vol.02",))
        self.assertTrue(preview.has_volume_mismatch)

    def test_live_source_inventory_does_not_reuse_stale_manga_volumes(self) -> None:
        source_page = self.add_source_page("Vol. 01")
        self.add_output_page("Vol.01")
        stale_manga = self.manga()
        source_page.unlink()
        source_page.parent.rmdir()

        preview = analyze_completion(self.root, stale_manga)

        self.assertEqual(preview.source_volumes, ())
        self.assertEqual(preview.source_folders, ())
        self.assertEqual(preview.missing_volumes, ())
        self.assertEqual(preview.unexpected_volumes, ("Vol.01",))
        self.assertTrue(preview.has_volume_mismatch)

        result = complete_manga(
            self.root, stale_manga, preview, allow_volume_mismatch=True
        )
        entry = json.loads(result.log_path.read_text(encoding="utf-8"))["completions"][0]
        self.assertEqual(entry["source"]["volumes"], [])
        self.assertEqual(entry["volume_check"]["unexpected_in_output"], ["Vol.01"])

    def test_refuses_missing_or_empty_output(self) -> None:
        self.add_source_page()
        manga = self.manga()

        with self.assertRaisesRegex(CompletionError, "no exported output"):
            analyze_completion(self.root, manga)

        (self.metadata / "output" / "Series" / "Vol.01").mkdir(parents=True)
        with self.assertRaisesRegex(CompletionError, "no exported JPG or PNG"):
            analyze_completion(self.root, manga)

    def test_refuses_existing_and_broken_symlink_destinations(self) -> None:
        self.add_source_page()
        self.add_output_page()
        completed = self.metadata / "completed" / "Series"
        completed.mkdir(parents=True)

        with self.assertRaisesRegex(CompletionError, "already exists"):
            analyze_completion(self.root, self.manga())

        completed.rmdir()
        completed.symlink_to(self.root / "does-not-exist", target_is_directory=True)
        with self.assertRaisesRegex(CompletionError, "already exists"):
            analyze_completion(self.root, self.manga())

    def test_refuses_invalid_history_without_changing_anything(self) -> None:
        source = self.add_source_page()
        output = self.add_output_page()
        log = self.metadata / "completed" / "completion-log.json"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"not json")

        with self.assertRaisesRegex(CompletionError, "history"):
            analyze_completion(self.root, self.manga())

        self.assertTrue(source.exists())
        self.assertTrue(output.exists())
        self.assertEqual(log.read_bytes(), b"not json")

    def test_refuses_symlinks_anywhere_in_source_or_output_tree(self) -> None:
        self.add_source_page()
        self.add_output_page()
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        link = self.root / "Series" / "Vol. 01" / "linked.jpg"
        link.symlink_to(outside)

        with self.assertRaisesRegex(CompletionError, "symbolic link"):
            analyze_completion(self.root, self.manga())

        link.unlink()
        output_link = self.metadata / "output" / "Series" / "Vol.01" / "linked.jpg"
        output_link.symlink_to(outside)
        with self.assertRaisesRegex(CompletionError, "symbolic link"):
            analyze_completion(self.root, self.manga())

    def test_refuses_reserved_log_filename_as_a_manga_name(self) -> None:
        self.add_source_page(manga="completion-log.json")
        self.add_output_page(manga="completion-log.json")

        with self.assertRaisesRegex(CompletionError, "reserved"):
            analyze_completion(self.root, self.manga("completion-log.json"))


class CompleteMangaTests(CompletionFixture):
    def test_completion_moves_output_deletes_source_and_metadata_and_logs_details(self) -> None:
        source = self.add_source_page()
        first = self.add_output_page(page_name="P001.jpg")
        second = self.add_output_page(page_name="P002.PNG")
        first_bytes = first.read_bytes()
        selection = self.metadata / "selections" / "Series" / "Vol.01.json"
        manifest = self.metadata / "exports" / "Series" / "Vol.01.json"
        selection.parent.mkdir(parents=True)
        manifest.parent.mkdir(parents=True)
        selection.write_text("selection", encoding="utf-8")
        manifest.write_text("manifest", encoding="utf-8")
        preview = analyze_completion(self.root, self.manga())

        result = complete_manga(self.root, self.manga(), preview)

        self.assertFalse(source.parents[1].exists())
        self.assertFalse((self.metadata / "output" / "Series").exists())
        self.assertFalse((self.metadata / "selections" / "Series").exists())
        self.assertFalse((self.metadata / "exports" / "Series").exists())
        self.assertEqual(
            (result.completed_directory / "Vol.01" / first.name).read_bytes(),
            first_bytes,
        )
        self.assertTrue((result.completed_directory / "Vol.01" / second.name).exists())
        self.assertEqual(result.total_image_count, 2)
        self.assertEqual(result.cleanup_warnings, ())
        self.assertFalse(any(self.metadata.glob(".pme-completion-*")))

        payload = json.loads(result.log_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], COMPLETION_LOG_SCHEMA_VERSION)
        entry = payload["completions"][0]
        self.assertEqual(str(uuid.UUID(entry["transaction_id"])), entry["transaction_id"])
        self.assertEqual(entry["manga"], "Series")
        self.assertEqual(entry["app_version"], __version__)
        self.assertEqual(entry["source"]["volumes"], ["Vol.01"])
        self.assertEqual(entry["source"]["folders"], ["Vol. 01"])
        self.assertEqual(
            entry["output"]["volumes"][0]["images"],
            ["P001.jpg", "P002.PNG"],
        )
        self.assertEqual(entry["output"]["total_image_count"], 2)
        self.assertEqual(entry["volume_check"]["missing_from_output"], [])
        self.assertIn("+00:00", entry["completed_at"])
        self.assertNotIn("previous_completions", entry)

    def test_requires_explicit_permission_for_a_volume_mismatch(self) -> None:
        self.add_source_page()
        (self.root / "Series" / "Vol. 02").mkdir()
        self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)

        with self.assertRaisesRegex(CompletionError, "Explicit confirmation"):
            complete_manga(self.root, manga, preview)

        self.assertTrue((self.root / "Series").exists())
        result = complete_manga(
            self.root, manga, preview, allow_volume_mismatch=True
        )
        self.assertTrue(result.completed_directory.exists())

    def test_rejects_a_stale_preview_without_mutation(self) -> None:
        source = self.add_source_page()
        output = self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)
        output.write_bytes(b"changed after dialog")

        with self.assertRaises(CompletionChangedError):
            complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertEqual(output.read_bytes(), b"changed after dialog")

    def test_log_failure_rolls_back_source_output_and_metadata(self) -> None:
        source = self.add_source_page()
        output = self.add_output_page()
        selection = self.metadata / "selections" / "Series" / "state.json"
        manifest = self.metadata / "exports" / "Series" / "manifest.json"
        selection.parent.mkdir(parents=True)
        manifest.parent.mkdir(parents=True)
        selection.write_bytes(b"selection")
        manifest.write_bytes(b"manifest")
        manga = self.manga()
        preview = analyze_completion(self.root, manga)

        def fail_log_only(path: Path, payload: dict) -> None:
            if path.name == "completion-log.json":
                raise OSError("simulated log failure")
            real_atomic_write_json(path, payload)

        with patch(
            "pocket_manga_editor.completion.atomic_write_json",
            side_effect=fail_log_only,
        ):
            with self.assertRaisesRegex(CompletionError, "completion history"):
                complete_manga(self.root, manga, preview)

        self.assertTrue(source.exists())
        self.assertTrue(output.exists())
        self.assertEqual(selection.read_bytes(), b"selection")
        self.assertEqual(manifest.read_bytes(), b"manifest")
        self.assertFalse((self.metadata / "completed" / "Series").exists())
        self.assertFalse((self.metadata / "completed" / "completion-log.json").exists())
        self.assertFalse(any(self.metadata.glob(".pme-completion-*")))

    def test_committed_cleanup_failure_returns_success_with_warning(self) -> None:
        self.add_source_page()
        self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)

        with patch(
            "pocket_manga_editor.completion._remove_managed_path",
            side_effect=OSError("simulated cleanup failure"),
        ):
            result = complete_manga(self.root, manga, preview)

        self.assertTrue(result.completed_directory.exists())
        self.assertTrue(result.log_path.exists())
        self.assertFalse((self.root / "Series").exists())
        self.assertTrue(result.cleanup_warnings)

    def test_incomplete_rollback_raises_recovery_error_for_mandatory_rescan(self) -> None:
        self.add_source_page()
        self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)
        completed = self.metadata / "completed" / "Series"
        output = self.metadata / "output" / "Series"
        real_replace = os.replace

        def fail_log_only(path: Path, payload: dict) -> None:
            if path.name == "completion-log.json":
                raise OSError("simulated log failure")
            real_atomic_write_json(path, payload)

        def fail_output_restore(source: str | Path, destination: str | Path) -> None:
            if (
                Path(source).parent.name == "completed"
                and Path(destination).parent.name == "output"
            ):
                raise OSError("simulated rollback failure")
            real_replace(source, destination)

        with patch(
            "pocket_manga_editor.completion.atomic_write_json",
            side_effect=fail_log_only,
        ), patch("pocket_manga_editor.completion.os.replace", side_effect=fail_output_restore):
            with self.assertRaises(CompletionRecoveryError) as raised:
                complete_manga(self.root, manga, preview)

        self.assertTrue(raised.exception.state_may_have_changed)
        self.assertTrue(completed.exists())
        self.assertFalse(output.exists())
        self.assertTrue(any(self.metadata.glob(".pme-completion-*")))

    def test_completion_preserves_every_other_mangas_data(self) -> None:
        self.add_source_page()
        self.add_output_page()
        other_source = self.add_source_page(manga="Other")
        other_output = self.add_output_page(manga="Other")
        other_selection = self.metadata / "selections" / "Other" / "state.json"
        other_export = self.metadata / "exports" / "Other" / "manifest.json"
        other_selection.parent.mkdir(parents=True)
        other_export.parent.mkdir(parents=True)
        other_selection.write_bytes(b"other selection")
        other_export.write_bytes(b"other export")
        manga = self.manga("Series")

        complete_manga(self.root, manga, analyze_completion(self.root, manga))

        self.assertTrue(other_source.exists())
        self.assertTrue(other_output.exists())
        self.assertEqual(other_selection.read_bytes(), b"other selection")
        self.assertEqual(other_export.read_bytes(), b"other export")

    def test_second_instance_with_stale_shared_log_preview_cannot_lose_an_entry(self) -> None:
        self.add_source_page(manga="First")
        self.add_output_page(manga="First")
        self.add_source_page(manga="Second")
        self.add_output_page(manga="Second")
        first_manga = self.manga("First")
        second_manga = self.manga("Second")
        first_preview = analyze_completion(self.root, first_manga)
        stale_second_preview = analyze_completion(self.root, second_manga)

        first_result = complete_manga(self.root, first_manga, first_preview)
        with self.assertRaises(CompletionChangedError):
            complete_manga(self.root, second_manga, stale_second_preview)

        entries = json.loads(first_result.log_path.read_text(encoding="utf-8"))[
            "completions"
        ]
        self.assertEqual([entry["manga"] for entry in entries], ["First"])
        self.assertTrue((self.root / "Second").exists())
        self.assertTrue((self.metadata / "output" / "Second").exists())


class CompletionCrashRecoveryTests(CompletionFixture):
    def test_discards_only_safe_markerless_pretransaction_staging(self) -> None:
        staging = self.metadata / ".pme-completion-before-marker"
        staging.mkdir(parents=True)
        (staging / ".transaction.json.interrupted.tmp").write_bytes(b"partial")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.recovered_count, 0)
        self.assertFalse(staging.exists())

    def test_noop_recovery_does_not_require_a_valid_completion_log(self) -> None:
        log = self.metadata / "completed" / "completion-log.json"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"corrupt but no recovery transaction exists")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.recovered_count, 0)
        self.assertEqual(log.read_bytes(), b"corrupt but no recovery transaction exists")

    def test_recovers_a_precommit_crash_by_rolling_staged_data_back(self) -> None:
        source_page = self.add_source_page()
        output_page = self.add_output_page()
        staging = self.metadata / ".pme-completion-simulated-precommit"
        staging.mkdir()
        marker = {
            "schema_version": 1,
            "transaction_id": str(uuid.uuid4()),
            "manga": "Series",
            "created_at": "2026-08-15T00:00:00+00:00",
            "app_version": __version__,
            "present": {
                "source": True,
                "output": True,
                "selections": False,
                "exports": False,
            },
        }
        real_atomic_write_json(staging / "transaction.json", marker)
        os.replace(self.metadata / "output" / "Series", staging / "output")
        os.replace(self.root / "Series", staging / "source")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.rolled_back_count, 1)
        self.assertEqual(result.cleaned_count, 0)
        self.assertTrue(source_page.exists())
        self.assertTrue(output_page.exists())
        self.assertFalse(staging.exists())

    def test_recovery_rejects_a_symlinked_managed_root(self) -> None:
        self.add_source_page()
        self.add_output_page()
        staging = self.metadata / ".pme-completion-simulated-unsafe"
        staging.mkdir()
        marker = {
            "schema_version": 1,
            "transaction_id": str(uuid.uuid4()),
            "manga": "Series",
            "created_at": "2026-08-15T00:00:00+00:00",
            "app_version": __version__,
            "present": {
                "source": True,
                "output": True,
                "selections": False,
                "exports": False,
            },
        }
        real_atomic_write_json(staging / "transaction.json", marker)
        os.replace(self.metadata / "output" / "Series", staging / "output")
        output_root = self.metadata / "output"
        output_root.rmdir()
        outside = self.root / "outside-output"
        outside.mkdir()
        output_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(CompletionRecoveryError):
            recover_interrupted_completions(self.root)

        self.assertFalse(any(outside.iterdir()))
        self.assertTrue((staging / "output").exists())

    def test_recovers_a_postcommit_crash_by_finishing_staged_cleanup(self) -> None:
        self.add_source_page()
        self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)

        with patch(
            "pocket_manga_editor.completion.shutil.rmtree",
            side_effect=OSError("simulated process exit before cleanup"),
        ):
            completed = complete_manga(self.root, manga, preview)
        staging = next(self.metadata.glob(".pme-completion-*"))
        self.assertTrue(staging.exists())
        self.assertTrue(completed.completed_directory.exists())

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.rolled_back_count, 0)
        self.assertEqual(result.cleaned_count, 1)
        self.assertFalse(staging.exists())
        self.assertTrue(completed.completed_directory.exists())
        self.assertTrue(completed.log_path.exists())

    def test_committed_recovery_preserves_a_new_batch_after_completed_was_moved(self) -> None:
        self.add_source_page("Vol. 01")
        self.add_output_page("Vol.01")
        manga = self.manga()
        preview = analyze_completion(self.root, manga)
        with patch(
            "pocket_manga_editor.completion.shutil.rmtree",
            side_effect=OSError("simulated process exit before cleanup"),
        ):
            completed = complete_manga(self.root, manga, preview)
        staging = next(self.metadata.glob(".pme-completion-*"))
        archived = self.root / "archived-completed-batch"
        os.replace(completed.completed_directory, archived)
        new_source = self.add_source_page("Vol. 02")
        new_output = self.add_output_page("Vol.02")

        result = recover_interrupted_completions(self.root)

        self.assertEqual(result.cleaned_count, 1)
        self.assertFalse(staging.exists())
        self.assertTrue((archived / "Vol.01" / "P001.jpg").exists())
        self.assertTrue(new_source.exists())
        self.assertTrue(new_output.exists())

    def test_recovery_refuses_immediately_when_the_global_lock_is_busy(self) -> None:
        self.metadata.mkdir()

        with library_mutation_lock(self.root):
            with self.assertRaisesRegex(CompletionBusyError, "already in progress"):
                recover_interrupted_completions(self.root)

    def test_same_manga_can_be_completed_again_after_completed_folder_is_moved(self) -> None:
        self.add_source_page("Vol. 01")
        self.add_output_page("Vol.01")
        first_manga = self.manga()
        first = complete_manga(
            self.root, first_manga, analyze_completion(self.root, first_manga)
        )
        original_entry = json.loads(first.log_path.read_text(encoding="utf-8"))[
            "completions"
        ][0]
        archive = self.root / "archived-first-batch"
        os.replace(first.completed_directory, archive)

        self.add_source_page("Vol. 02")
        self.add_output_page("Vol.02")
        second_manga = self.manga()
        second_preview = analyze_completion(self.root, second_manga)
        self.assertEqual(len(second_preview.previous_completions), 1)
        self.assertEqual(second_preview.previous_completions[0].source_volumes, ("Vol.01",))

        second = complete_manga(self.root, second_manga, second_preview)
        entries = json.loads(second.log_path.read_text(encoding="utf-8"))["completions"]

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], original_entry)
        self.assertEqual(entries[1]["source"]["volumes"], ["Vol.02"])
        self.assertTrue((archive / "Vol.01" / "P001.jpg").exists())
        self.assertTrue((second.completed_directory / "Vol.02" / "P001.jpg").exists())


class LibraryMutationLockTests(CompletionFixture):
    @unittest.skipIf(os.name == "nt", "POSIX flock-specific fault injection")
    def test_unlock_failure_cannot_turn_a_committed_completion_into_failure(self) -> None:
        self.add_source_page()
        self.add_output_page()
        manga = self.manga()
        preview = analyze_completion(self.root, manga)

        with patch(
            "pocket_manga_editor.library_lock.fcntl.flock",
            side_effect=[None, OSError("simulated unlock failure")],
        ):
            result = complete_manga(self.root, manga, preview)

        self.assertTrue(result.completed_directory.exists())
        self.assertTrue(result.log_path.exists())

    def test_stale_session_save_cannot_recreate_state_after_completion(self) -> None:
        self.add_source_page()
        self.add_output_page()
        manga = self.manga()
        volume = manga.volumes[0]
        store = SessionStore(self.root)
        complete_manga(self.root, manga, analyze_completion(self.root, manga))

        with self.assertRaisesRegex(OSError, "source manga no longer exists"):
            store.save(volume, 0, {volume.pages[0].relative_path})

        self.assertFalse((self.metadata / "selections" / "Series").exists())

        with self.assertRaisesRegex(ExportError, "source manga no longer exists"):
            export_selected_pages(
                self.root, volume, {volume.pages[0].relative_path}
            )
        self.assertFalse((self.metadata / "output" / "Series").exists())
        self.assertFalse((self.metadata / "exports" / "Series").exists())

    def test_export_fails_without_mutation_when_another_mutation_holds_the_lock(self) -> None:
        self.add_source_page()
        volume = self.manga().volumes[0]

        with library_mutation_lock(self.root):
            with self.assertRaisesRegex(ExportError, "already in progress"):
                export_selected_pages(
                    self.root, volume, {volume.pages[0].relative_path}
                )

        self.assertFalse((self.metadata / "output" / "Series").exists())

    def test_lock_excludes_a_real_second_process(self) -> None:
        self.add_source_page()
        volume = self.manga().volumes[0]
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_library_lock_in_child,
            args=(str(self.root), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "child process did not acquire the lock")
            with self.assertRaisesRegex(ExportError, "already in progress"):
                export_selected_pages(
                    self.root, volume, {volume.pages[0].relative_path}
                )
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
