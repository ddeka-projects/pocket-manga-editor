from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pocket_manga_editor.exporter as exporter_module
from pocket_manga_editor.completion import recover_interrupted_completions
from pocket_manga_editor.exporter import (
    ExportBusyError,
    ExportConflict,
    ExportError,
    ExportRecoveryError,
    export_manga,
    exported_image_name,
    manga_output_directory,
    recover_interrupted_exports,
    recover_interrupted_exports_locked,
    verify_managed_output,
)
from pocket_manga_editor.filesystem_ops import (
    remove_managed_path,
    rename_no_replace,
)
from pocket_manga_editor.library_lock import (
    LibraryBusyError,
    LibraryLockError,
    library_mutation_lock,
)
from pocket_manga_editor.path_safety import is_link_or_reparse
from pocket_manga_editor.scanner import ScanError, scan_working_directory
from pocket_manga_editor.storage import (
    EditingStateError,
    EditingStore,
    ReadingStore,
)
from pocket_manga_editor.workspace import (
    WorkspaceError,
    manga_workspace_paths,
)


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_image(
        self,
        manga: str,
        folder: str,
        image: str,
        content: bytes | None = None,
    ) -> Path:
        source_folder = self.root / manga / folder
        source_folder.mkdir(parents=True, exist_ok=True)
        source = source_folder / image
        source.write_bytes(content if content is not None else image.encode("utf-8"))
        return source

    def manga(self, name: str = "Kimi wa 08"):
        result = scan_working_directory(self.root)
        return next(manga for manga in result.mangas if manga.name == name)


class ScannerTests(RepositoryFixture):
    def test_scans_arbitrary_exact_names_in_natural_order(self) -> None:
        for folder, images in (
            ("Part 10", ("10.jpg", "2.png", "1.JPG", "cover-final.PNG")),
            ("part 2", ("page 10.png", "page 2.png", "page 1.jpg")),
            ("Chapter Eleven", ("scan-b.jpg", "scan-a.jpg")),
        ):
            for image in images:
                self.add_image("Kimi wa 08", folder, image)

        manga = self.manga()

        self.assertEqual(
            [folder.name for folder in manga.folders],
            ["Chapter Eleven", "part 2", "Part 10"],
        )
        self.assertEqual(
            [image.name for image in manga.folders[1].images],
            ["page 1.jpg", "page 2.png", "page 10.png"],
        )
        self.assertEqual(
            [image.name for image in manga.folders[2].images],
            ["1.JPG", "2.png", "10.jpg", "cover-final.PNG"],
        )
        self.assertEqual(manga.path.name, "Kimi wa 08")
        self.assertEqual(manga.folders[0].path.name, "Chapter Eleven")

    def test_keeps_same_stem_different_extensions_as_distinct_images(self) -> None:
        self.add_image("Series", "Anything", "004.jpg")
        self.add_image("Series", "Anything", "004.PNG")

        images = self.manga("Series").folders[0].images

        self.assertEqual([image.name for image in images], ["004.jpg", "004.PNG"])

    def test_ignores_empty_nested_direct_and_unrelated_content(self) -> None:
        self.add_image("Series", "Valid", "cover.jpg")
        (self.root / "Series" / "Empty").mkdir()
        (self.root / "Series" / "Valid" / "notes.txt").write_text("ignored")
        nested = self.root / "Series" / "Nested" / "Inside"
        nested.mkdir(parents=True)
        (nested / "nested.jpg").write_bytes(b"ignored")
        (self.root / "Series" / "direct.png").write_bytes(b"ignored")
        (self.root / "No Images").mkdir()

        result = scan_working_directory(self.root)

        self.assertEqual([manga.name for manga in result.mangas], ["Series"])
        self.assertEqual([folder.name for folder in result.mangas[0].folders], ["Valid"])
        self.assertEqual(result.issues, ())

    def test_rejects_symlinked_manga_folder_and_image_candidates(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escape.jpg").write_bytes(b"outside")
        self.add_image("Safe", "Folder", "safe.jpg")
        try:
            (self.root / "Linked Manga").symlink_to(
                self.root / "Safe", target_is_directory=True
            )
            (self.root / "Safe" / "Linked Folder").symlink_to(
                outside, target_is_directory=True
            )
            (self.root / "Safe" / "Folder" / "linked.jpg").symlink_to(
                outside / "escape.jpg"
            )
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")

        result = scan_working_directory(self.root)

        self.assertEqual([manga.name for manga in result.mangas], ["Safe"])
        self.assertEqual(
            [image.name for image in result.mangas[0].folders[0].images],
            ["safe.jpg"],
        )
        self.assertEqual(len(result.issues), 3)
        self.assertTrue(all("link" in issue.message.lower() for issue in result.issues))

    def test_scanner_skips_lock_named_manga_with_an_issue(self) -> None:
        self.add_image(".library-mutation.lock", "Folder", "1.jpg")

        result = scan_working_directory(self.root)

        self.assertEqual(result.mangas, ())
        self.assertEqual(len(result.issues), 1)
        self.assertIn("reserved", result.issues[0].message)

    def test_rejects_a_symlink_working_directory(self) -> None:
        alias = self.root.parent / f"{self.root.name}-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")
        try:
            with self.assertRaises(ScanError):
                scan_working_directory(alias)

            with self.assertRaises(LibraryLockError):
                with library_mutation_lock(alias):
                    self.fail("A linked library root must never acquire a lock.")
        finally:
            alias.unlink(missing_ok=True)

    def test_windows_reparse_attribute_is_rejected_without_path_is_junction(self) -> None:
        candidate = self.root / "junction-like"
        information = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ),
        )

        with patch("pocket_manga_editor.path_safety.os.name", "nt"), patch.object(
            Path, "lstat", return_value=information
        ):
            self.assertTrue(is_link_or_reparse(candidate))


class FilesystemOperationTests(RepositoryFixture):
    def test_atomic_no_replace_preserves_an_existing_empty_directory(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()

        with self.assertRaises(OSError):
            rename_no_replace(source, destination)

        self.assertTrue(source.is_dir())
        self.assertTrue(destination.is_dir())

    def test_managed_removal_handles_a_read_only_tree(self) -> None:
        tree = self.root / "tree"
        locked = tree / "locked"
        locked.mkdir(parents=True)
        image = locked / "1.jpg"
        image.write_bytes(b"one")
        image.chmod(0)
        locked.chmod(0)

        remove_managed_path(tree)

        self.assertFalse(tree.exists())

    def test_managed_removal_refuses_a_cross_device_child_before_deletion(self) -> None:
        tree = self.root / "tree"
        mounted = tree / "mounted"
        mounted.mkdir(parents=True)
        sentinel = mounted / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        root_device = tree.stat().st_dev
        real_stat = Path.stat

        def cross_device_stat(candidate: Path, *args, **kwargs):
            information = real_stat(candidate, *args, **kwargs)
            if candidate == mounted:
                return SimpleNamespace(
                    st_mode=information.st_mode,
                    st_dev=root_device + 1,
                )
            return information

        with patch.object(Path, "stat", cross_device_stat):
            with self.assertRaisesRegex(OSError, "mounted filesystem"):
                remove_managed_path(tree)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_windows_file_sync_uses_writable_descriptor_and_restores_mode(self) -> None:
        staged = self.root / "staged.jpg"
        staged.write_bytes(b"staged")
        staged.chmod(stat.S_IRUSR)
        opened_flags: list[int] = []
        real_open = os.open

        def record_open(path, flags, *args, **kwargs):
            opened_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(exporter_module, "_WINDOWS_FILE_SYNC", True), patch.object(
            exporter_module.os, "open", side_effect=record_open
        ), patch.object(exporter_module.os, "fsync") as fsync:
            exporter_module._fsync_file(staged)

        self.assertEqual(len(opened_flags), 1)
        self.assertEqual(opened_flags[0] & os.O_ACCMODE, os.O_RDWR)
        fsync.assert_called_once()
        self.assertFalse(stat.S_IMODE(staged.stat().st_mode) & stat.S_IWRITE)

    def test_managed_removal_refuses_a_mounted_root_before_deletion(self) -> None:
        tree = self.root / "tree"
        tree.mkdir()
        sentinel = tree / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        parent_device = tree.parent.stat().st_dev
        real_stat = Path.stat

        def mounted_root_stat(candidate: Path, *args, **kwargs):
            information = real_stat(candidate, *args, **kwargs)
            if candidate == tree:
                return SimpleNamespace(
                    st_mode=information.st_mode,
                    st_dev=parent_device + 1,
                )
            return information

        with patch.object(Path, "stat", mounted_root_stat):
            with self.assertRaisesRegex(OSError, "mounted filesystem"):
                remove_managed_path(tree)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


class StoreTests(RepositoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.add_image("Series", "Folder 1", "1.jpg")
        self.add_image("Series", "Folder 1", "2.PNG")
        self.add_image("Series", "Folder 2", "10.jpg")
        self.add_image("Series", "Folder 2", "2.jpg")
        self.manga_ref = self.manga("Series")
        self.reading = ReadingStore(self.root)
        self.editing = EditingStore(self.root)

    def test_reading_and_editing_positions_round_trip_independently(self) -> None:
        initial = self.reading.load(self.manga_ref)
        self.assertFalse(self.reading.path_for(self.manga_ref).exists())
        self.assertEqual(initial.last_folder, "Folder 1")
        self.assertEqual(initial.folders["Folder 1"].current_image, "1.jpg")

        read = self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")
        edit = self.editing.save_folder(
            self.manga_ref, "Folder 1", "2.PNG", {"2.PNG"}
        )

        self.assertEqual(read.last_folder, "Folder 2")
        self.assertEqual(read.folders["Folder 2"].current_image, "10.jpg")
        self.assertEqual(edit.last_folder, "Folder 1")
        self.assertEqual(edit.folders["Folder 1"].current_image, "2.PNG")
        self.assertEqual(edit.folders["Folder 1"].selected_images, {"2.PNG"})
        self.assertNotIn("selected_images", self.reading.path_for(self.manga_ref).read_text())

    def test_reading_remembers_each_visited_folder_by_exact_name(self) -> None:
        self.reading.set_position(self.manga_ref, "Folder 1", "2.PNG")
        self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")

        restored = self.reading.load(self.manga_ref)
        payload = json.loads(self.reading.path_for(self.manga_ref).read_text())

        self.assertEqual(restored.last_folder, "Folder 2")
        self.assertEqual(restored.folders["Folder 1"].current_image, "2.PNG")
        self.assertEqual(restored.folders["Folder 2"].current_image, "10.jpg")
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "last_folder": "Folder 2",
                "folders": {
                    "Folder 1": {"current_image": "2.PNG"},
                    "Folder 2": {"current_image": "10.jpg"},
                },
            },
        )

    def test_selection_mutation_does_not_change_editing_position(self) -> None:
        self.editing.set_position(self.manga_ref, "Folder 1", "2.PNG")

        updated = self.editing.set_selection(
            self.manga_ref, "Folder 1", "1.jpg", True
        )

        self.assertEqual(updated.folders["Folder 1"].current_image, "2.PNG")
        self.assertEqual(updated.folders["Folder 1"].selected_images, {"1.jpg"})

    def test_save_folder_merges_latest_other_folder_and_export_state(self) -> None:
        self.editing.save_folder(
            self.manga_ref, "Folder 1", "2.PNG", {"1.jpg", "2.PNG"}
        )
        self.editing.save_folder(
            self.manga_ref, "Folder 2", "10.jpg", {"2.jpg"}
        )

        exported = export_manga(self.root, self.manga_ref)
        saved = self.editing.save_folder(
            self.manga_ref, "Folder 1", "1.jpg", {"2.PNG"}
        )

        self.assertEqual(exported.copied_count, 3)
        self.assertEqual(saved.folders["Folder 2"].selected_images, {"2.jpg"})
        self.assertEqual(set(saved.exports), {"Folder 1", "Folder 2"})

    def test_stale_positions_and_selections_fall_back_without_rename_inference(self) -> None:
        path = self.editing.path_for(self.manga_ref)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "last_folder": "Renamed Folder",
                    "folders": {
                        "Folder 1": {
                            "current_image": "missing.jpg",
                            "selected_images": ["1.jpg", "renamed.png", "gone.png"],
                        },
                        "Old Folder": {
                            "current_image": "old.jpg",
                            "selected_images": ["old.jpg"],
                        },
                    },
                    "exports": {},
                }
            ),
            encoding="utf-8",
        )

        restored = self.editing.load(self.manga_ref)

        self.assertEqual(restored.last_folder, "Folder 1")
        self.assertEqual(restored.folders["Folder 1"].current_image, "1.jpg")
        self.assertEqual(restored.folders["Folder 1"].selected_images, {"1.jpg"})
        self.assertGreaterEqual(len(restored.warnings), 3)

    def test_unsafe_editing_identities_fail_closed_even_when_nested_under_stale_folders(
        self,
    ) -> None:
        path = self.editing.path_for(self.manga_ref)
        path.parent.mkdir(parents=True)
        base = {
            "schema_version": 1,
            "last_folder": "Folder 1",
            "folders": {
                "Folder 1": {
                    "current_image": "1.jpg",
                    "selected_images": [],
                },
                "Old Folder": {
                    "current_image": "old.jpg",
                    "selected_images": ["old.jpg"],
                },
            },
            "exports": {},
        }
        invalid_payloads = []
        unsafe_last = json.loads(json.dumps(base))
        unsafe_last["last_folder"] = "../Folder 1"
        invalid_payloads.append(unsafe_last)
        unsafe_folder = json.loads(json.dumps(base))
        unsafe_folder["folders"]["../Old Folder"] = unsafe_folder["folders"].pop(
            "Old Folder"
        )
        invalid_payloads.append(unsafe_folder)
        unsafe_current = json.loads(json.dumps(base))
        unsafe_current["folders"]["Old Folder"]["current_image"] = "../old.jpg"
        invalid_payloads.append(unsafe_current)
        non_string_selection = json.loads(json.dumps(base))
        non_string_selection["folders"]["Old Folder"]["selected_images"] = [7]
        invalid_payloads.append(non_string_selection)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(EditingStateError):
                    self.editing.load(self.manga_ref)

    def test_malformed_reading_soft_resets_but_malformed_editing_fails_closed(self) -> None:
        reading_path = self.reading.path_for(self.manga_ref)
        reading_path.parent.mkdir(parents=True)
        reading_path.write_bytes(b"\xff")
        editing_path = self.editing.path_for(self.manga_ref)
        editing_path.write_bytes(b"\xff")

        reading = self.reading.load(self.manga_ref)

        self.assertEqual(reading.last_folder, "Folder 1")
        self.assertEqual(len(reading.warnings), 1)
        with self.assertRaises(EditingStateError):
            self.editing.load(self.manga_ref)
        with self.assertRaises(EditingStateError):
            export_manga(self.root, self.manga_ref)

    def test_one_manga_state_is_isolated_from_another(self) -> None:
        self.add_image("Other", "Folder", "1.jpg")
        other = self.manga("Other")
        self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        self.editing.set_selection(other, "Folder", "1.jpg", True)

        first_path = self.editing.path_for(self.manga_ref)
        other_path = self.editing.path_for(other)

        self.assertNotEqual(first_path.parent, other_path.parent)
        self.assertEqual(first_path.parent.name, "Series")
        self.assertEqual(other_path.parent.name, "Other")

    def test_store_mutations_honor_the_nonblocking_library_lock(self) -> None:
        with library_mutation_lock(self.root):
            with self.assertRaises(LibraryBusyError):
                self.editing.set_selection(
                    self.manga_ref, "Folder 1", "1.jpg", True
                )

    def test_store_refuses_a_symlinked_workspace(self) -> None:
        metadata = self.root / ".pocket-manga-editor"
        metadata.mkdir()
        outside = self.root / "outside-workspace"
        outside.mkdir()
        try:
            (metadata / "Series").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        with self.assertRaises(WorkspaceError):
            self.editing.load(self.manga_ref)

    def test_bad_editing_leaf_does_not_break_reading(self) -> None:
        workspace = manga_workspace_paths(self.root, "Series")
        workspace.workspace.mkdir(parents=True)
        workspace.editing.mkdir()

        restored = self.reading.set_position(
            self.manga_ref, "Folder 2", "10.jpg"
        )

        self.assertEqual(restored.last_folder, "Folder 2")
        self.assertTrue(workspace.reading.is_file())
        with self.assertRaises(WorkspaceError):
            self.editing.load(self.manga_ref)

    def test_bad_reading_leaf_does_not_break_editing(self) -> None:
        workspace = manga_workspace_paths(self.root, "Series")
        workspace.workspace.mkdir(parents=True)
        workspace.reading.mkdir()

        restored = self.editing.set_selection(
            self.manga_ref, "Folder 1", "1.jpg", True
        )

        self.assertEqual(restored.folders["Folder 1"].selected_images, {"1.jpg"})
        self.assertTrue(workspace.editing.is_file())
        with self.assertRaises(WorkspaceError):
            self.reading.load(self.manga_ref)

    def test_editing_rejects_non_authoritative_export_filename(self) -> None:
        path = self.editing.path_for(self.manga_ref)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "last_folder": "Folder 1",
                    "folders": {
                        "Folder 1": {
                            "current_image": "1.jpg",
                            "selected_images": [],
                        },
                        "Folder 2": {
                            "current_image": "2.jpg",
                            "selected_images": [],
                        },
                    },
                    "exports": {
                        "Folder 1": {
                            "files": {
                                "1.jpg": {
                                    "output_name": "plausible-but-wrong.jpg",
                                    "digest": "0" * 64,
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(EditingStateError):
            self.editing.load(self.manga_ref)


class ExporterTests(RepositoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.add_image("Series", "Chapter One", "1.jpg", b"one")
        self.add_image("Series", "Chapter One", "2.PNG", b"two")
        self.add_image("Series", "Chapter One", "10.jpg", b"ten")
        self.add_image("Series", "Odd & Ends", "cover.png", b"cover")
        self.manga_ref = self.manga("Series")
        self.editing = EditingStore(self.root)
        self.workspace = manga_workspace_paths(self.root, "Series")

    def tree_snapshot(self, path: Path) -> dict[str, bytes | None]:
        if not path.exists():
            return {}
        snapshot: dict[str, bytes | None] = {}
        for entry in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
            relative = entry.relative_to(path).as_posix()
            snapshot[relative] = None if entry.is_dir() else entry.read_bytes()
        return snapshot

    def select_initial_images(self) -> None:
        self.editing.save_folder(
            self.manga_ref,
            "Chapter One",
            "2.PNG",
            {"1.jpg", "2.PNG"},
        )
        self.editing.save_folder(
            self.manga_ref,
            "Odd & Ends",
            "cover.png",
            {"cover.png"},
        )

    @unittest.skipIf(os.name == "nt", "Backslash is a path separator on Windows.")
    def test_posix_backslashes_round_trip_as_exact_names_and_export(self) -> None:
        manga_name = r"Series\Exact"
        folder_name = r"Chapter\Odd"
        image_name = r"page\01.JPG"
        self.add_image(manga_name, folder_name, image_name, b"exact")
        manga = self.manga(manga_name)
        editing = EditingStore(self.root)

        saved = editing.save_folder(
            manga,
            folder_name,
            image_name,
            {image_name},
        )

        self.assertEqual(saved.last_folder, folder_name)
        self.assertEqual(saved.folders[folder_name].current_image, image_name)
        self.assertEqual(saved.folders[folder_name].selected_images, {image_name})

        result = export_manga(self.root, manga)
        workspace = manga_workspace_paths(self.root, manga_name)
        output_name = exported_image_name(folder_name, image_name)

        self.assertEqual(result.copied_count, 1)
        self.assertEqual(
            (workspace.output / folder_name / output_name).read_bytes(),
            b"exact",
        )
        restored = editing.load(manga)
        self.assertEqual(restored.last_folder, folder_name)
        self.assertEqual(restored.folders[folder_name].current_image, image_name)
        self.assertEqual(restored.folders[folder_name].selected_images, {image_name})
        self.assertEqual(
            restored.exports[folder_name].files[image_name].output_name,
            output_name,
        )
        inventory = verify_managed_output(workspace, restored)
        self.assertEqual(
            [(folder.folder_name, folder.image_names) for folder in inventory.folders],
            [(folder_name, (image_name,))],
        )

    def test_whole_manga_export_uses_exact_folders_names_and_extensions(self) -> None:
        self.select_initial_images()

        result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.output_directory, self.workspace.output)
        self.assertEqual(result.copied_count, 3)
        self.assertEqual(result.retained_count, 0)
        self.assertEqual(result.removed_count, 0)
        expected = {
            "Chapter One/Chapter One__1.jpg": b"one",
            "Chapter One/Chapter One__2.PNG": b"two",
            "Odd & Ends/Odd & Ends__cover.png": b"cover",
        }
        self.assertEqual(
            {
                name: content
                for name, content in self.tree_snapshot(self.workspace.output).items()
                if content is not None
            },
            expected,
        )
        restored = self.editing.load(self.manga_ref)
        inventory = verify_managed_output(self.workspace, restored)
        self.assertEqual(inventory.output_directory, self.workspace.output)
        self.assertEqual(inventory.image_count, 3)
        self.assertEqual(
            [(folder.folder_name, folder.image_names) for folder in inventory.folders],
            [
                ("Chapter One", ("1.jpg", "2.PNG")),
                ("Odd & Ends", ("cover.png",)),
            ],
        )
        self.assertFalse(
            (self.root / ".pocket-manga-editor" / "output").exists()
        )
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_reconciliation_adds_updates_removes_and_preserves_untracked_files(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        (self.workspace.output / "notes.txt").write_text("keep", encoding="utf-8")
        (self.workspace.output / "Chapter One" / "untracked.txt").write_text(
            "keep too", encoding="utf-8"
        )
        self.add_image("Series", "Chapter One", "2.PNG", b"two changed")
        self.manga_ref = self.manga("Series")
        self.editing.save_folder(
            self.manga_ref,
            "Chapter One",
            "10.jpg",
            {"2.PNG", "10.jpg"},
        )
        self.editing.replace_folder_selections(
            self.manga_ref, "Odd & Ends", set()
        )

        result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.copied_count, 2)
        self.assertEqual(result.retained_count, 0)
        self.assertEqual(result.removed_count, 2)
        self.assertFalse(
            (self.workspace.output / "Chapter One" / "Chapter One__1.jpg").exists()
        )
        self.assertEqual(
            (
                self.workspace.output
                / "Chapter One"
                / "Chapter One__2.PNG"
            ).read_bytes(),
            b"two changed",
        )
        self.assertEqual(
            (
                self.workspace.output
                / "Chapter One"
                / "Chapter One__10.jpg"
            ).read_bytes(),
            b"ten",
        )
        self.assertEqual(
            (self.workspace.output / "Chapter One" / "untracked.txt").read_text(),
            "keep too",
        )
        self.assertEqual((self.workspace.output / "notes.txt").read_text(), "keep")
        self.assertFalse((self.workspace.output / "Odd & Ends").exists())

        repeated = export_manga(self.root, self.manga_ref)
        self.assertEqual(repeated.copied_count, 0)
        self.assertEqual(repeated.retained_count, 2)
        self.assertEqual(repeated.removed_count, 0)

    def test_committed_cleanup_handles_read_only_preserved_output(
        self,
    ) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        locked = self.workspace.output / "read-only untracked"
        locked.mkdir()
        note = locked / "keep.txt"
        note.write_text("preserve", encoding="utf-8")
        note.chmod(stat.S_IRUSR)
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)

        try:
            result = export_manga(self.root, self.manga_ref)

            self.assertEqual(result.warnings, ())
            self.assertEqual(list(self.workspace.transactions.iterdir()), [])
            recovered = recover_interrupted_exports(self.root)
            self.assertEqual(recovered.recovered_count, 0)
            self.assertEqual(note.read_text(encoding="utf-8"), "preserve")
        finally:
            if note.exists():
                note.chmod(stat.S_IRUSR | stat.S_IWUSR)
            if locked.exists():
                locked.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def test_committed_cleanup_removes_payload_before_retiring_its_journal(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        real_remove = exporter_module.remove_managed_path
        interrupted = False

        def interrupt_after_old_output(path: Path) -> None:
            nonlocal interrupted
            real_remove(path)
            if Path(path).name == "old-output" and not interrupted:
                interrupted = True
                raise OSError("interrupted after deleting old output")

        with patch(
            "pocket_manga_editor.exporter.remove_managed_path",
            side_effect=interrupt_after_old_output,
        ):
            result = export_manga(self.root, self.manga_ref)

        self.assertTrue(result.warnings)
        transactions = list(self.workspace.transactions.glob("export-*"))
        self.assertEqual(len(transactions), 1)
        self.assertTrue((transactions[0] / "transaction.json").is_file())
        self.assertFalse((transactions[0] / "old-output").exists())

        recovered = recover_interrupted_exports(self.root)
        self.assertEqual(recovered.committed_count, 1)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_unchanged_managed_images_reuse_the_staged_output_copy(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)

        with patch(
            "pocket_manga_editor.exporter._copy_source_image",
            side_effect=AssertionError("retained images must not be recopied"),
        ):
            result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.copied_count, 0)
        self.assertEqual(result.retained_count, 3)
        self.assertEqual(
            verify_managed_output(
                self.workspace, self.editing.load(self.manga_ref)
            ).image_count,
            3,
        )

    def test_export_uses_safe_defaults_when_pathconf_is_unavailable(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )

        with patch.object(exporter_module.os, "pathconf", None):
            result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.copied_count, 1)
        self.assertEqual(
            (
                self.workspace.output
                / "Chapter One"
                / "Chapter One__1.jpg"
            ).read_bytes(),
            b"one",
        )

    def test_output_path_lookup_ignores_unrelated_bad_managed_leaves(self) -> None:
        self.workspace.workspace.mkdir(parents=True)
        self.workspace.reading.mkdir()
        self.workspace.editing.mkdir()
        self.workspace.transactions.write_text("unsafe", encoding="utf-8")

        destination = manga_output_directory(self.root, self.manga_ref)

        self.assertEqual(destination, self.workspace.output)

    def test_deselecting_everything_removes_only_managed_output(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        (self.workspace.output / "keep.txt").write_text("untracked", encoding="utf-8")
        self.editing.replace_folder_selections(
            self.manga_ref, "Chapter One", set()
        )
        self.editing.replace_folder_selections(
            self.manga_ref, "Odd & Ends", set()
        )

        result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.removed_count, 3)
        self.assertEqual((self.workspace.output / "keep.txt").read_text(), "untracked")
        self.assertEqual(self.editing.load(self.manga_ref).exports, {})
        inventory = verify_managed_output(
            self.workspace, self.editing.load(self.manga_ref)
        )
        self.assertEqual(inventory.image_count, 0)

    def test_export_with_no_selection_or_history_is_refused(self) -> None:
        with self.assertRaisesRegex(ExportError, "Select at least one"):
            export_manga(self.root, self.manga_ref)

        self.assertFalse(self.workspace.output.exists())

    def test_export_honors_the_nonblocking_library_lock(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )

        with library_mutation_lock(self.root):
            with self.assertRaises(ExportBusyError):
                export_manga(self.root, self.manga_ref)

    def test_modified_managed_file_aborts_before_any_manga_output_changes(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        managed = (
            self.workspace.output / "Chapter One" / "Chapter One__1.jpg"
        )
        managed.write_bytes(b"user changed")
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "10.jpg", True
        )
        before = self.tree_snapshot(self.workspace.output)
        editing_before = self.workspace.editing.read_bytes()

        with self.assertRaisesRegex(ExportConflict, "changed"):
            export_manga(self.root, self.manga_ref)

        self.assertEqual(self.tree_snapshot(self.workspace.output), before)
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)

    def test_live_output_change_after_staging_is_preserved_and_aborts_export(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "10.jpg", True
        )
        editing_before = self.workspace.editing.read_bytes()
        real_copytree = exporter_module.shutil.copytree

        def copy_then_change_live_output(source, destination, *args, **kwargs):
            copied = real_copytree(source, destination, *args, **kwargs)
            (self.workspace.output / "late-user-file.txt").write_text(
                "preserve me", encoding="utf-8"
            )
            return copied

        with patch(
            "pocket_manga_editor.exporter.shutil.copytree",
            side_effect=copy_then_change_live_output,
        ):
            with self.assertRaisesRegex(ExportConflict, "changed"):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(
            (self.workspace.output / "late-user-file.txt").read_text(),
            "preserve me",
        )
        self.assertFalse(
            (
                self.workspace.output
                / "Chapter One"
                / "Chapter One__10.jpg"
            ).exists()
        )
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_retained_source_change_after_plan_aborts_before_commit(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        output_before = self.tree_snapshot(self.workspace.output)
        editing_before = self.workspace.editing.read_bytes()
        source = self.root / "Series" / "Chapter One" / "1.jpg"
        real_apply_plan = exporter_module._apply_plan

        def apply_then_change_source(staged_output, plan):
            result = real_apply_plan(staged_output, plan)
            source.write_bytes(b"changed after plan")
            return result

        with patch(
            "pocket_manga_editor.exporter._apply_plan",
            side_effect=apply_then_change_source,
        ):
            with self.assertRaisesRegex(ExportConflict, "Source image.*changed"):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(self.tree_snapshot(self.workspace.output), output_before)
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
        self.assertEqual(source.read_bytes(), b"changed after plan")
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_output_change_during_original_rename_is_restored_without_loss(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "10.jpg", True
        )
        editing_before = self.workspace.editing.read_bytes()
        managed_relative = Path("Chapter One") / "Chapter One__1.jpg"
        real_replace = exporter_module.os.replace

        def move_then_change_original(source, destination):
            result = real_replace(source, destination)
            if (
                Path(source) == self.workspace.output
                and Path(destination).name == "old-output"
            ):
                (Path(destination) / managed_relative).write_bytes(
                    b"changed at rename boundary"
                )
            return result

        with patch(
            "pocket_manga_editor.exporter.os.replace",
            side_effect=move_then_change_original,
        ):
            with self.assertRaisesRegex(ExportConflict, "immediately before"):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(
            (self.workspace.output / managed_relative).read_bytes(),
            b"changed at rename boundary",
        )
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_new_output_install_never_replaces_a_late_destination(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )
        editing_before = self.workspace.editing.read_bytes()
        real_install = exporter_module._rename_no_replace

        def create_destination_before_install(source, destination):
            Path(destination).mkdir()
            (Path(destination) / "late-user-file.txt").write_text(
                "preserve me", encoding="utf-8"
            )
            return real_install(source, destination)

        with patch(
            "pocket_manga_editor.exporter._rename_no_replace",
            side_effect=create_destination_before_install,
        ):
            with self.assertRaisesRegex(ExportConflict, "appeared"):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(
            (self.workspace.output / "late-user-file.txt").read_text(),
            "preserve me",
        )
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_untracked_file_at_a_selected_destination_is_never_adopted(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )
        target = self.workspace.output / "Chapter One" / "Chapter One__1.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"one")

        with self.assertRaisesRegex(ExportConflict, "Untracked output"):
            export_manga(self.root, self.manga_ref)

        self.assertEqual(target.read_bytes(), b"one")
        self.assertEqual(self.editing.load(self.manga_ref).exports, {})

    def test_casefold_folder_collisions_are_rejected_before_mutation(self) -> None:
        upper = self.root / "Collision" / "Foo"
        lower = self.root / "Collision" / "foo"
        upper.mkdir(parents=True)
        lower.mkdir(parents=True, exist_ok=True)
        if upper.samefile(lower):
            self.skipTest("The test filesystem is case-insensitive.")
        (upper / "a.jpg").write_bytes(b"a")
        (lower / "b.jpg").write_bytes(b"b")
        manga = self.manga("Collision")
        editing = EditingStore(self.root)
        editing.set_selection(manga, "Foo", "a.jpg", True)
        editing.set_selection(manga, "foo", "b.jpg", True)

        with self.assertRaisesRegex(ExportConflict, "case-insensitive"):
            export_manga(self.root, manga)

        self.assertFalse(manga_workspace_paths(self.root, "Collision").output.exists())

    def test_output_component_length_is_preflighted(self) -> None:
        folder_name = "f" * 140
        image_name = f"{'i' * 115}.jpg"
        self.add_image("Long", folder_name, image_name, b"long")
        manga = self.manga("Long")
        EditingStore(self.root).set_selection(
            manga, folder_name, image_name, True
        )

        with self.assertRaisesRegex(ExportError, "too long"):
            export_manga(self.root, manga)

        self.assertFalse(manga_workspace_paths(self.root, "Long").output.exists())

    def test_each_precommit_failure_restores_the_entire_manga(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "10.jpg", True
        )
        output_before = self.tree_snapshot(self.workspace.output)
        editing_before = self.workspace.editing.read_bytes()
        real_atomic_write = exporter_module.atomic_write_json
        real_replace = exporter_module.os.replace
        real_no_replace = exporter_module.rename_no_replace

        def fail_new_editing(path, payload):
            if Path(path).name == "new-editing.json":
                raise OSError("injected new-editing failure")
            return real_atomic_write(path, payload)

        def fail_journal(path, payload):
            if Path(path).name == "transaction.json":
                raise OSError("injected journal failure")
            return real_atomic_write(path, payload)

        def replace_failure(source_name: str, destination: Path):
            def fail(source, target):
                source_path = Path(source)
                target_path = Path(target)
                if source_path.name == source_name and target_path == destination:
                    raise OSError(f"injected {source_name} failure")
                return real_replace(source, target)

            return fail

        def fail_new_output_rename(source, target):
            if (
                Path(source).name == "new-output"
                and Path(target) == self.workspace.output
            ):
                raise OSError("injected new-output failure")
            return real_no_replace(source, target)

        failures = [
            (
                "staged editing document",
                patch(
                "pocket_manga_editor.exporter.atomic_write_json",
                side_effect=fail_new_editing,
                ),
            ),
            (
                "transaction journal",
                patch(
                "pocket_manga_editor.exporter.atomic_write_json",
                side_effect=fail_journal,
                ),
            ),
            (
                "new output install",
                patch(
                    "pocket_manga_editor.exporter.rename_no_replace",
                side_effect=fail_new_output_rename,
                ),
            ),
            (
                "editing commit",
                patch(
                "pocket_manga_editor.exporter.os.replace",
                side_effect=replace_failure("new-editing.json", self.workspace.editing),
                ),
            ),
        ]

        def fail_old_output(source, target):
            if Path(source) == self.workspace.output and Path(target).name == "old-output":
                raise OSError("injected old-output failure")
            return real_replace(source, target)

        failures.insert(
            2,
            (
                "old output staging",
                patch(
                    "pocket_manga_editor.exporter.os.replace",
                    side_effect=fail_old_output,
                ),
            ),
        )

        for label, failure in failures:
            with self.subTest(failure=label):
                with failure:
                    with self.assertRaises(ExportError):
                        export_manga(self.root, self.manga_ref)
                self.assertEqual(self.tree_snapshot(self.workspace.output), output_before)
                self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
                self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_incomplete_precommit_rollback_is_recovered_from_journal(self) -> None:
        self.select_initial_images()
        export_manga(self.root, self.manga_ref)
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "10.jpg", True
        )
        output_before = self.tree_snapshot(self.workspace.output)
        editing_before = self.workspace.editing.read_bytes()
        real_replace = exporter_module.os.replace

        def fail_editing_install(source, target):
            if (
                Path(source).name == "new-editing.json"
                and Path(target) == self.workspace.editing
            ):
                raise OSError("simulated process interruption")
            return real_replace(source, target)

        with patch(
            "pocket_manga_editor.exporter.os.replace",
            side_effect=fail_editing_install,
        ), patch(
            "pocket_manga_editor.exporter._rollback_transaction",
            return_value=["simulated incomplete rollback"],
        ):
            with self.assertRaises(ExportRecoveryError):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(len(list(self.workspace.transactions.iterdir())), 1)
        recovered = recover_interrupted_exports(self.root)
        self.assertEqual(recovered.rolled_back_count, 1)
        self.assertEqual(self.tree_snapshot(self.workspace.output), output_before)
        self.assertEqual(self.workspace.editing.read_bytes(), editing_before)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])

    def test_committed_export_cleanup_is_finished_by_recovery(self) -> None:
        self.select_initial_images()
        real_retire = exporter_module._retire_export_transaction
        failed = False

        def interrupt_cleanup(path, transaction_root):
            nonlocal failed
            if Path(path).name.startswith("export-") and not failed:
                failed = True
                raise OSError("simulated cleanup interruption")
            return real_retire(path, transaction_root)

        with patch(
            "pocket_manga_editor.exporter._retire_export_transaction",
            side_effect=interrupt_cleanup,
        ):
            result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.copied_count, 3)
        self.assertTrue(result.warnings)
        self.assertEqual(len(list(self.workspace.transactions.iterdir())), 1)
        recovered = recover_interrupted_exports(self.root)
        self.assertEqual(recovered.committed_count, 1)
        self.assertEqual(list(self.workspace.transactions.iterdir()), [])
        self.assertEqual(
            verify_managed_output(
                self.workspace, self.editing.load(self.manga_ref)
            ).image_count,
            3,
        )

    def test_markerless_preparation_artifact_is_discarded(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )
        transaction = self.workspace.transactions / "export-orphan"
        (transaction / "new-output").mkdir(parents=True)
        (transaction / "new-output" / "temporary.jpg").write_bytes(b"temporary")

        with library_mutation_lock(self.root) as locked_root:
            result = recover_interrupted_exports_locked(locked_root)

        self.assertEqual(result.discarded_count, 1)
        self.assertFalse(transaction.exists())

    def test_malformed_export_journal_fails_closed(self) -> None:
        self.editing.set_selection(
            self.manga_ref, "Chapter One", "1.jpg", True
        )
        transaction = self.workspace.transactions / "export-bad"
        transaction.mkdir(parents=True)
        (transaction / "transaction.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(ExportRecoveryError):
            recover_interrupted_exports(self.root)

        self.assertTrue(transaction.exists())

    def test_transaction_free_bad_leaf_does_not_block_other_manga_recovery(self) -> None:
        bad_workspace = manga_workspace_paths(self.root, "Series")
        bad_workspace.workspace.mkdir(parents=True, exist_ok=True)
        bad_workspace.editing.mkdir()
        self.add_image("Other", "Folder", "1.jpg", b"other")
        other = self.manga("Other")
        EditingStore(self.root).set_selection(other, "Folder", "1.jpg", True)

        recovered = recover_interrupted_exports(self.root)
        completion_recovered = recover_interrupted_completions(self.root)
        exported = export_manga(self.root, other)

        self.assertEqual(recovered.recovered_count, 0)
        self.assertEqual(completion_recovered.rolled_back_count, 0)
        self.assertEqual(completion_recovered.cleaned_count, 0)
        self.assertEqual(exported.copied_count, 1)
        self.assertTrue(
            manga_workspace_paths(self.root, "Other").output.is_dir()
        )


if __name__ == "__main__":
    unittest.main()
