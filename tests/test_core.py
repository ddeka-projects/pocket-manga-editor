from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pocket_manga_editor.exporter as exporter_module
from pocket_manga_editor.config import ConfigurationError, load_configuration
from pocket_manga_editor.exporter import (
    ExportBusyError,
    ExportConfirmationRequired,
    ExportConflict,
    ExportError,
    ExportRecoveryError,
    NothingSelectedError,
    export_manga,
    exported_image_name,
    inspect_export,
    manga_output_directory,
    recover_interrupted_exports,
)
from pocket_manga_editor.filesystem_ops import remove_managed_path, rename_no_replace
from pocket_manga_editor.library_lock import (
    LibraryBusyError,
    LibraryLockError,
    library_mutation_lock,
)
from pocket_manga_editor.models import FolderRef, ImageRef, MangaRef
from pocket_manga_editor.path_safety import is_link_or_reparse
from pocket_manga_editor.scanner import ScanError, scan_working_directory
from pocket_manga_editor.storage import (
    STATE_SCHEMA_VERSION,
    EditingStateError,
    EditingStore,
    ReadingStore,
)
from pocket_manga_editor.workspace import WorkspaceError, manga_workspace_paths


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

    def manga(self, name: str = "Kimi wa 08") -> MangaRef:
        result = scan_working_directory(self.root)
        return next(manga for manga in result.mangas if manga.name == name)


class ConfigurationTests(RepositoryFixture):
    def test_env_file_loads_absolute_library_host_and_port(self) -> None:
        library = self.root / "Manga Library"
        library.mkdir()
        env_file = self.root / ".env"
        env_file.write_text(
            "\n".join(
                (
                    "# Local server",
                    f'POCKET_MANGA_EDITOR_WORKING_DIRECTORY="{library}"',
                    "POCKET_MANGA_EDITOR_HOST=0.0.0.0",
                    "POCKET_MANGA_EDITOR_PORT=9123",
                )
            ),
            encoding="utf-8",
        )

        configuration = load_configuration(env_file, environ={})

        self.assertEqual(configuration.working_directory, library.resolve())
        self.assertEqual(configuration.host, "0.0.0.0")
        self.assertEqual(configuration.port, 9123)

    def test_real_environment_overrides_file_values(self) -> None:
        file_library = self.root / "File Library"
        process_library = self.root / "Process Library"
        file_library.mkdir()
        process_library.mkdir()
        env_file = self.root / ".env"
        env_file.write_text(
            f"POCKET_MANGA_EDITOR_WORKING_DIRECTORY={file_library}\n"
            "POCKET_MANGA_EDITOR_PORT=8000\n",
            encoding="utf-8",
        )

        configuration = load_configuration(
            env_file,
            environ={
                "POCKET_MANGA_EDITOR_WORKING_DIRECTORY": str(process_library),
                "POCKET_MANGA_EDITOR_HOST": "127.0.0.1",
                "POCKET_MANGA_EDITOR_PORT": "9000",
            },
        )

        self.assertEqual(configuration.working_directory, process_library.resolve())
        self.assertEqual(configuration.host, "127.0.0.1")
        self.assertEqual(configuration.port, 9000)

    def test_configuration_rejects_relative_missing_and_linked_library_paths(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "absolute"):
            load_configuration(
                self.root / "missing.env",
                environ={"POCKET_MANGA_EDITOR_WORKING_DIRECTORY": "relative"},
            )
        with self.assertRaises(ConfigurationError):
            load_configuration(
                self.root / "missing.env",
                environ={
                    "POCKET_MANGA_EDITOR_WORKING_DIRECTORY": str(
                        self.root / "does-not-exist"
                    )
                },
            )

        library = self.root / "Library"
        library.mkdir()
        alias = self.root / "Alias"
        try:
            alias.symlink_to(library, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")
        with self.assertRaisesRegex(ConfigurationError, "link|junction"):
            load_configuration(
                self.root / "missing.env",
                environ={"POCKET_MANGA_EDITOR_WORKING_DIRECTORY": str(alias)},
            )

    def test_configuration_rejects_bad_host_port_and_env_syntax(self) -> None:
        library = self.root / "Library"
        library.mkdir()
        base = {"POCKET_MANGA_EDITOR_WORKING_DIRECTORY": str(library)}
        for name, value in (
            ("POCKET_MANGA_EDITOR_HOST", "host name"),
            ("POCKET_MANGA_EDITOR_HOST", "::1"),
            ("POCKET_MANGA_EDITOR_PORT", "0"),
            ("POCKET_MANGA_EDITOR_PORT", "not-a-port"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ConfigurationError):
                    load_configuration(
                        self.root / "missing.env", environ={**base, name: value}
                    )

        malformed = self.root / ".env"
        malformed.write_text("this is not an assignment\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "line 1"):
            load_configuration(malformed, environ=base)


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

    def test_keeps_same_stem_different_extensions_as_distinct_images(self) -> None:
        self.add_image("Series", "Anything", "004.jpg")
        self.add_image("Series", "Anything", "004.PNG")

        images = self.manga("Series").folders[0].images

        self.assertEqual([image.name for image in images], ["004.jpg", "004.PNG"])

    def test_ignores_empty_nested_direct_and_unrelated_content(self) -> None:
        self.add_image("Series", "Valid", "cover.jpg")
        (self.root / "Series" / "Empty").mkdir()
        (self.root / "Series" / "Valid" / "notes.txt").write_text(
            "ignored", encoding="utf-8"
        )
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
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
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
                return SimpleNamespace(st_mode=information.st_mode, st_dev=root_device + 1)
            return information

        with patch.object(Path, "stat", cross_device_stat):
            with self.assertRaisesRegex(OSError, "mounted filesystem"):
                remove_managed_path(tree)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

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
                    st_mode=information.st_mode, st_dev=parent_device + 1
                )
            return information

        with patch.object(Path, "stat", mounted_root_stat):
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

        access_mode_mask = getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
        self.assertEqual(opened_flags[0] & access_mode_mask, os.O_RDWR)
        fsync.assert_called_once()
        self.assertFalse(stat.S_IMODE(staged.stat().st_mode) & stat.S_IWRITE)


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
        self.assertEqual((initial.last_folder, initial.last_image), ("Folder 1", "1.jpg"))

        read = self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")
        self.editing.set_position(self.manga_ref, "Folder 1", "2.PNG")
        edit = self.editing.set_selection(self.manga_ref, "Folder 1", "2.PNG", True)

        self.assertEqual((read.last_folder, read.last_image), ("Folder 2", "10.jpg"))
        self.assertEqual((edit.last_folder, edit.last_image), ("Folder 1", "2.PNG"))
        self.assertEqual(edit.folders["Folder 1"].selected_images, {"2.PNG"})
        self.assertNotIn("selected_images", self.reading.path_for(self.manga_ref).read_text())

    def test_reading_remembers_only_latest_pair_in_schema_three(self) -> None:
        self.reading.set_position(self.manga_ref, "Folder 1", "2.PNG")
        self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")

        restored = self.reading.load(self.manga_ref)
        payload = json.loads(self.reading.path_for(self.manga_ref).read_text())

        self.assertEqual((restored.last_folder, restored.last_image), ("Folder 2", "10.jpg"))
        self.assertEqual(
            payload,
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "last_folder": "Folder 2",
                "last_image": "10.jpg",
            },
        )

    def test_editing_payload_contains_only_pair_and_sparse_selections(self) -> None:
        self.editing.set_selection(self.manga_ref, "Folder 1", "2.PNG", True)
        self.editing.set_position(self.manga_ref, "Folder 2", "10.jpg")

        payload = json.loads(self.editing.path_for(self.manga_ref).read_text())

        self.assertEqual(
            payload,
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "last_folder": "Folder 2",
                "last_image": "10.jpg",
                "folders": {"Folder 1": {"selected_images": ["2.PNG"]}},
            },
        )
        self.assertNotIn("exports", payload)

    def test_selection_mutation_does_not_change_position_and_empty_is_sparse(self) -> None:
        self.editing.set_position(self.manga_ref, "Folder 1", "2.PNG")
        updated = self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        self.assertEqual((updated.last_folder, updated.last_image), ("Folder 1", "2.PNG"))

        cleared = self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", False)
        self.assertEqual(cleared.folders, {})
        payload = json.loads(self.editing.path_for(self.manga_ref).read_text())
        self.assertEqual(payload["folders"], {})

    def test_selection_validates_only_target_in_a_340_folder_snapshot(self) -> None:
        uninspected = tuple(
            FolderRef(
                f"Chapter {number}",
                self.root / "Series" / f"Chapter {number}",
                (
                    ImageRef(
                        "missing.jpg",
                        self.root / "Series" / f"Chapter {number}" / "missing.jpg",
                    ),
                ),
            )
            for number in range(3, 341)
        )
        large = MangaRef(
            self.manga_ref.name,
            self.manga_ref.path,
            self.manga_ref.folders + uninspected,
        )

        self.editing.set_selection(large, "Folder 1", "1.jpg", True)
        self.editing.set_position(large, "Folder 2", "10.jpg")

        payload = json.loads(self.editing.path_for(large).read_text())
        self.assertEqual(set(payload["folders"]), {"Folder 1"})
        self.assertEqual((payload["last_folder"], payload["last_image"]), ("Folder 2", "10.jpg"))

    def test_stale_position_and_selections_fall_back_without_rename_inference(self) -> None:
        path = self.editing.path_for(self.manga_ref)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "last_folder": "Renamed Folder",
                    "last_image": "missing.jpg",
                    "folders": {
                        "Folder 1": {
                            "selected_images": ["1.jpg", "renamed.png", "gone.png"]
                        },
                        "Old Folder": {"selected_images": ["old.jpg"]},
                    },
                }
            ),
            encoding="utf-8",
        )

        restored = self.editing.load(self.manga_ref)

        self.assertEqual((restored.last_folder, restored.last_image), ("Folder 1", "1.jpg"))
        self.assertEqual(restored.folders["Folder 1"].selected_images, {"1.jpg"})
        self.assertGreaterEqual(len(restored.warnings), 3)

    def test_missing_resume_image_resets_whole_pair_to_manga_start(self) -> None:
        reading_path = self.reading.path_for(self.manga_ref)
        reading_path.parent.mkdir(parents=True)
        reading_path.write_text(
            json.dumps(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "last_folder": "Folder 2",
                    "last_image": "missing.jpg",
                }
            ),
            encoding="utf-8",
        )
        editing_path = self.editing.path_for(self.manga_ref)
        editing_path.write_text(
            json.dumps(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "last_folder": "Folder 2",
                    "last_image": "missing.jpg",
                    "folders": {},
                }
            ),
            encoding="utf-8",
        )

        reading = self.reading.load(self.manga_ref)
        editing = self.editing.load(self.manga_ref)

        self.assertEqual((reading.last_folder, reading.last_image), ("Folder 1", "1.jpg"))
        self.assertEqual((editing.last_folder, editing.last_image), ("Folder 1", "1.jpg"))

    def test_unsafe_editing_identities_fail_closed_even_when_stale(self) -> None:
        path = self.editing.path_for(self.manga_ref)
        path.parent.mkdir(parents=True)
        base = {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_folder": "Folder 1",
            "last_image": "1.jpg",
            "folders": {"Old Folder": {"selected_images": ["old.jpg"]}},
        }
        invalid_payloads = []
        for key, value in (("last_folder", "../Folder 1"), ("last_image", "../old.jpg")):
            payload = json.loads(json.dumps(base))
            payload[key] = value
            invalid_payloads.append(payload)
        unsafe_folder = json.loads(json.dumps(base))
        unsafe_folder["folders"]["../Old Folder"] = unsafe_folder["folders"].pop("Old Folder")
        invalid_payloads.append(unsafe_folder)
        non_string = json.loads(json.dumps(base))
        non_string["folders"]["Old Folder"]["selected_images"] = [7]
        invalid_payloads.append(non_string)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(EditingStateError):
                    self.editing.load(self.manga_ref)

    def test_malformed_reading_soft_resets_but_editing_fails_closed(self) -> None:
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

    def test_old_schema_is_a_clean_break(self) -> None:
        reading_path = self.reading.path_for(self.manga_ref)
        reading_path.parent.mkdir(parents=True)
        reading_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "last_folder": "Folder 2",
                    "last_image": "10.jpg",
                }
            ),
            encoding="utf-8",
        )
        editing_path = self.editing.path_for(self.manga_ref)
        editing_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "last_folder": "Folder 1",
                    "last_image": "1.jpg",
                    "folders": {},
                    "exports": {},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.reading.load(self.manga_ref).last_folder, "Folder 1")
        with self.assertRaises(EditingStateError):
            self.editing.load(self.manga_ref)

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

    def test_store_mutations_honor_nonblocking_library_lock(self) -> None:
        with library_mutation_lock(self.root):
            with self.assertRaises(LibraryBusyError):
                self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)

    def test_store_refuses_symlinked_workspace(self) -> None:
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

    def test_bad_metadata_leafs_are_isolated_by_activity(self) -> None:
        workspace = manga_workspace_paths(self.root, "Series")
        workspace.workspace.mkdir(parents=True)
        workspace.editing.mkdir()

        restored = self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")

        self.assertEqual(restored.last_folder, "Folder 2")
        with self.assertRaises(WorkspaceError):
            self.editing.load(self.manga_ref)

        workspace.editing.rmdir()
        workspace.reading.unlink()
        workspace.reading.mkdir()
        changed = self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        self.assertEqual(changed.folders["Folder 1"].selected_images, {"1.jpg"})
        with self.assertRaises(WorkspaceError):
            self.reading.load(self.manga_ref)


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

    @staticmethod
    def tree_snapshot(path: Path) -> dict[str, bytes | None]:
        if not path.exists():
            return {}
        snapshot: dict[str, bytes | None] = {}
        for entry in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
            relative = entry.relative_to(path).as_posix()
            snapshot[relative] = None if entry.is_dir() else entry.read_bytes()
        return snapshot

    def select(self, *identities: tuple[str, str]) -> None:
        for folder_name, image_name in identities:
            self.editing.set_selection(
                self.manga_ref, folder_name, image_name, True
            )

    def deselect(self, *identities: tuple[str, str]) -> None:
        for folder_name, image_name in identities:
            self.editing.set_selection(
                self.manga_ref, folder_name, image_name, False
            )

    def target(self, folder_name: str, image_name: str) -> Path:
        return (
            self.workspace.output
            / folder_name
            / exported_image_name(folder_name, image_name)
        )

    def test_inspection_and_first_export_use_exact_fresh_tree(self) -> None:
        self.select(
            ("Chapter One", "1.jpg"),
            ("Chapter One", "2.PNG"),
            ("Odd & Ends", "cover.png"),
        )

        preview = inspect_export(self.root, self.manga_ref)

        self.assertEqual(preview.output_directory, self.workspace.output)
        self.assertEqual(preview.selected_folder_count, 2)
        self.assertEqual(preview.selected_image_count, 3)
        self.assertFalse(preview.output_exists)
        self.assertFalse(preview.requires_confirmation)
        result = export_manga(self.root, self.manga_ref)
        self.assertEqual((result.folder_count, result.image_count), (2, 3))
        self.assertEqual(result.warnings, ())
        self.assertEqual(self.target("Chapter One", "1.jpg").read_bytes(), b"one")
        self.assertEqual(self.target("Chapter One", "2.PNG").read_bytes(), b"two")
        self.assertEqual(self.target("Odd & Ends", "cover.png").read_bytes(), b"cover")

    def test_subsequent_export_replaces_entire_prior_output(self) -> None:
        self.select(("Chapter One", "1.jpg"), ("Odd & Ends", "cover.png"))
        export_manga(self.root, self.manga_ref)
        old_only = self.target("Odd & Ends", "cover.png")
        self.assertTrue(old_only.is_file())

        self.deselect(("Chapter One", "1.jpg"), ("Odd & Ends", "cover.png"))
        self.select(("Chapter One", "2.PNG"), ("Chapter One", "10.jpg"))
        result = export_manga(self.root, self.manga_ref)

        self.assertEqual((result.folder_count, result.image_count), (1, 2))
        self.assertEqual(
            self.tree_snapshot(self.workspace.output),
            {
                "Chapter One": None,
                "Chapter One/Chapter One__2.PNG": b"two",
                "Chapter One/Chapter One__10.jpg": b"ten",
            },
        )
        self.assertFalse(old_only.exists())

    def test_zero_selection_refuses_and_preserves_every_existing_output_byte(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        (self.workspace.output / "test.txt").write_bytes(b"external")
        self.deselect(("Chapter One", "1.jpg"))
        before = self.tree_snapshot(self.workspace.output)

        preview = inspect_export(self.root, self.manga_ref)
        self.assertEqual(preview.selected_image_count, 0)
        self.assertEqual(preview.unrecognized_entries, ())
        with self.assertRaisesRegex(NothingSelectedError, "Nothing selected"):
            export_manga(
                self.root,
                self.manga_ref,
                confirm_unrecognized_output=True,
            )

        self.assertEqual(self.tree_snapshot(self.workspace.output), before)

    def test_unrecognized_output_requires_confirmation_then_is_deleted(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        (self.workspace.output / "test.txt").write_bytes(b"external")
        nested = self.workspace.output / "Chapter One" / "nested"
        nested.mkdir()
        (nested / "extra.jpg").write_bytes(b"external image")
        before = self.tree_snapshot(self.workspace.output)

        preview = inspect_export(self.root, self.manga_ref)

        self.assertTrue(preview.requires_confirmation)
        self.assertIn("test.txt", preview.unrecognized_entries)
        self.assertIn("Chapter One/nested/", preview.unrecognized_entries)
        self.assertIn("Chapter One/nested/extra.jpg", preview.unrecognized_entries)
        with self.assertRaises(ExportConfirmationRequired) as raised:
            export_manga(self.root, self.manga_ref)
        self.assertEqual(raised.exception.preview, preview)
        self.assertEqual(self.tree_snapshot(self.workspace.output), before)

        export_manga(
            self.root,
            self.manga_ref,
            confirm_unrecognized_output=True,
        )
        self.assertEqual(
            self.tree_snapshot(self.workspace.output),
            {
                "Chapter One": None,
                "Chapter One/Chapter One__1.jpg": b"one",
            },
        )

    def test_output_links_hard_fail_even_when_replacement_is_confirmed(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        link = self.workspace.output / "linked.jpg"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symbolic links are unavailable: {exc}")

        with self.assertRaisesRegex(ExportError, "link"):
            inspect_export(self.root, self.manga_ref)
        with self.assertRaisesRegex(ExportError, "link"):
            export_manga(
                self.root,
                self.manga_ref,
                confirm_unrecognized_output=True,
            )
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(link.is_symlink())

    def test_export_never_changes_editing_metadata(self) -> None:
        self.select(("Chapter One", "2.PNG"), ("Odd & Ends", "cover.png"))
        self.editing.set_position(self.manga_ref, "Chapter One", "10.jpg")
        editing_path = self.editing.path_for(self.manga_ref)
        before = editing_path.read_bytes()

        export_manga(self.root, self.manga_ref)

        self.assertEqual(editing_path.read_bytes(), before)
        payload = json.loads(before)
        self.assertNotIn("exports", payload)

    def test_copy_failure_before_journal_preserves_prior_output(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        prior = self.tree_snapshot(self.workspace.output)
        self.deselect(("Chapter One", "1.jpg"))
        self.select(("Chapter One", "2.PNG"), ("Chapter One", "10.jpg"))
        real_copy = exporter_module._copy_source_image

        def fail_second(folder, image, destination):
            if image.name == "10.jpg":
                raise ExportError("simulated copy failure")
            return real_copy(folder, image, destination)

        with patch.object(exporter_module, "_copy_source_image", side_effect=fail_second):
            with self.assertRaisesRegex(ExportError, "simulated copy failure"):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(self.tree_snapshot(self.workspace.output), prior)
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_source_change_after_staging_aborts_and_preserves_prior_output(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        prior = self.tree_snapshot(self.workspace.output)
        self.select(("Chapter One", "10.jpg"))
        source = self.root / "Series" / "Chapter One" / "10.jpg"
        real_revalidate = exporter_module._revalidate_sources

        def change_source(desired):
            source.write_bytes(b"changed after staging")
            return real_revalidate(desired)

        with patch.object(exporter_module, "_revalidate_sources", side_effect=change_source):
            with self.assertRaises(ExportConflict):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(self.tree_snapshot(self.workspace.output), prior)
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_live_output_change_during_staging_is_never_overwritten(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        self.select(("Chapter One", "2.PNG"))
        active = self.target("Chapter One", "1.jpg")
        real_revalidate = exporter_module._revalidate_sources

        def change_output(desired):
            result = real_revalidate(desired)
            active.write_bytes(b"changed by another actor")
            return result

        with patch.object(exporter_module, "_revalidate_sources", side_effect=change_output):
            with self.assertRaises(ExportRecoveryError):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(active.read_bytes(), b"changed by another actor")
        self.assertTrue(any(self.workspace.transactions.iterdir()))

    def test_late_destination_collision_preserves_both_copies_for_recovery(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        prior = self.tree_snapshot(self.workspace.output)
        self.select(("Chapter One", "2.PNG"))
        real_rename = exporter_module._rename_no_replace

        def collide(source: Path, destination: Path):
            if source.name == "new-output":
                destination.mkdir()
                (destination / "late.txt").write_bytes(b"late")
            return real_rename(source, destination)

        with patch.object(exporter_module, "_rename_no_replace", side_effect=collide):
            with self.assertRaises(ExportRecoveryError):
                export_manga(self.root, self.manga_ref)

        self.assertEqual((self.workspace.output / "late.txt").read_bytes(), b"late")
        transactions = tuple(self.workspace.transactions.iterdir())
        self.assertEqual(len(transactions), 1)
        self.assertEqual(self.tree_snapshot(transactions[0] / "old-output"), prior)

    def test_prepared_transaction_is_rolled_back_on_next_recovery(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        prior = self.tree_snapshot(self.workspace.output)
        self.deselect(("Chapter One", "1.jpg"))
        self.select(("Chapter One", "2.PNG"))
        real_atomic_write = exporter_module.atomic_write_json

        def interrupt_commit(path: Path, payload: dict[str, object]):
            if path.name == "transaction.json" and payload.get("phase") == "committed":
                raise OSError("simulated interruption before commit marker")
            return real_atomic_write(path, payload)

        with patch.object(
            exporter_module, "atomic_write_json", side_effect=interrupt_commit
        ), patch.object(
            exporter_module,
            "_rollback_prepared",
            return_value=["simulated process termination"],
        ):
            with self.assertRaises(ExportRecoveryError):
                export_manga(self.root, self.manga_ref)

        recovery = recover_interrupted_exports(self.root)

        self.assertEqual(recovery.rolled_back_count, 1)
        self.assertEqual(self.tree_snapshot(self.workspace.output), prior)
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_recovery_discards_partially_cleaned_new_staging_after_rollback(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        prior = self.tree_snapshot(self.workspace.output)
        self.deselect(("Chapter One", "1.jpg"))
        self.select(("Chapter One", "2.PNG"), ("Chapter One", "10.jpg"))

        def interrupt_cleanup(transaction: Path, _transaction_root: Path) -> None:
            staged_file = next(
                entry
                for entry in (transaction / "new-output").rglob("*")
                if entry.is_file()
            )
            staged_file.unlink()
            raise OSError("simulated partial staging cleanup")

        with patch.object(
            exporter_module,
            "_revalidate_sources",
            side_effect=ExportConflict("simulated pre-commit failure"),
        ), patch.object(
            exporter_module,
            "_cleanup_transaction",
            side_effect=interrupt_cleanup,
        ):
            with self.assertRaises(ExportRecoveryError):
                export_manga(self.root, self.manga_ref)

        self.assertEqual(self.tree_snapshot(self.workspace.output), prior)
        self.assertTrue(any(self.workspace.transactions.iterdir()))

        recovery = recover_interrupted_exports(self.root)

        self.assertEqual(recovery.rolled_back_count, 1)
        self.assertEqual(self.tree_snapshot(self.workspace.output), prior)
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_committed_cleanup_is_finished_by_next_recovery(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        self.deselect(("Chapter One", "1.jpg"))
        self.select(("Chapter One", "2.PNG"))

        with patch.object(
            exporter_module,
            "_cleanup_transaction",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            result = export_manga(self.root, self.manga_ref)

        self.assertEqual(len(result.warnings), 1)
        committed = self.tree_snapshot(self.workspace.output)
        self.assertTrue(any(self.workspace.transactions.iterdir()))
        recovery = recover_interrupted_exports(self.root)
        self.assertEqual(recovery.committed_count, 1)
        self.assertEqual(self.tree_snapshot(self.workspace.output), committed)
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_markerless_new_output_is_discarded_but_old_output_is_not(self) -> None:
        self.workspace.workspace.mkdir(parents=True)
        self.workspace.transactions.mkdir()
        disposable = self.workspace.transactions / "export-disposable"
        (disposable / "new-output").mkdir(parents=True)
        (disposable / "new-output" / "file.jpg").write_bytes(b"new")

        recovery = recover_interrupted_exports(self.root)

        self.assertEqual(recovery.discarded_count, 1)
        self.assertFalse(disposable.exists())

        unsafe = self.workspace.transactions / "export-unsafe"
        (unsafe / "old-output").mkdir(parents=True)
        (unsafe / "old-output" / "file.jpg").write_bytes(b"old")
        with self.assertRaisesRegex(ExportRecoveryError, "active data"):
            recover_interrupted_exports(self.root)
        self.assertTrue((unsafe / "old-output" / "file.jpg").is_file())

    def test_malformed_export_journal_fails_closed(self) -> None:
        self.workspace.workspace.mkdir(parents=True)
        self.workspace.transactions.mkdir()
        transaction = self.workspace.transactions / "export-malformed"
        (transaction / "new-output").mkdir(parents=True)
        (transaction / "transaction.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ExportRecoveryError, "invalid format"):
            recover_interrupted_exports(self.root)

        self.assertTrue(transaction.is_dir())

    def test_transaction_free_bad_output_in_other_workspace_does_not_block_recovery(self) -> None:
        metadata = self.root / ".pocket-manga-editor"
        other = metadata / "Other"
        other.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (other / "output").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        recovery = recover_interrupted_exports(self.root)

        self.assertEqual(recovery.recovered_count, 0)
        self.assertTrue((other / "output").is_symlink())

    @unittest.skipIf(os.name == "nt", "Backslash is a path separator on Windows.")
    def test_posix_backslashes_round_trip_as_exact_names(self) -> None:
        self.add_image("Slash\\Series", "Part\\One", "page\\1.jpg", b"slash")
        manga = self.manga("Slash\\Series")
        store = EditingStore(self.root)
        store.set_selection(manga, "Part\\One", "page\\1.jpg", True)

        result = export_manga(self.root, manga)

        expected = (
            result.output_directory
            / "Part\\One"
            / exported_image_name("Part\\One", "page\\1.jpg")
        )
        self.assertEqual(expected.read_bytes(), b"slash")

    def test_casefold_folder_collision_is_rejected_before_output_mutation(self) -> None:
        self.add_image("Collision", "Entry", "a.jpg")
        self.add_image("Collision", "entry", "b.jpg")
        manga = self.manga("Collision")
        if len(manga.folders) != 2:
            self.skipTest("The test filesystem is case-insensitive.")
        store = EditingStore(self.root)
        for folder in manga.folders:
            store.set_selection(manga, folder.name, folder.images[0].name, True)

        with self.assertRaisesRegex(ExportConflict, "case-insensitive"):
            export_manga(self.root, manga)

        self.assertFalse(manga_workspace_paths(self.root, "Collision").output.exists())

    def test_casefold_image_collision_is_rejected_before_output_mutation(self) -> None:
        self.add_image("Collision", "Entry", "page.jpg")
        self.add_image("Collision", "Entry", "PAGE.JPG")
        manga = self.manga("Collision")
        if len(manga.folders[0].images) != 2:
            self.skipTest("The test filesystem is case-insensitive.")
        store = EditingStore(self.root)
        for image in manga.folders[0].images:
            store.set_selection(manga, "Entry", image.name, True)

        with self.assertRaisesRegex(ExportConflict, "collide"):
            export_manga(self.root, manga)

        self.assertFalse(manga_workspace_paths(self.root, "Collision").output.exists())

    def test_output_component_length_is_preflighted(self) -> None:
        self.select(("Chapter One", "1.jpg"))

        with patch.object(exporter_module.os, "pathconf", return_value=8, create=True):
            with self.assertRaisesRegex(ExportError, "too long"):
                export_manga(self.root, self.manga_ref)

        self.assertFalse(self.workspace.output.exists())
        self.assertFalse(self.workspace.transactions.exists())

    def test_export_uses_safe_defaults_when_pathconf_is_unavailable(self) -> None:
        self.select(("Chapter One", "1.jpg"))

        with patch.object(exporter_module.os, "pathconf", None, create=True):
            result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.image_count, 1)
        self.assertEqual(self.target("Chapter One", "1.jpg").read_bytes(), b"one")

    def test_export_and_inspection_honor_nonblocking_library_lock(self) -> None:
        self.select(("Chapter One", "1.jpg"))

        with library_mutation_lock(self.root):
            with self.assertRaises(ExportBusyError):
                inspect_export(self.root, self.manga_ref)
            with self.assertRaises(ExportBusyError):
                export_manga(self.root, self.manga_ref)

    def test_read_only_prior_output_is_replaced_and_cleaned(self) -> None:
        self.select(("Chapter One", "1.jpg"))
        export_manga(self.root, self.manga_ref)
        old_file = self.target("Chapter One", "1.jpg")
        old_file.chmod(stat.S_IRUSR)
        self.select(("Chapter One", "2.PNG"))

        result = export_manga(self.root, self.manga_ref)

        self.assertEqual(result.image_count, 2)
        self.assertEqual(self.target("Chapter One", "1.jpg").read_bytes(), b"one")
        self.assertEqual(self.target("Chapter One", "2.PNG").read_bytes(), b"two")
        self.assertEqual(tuple(self.workspace.transactions.iterdir()), ())

    def test_output_path_lookup_ignores_unrelated_bad_metadata_leafs(self) -> None:
        self.workspace.workspace.mkdir(parents=True)
        self.workspace.reading.mkdir()

        output = manga_output_directory(self.root, self.manga_ref)

        self.assertEqual(output, self.workspace.output)


if __name__ == "__main__":
    unittest.main()
