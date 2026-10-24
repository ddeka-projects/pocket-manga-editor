from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pocket_manga_editor.config import ConfigurationError, load_settings
from pocket_manga_editor.library_lock import (
    LibraryBusyError,
    LibraryLockError,
    library_mutation_lock,
)
from pocket_manga_editor.models import FolderRef, ImageRef, MangaRef
from pocket_manga_editor.path_safety import is_link_or_reparse
from pocket_manga_editor.scanner import ScanError, scan_working_directory
from pocket_manga_editor.storage import (
    EditingStateError,
    EditingStore,
    ReadingStateError,
    ReadingStore,
)
from pocket_manga_editor.workspace import WorkspaceError, manga_metadata_paths


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

        configuration = load_settings(env_file, environ={})

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

        configuration = load_settings(
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
            load_settings(
                self.root / "missing.env",
                environ={"POCKET_MANGA_EDITOR_WORKING_DIRECTORY": "relative"},
            )
        with self.assertRaises(ConfigurationError):
            load_settings(
                self.root / "missing.env",
                environ={
                    "POCKET_MANGA_EDITOR_WORKING_DIRECTORY": str(self.root / "does-not-exist")
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
            load_settings(
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
                    load_settings(self.root / "missing.env", environ={**base, name: value})

        malformed = self.root / ".env"
        malformed.write_text("this is not an assignment\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "line 1"):
            load_settings(malformed, environ=base)


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
        (self.root / "Series" / "Valid" / "notes.txt").write_text("ignored", encoding="utf-8")
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
            (self.root / "Linked Manga").symlink_to(self.root / "Safe", target_is_directory=True)
            (self.root / "Safe" / "Linked Folder").symlink_to(outside, target_is_directory=True)
            (self.root / "Safe" / "Folder" / "linked.jpg").symlink_to(outside / "escape.jpg")
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

        with (
            patch("pocket_manga_editor.path_safety.os.name", "nt"),
            patch.object(Path, "lstat", return_value=information),
        ):
            self.assertTrue(is_link_or_reparse(candidate))


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

    def test_reading_remembers_only_the_latest_pair(self) -> None:
        self.reading.set_position(self.manga_ref, "Folder 1", "2.PNG")
        self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")

        restored = self.reading.load(self.manga_ref)
        payload = json.loads(self.reading.path_for(self.manga_ref).read_text())

        self.assertEqual((restored.last_folder, restored.last_image), ("Folder 2", "10.jpg"))
        self.assertEqual(
            payload,
            {
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
                    "last_folder": "Renamed Folder",
                    "last_image": "missing.jpg",
                    "folders": {
                        "Folder 1": {"selected_images": ["1.jpg", "renamed.png", "gone.png"]},
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

    def test_malformed_metadata_fails_closed_without_overwriting_bytes(self) -> None:
        reading_path = self.reading.path_for(self.manga_ref)
        reading_path.parent.mkdir(parents=True)
        reading_path.write_bytes(b"\xff")
        editing_path = self.editing.path_for(self.manga_ref)
        editing_path.write_bytes(b"\xff")

        with self.assertRaises(ReadingStateError):
            self.reading.load(self.manga_ref)
        with self.assertRaises(ReadingStateError):
            self.reading.set_position(self.manga_ref, "Folder 1", "1.jpg")
        with self.assertRaises(EditingStateError):
            self.editing.load(self.manga_ref)
        with self.assertRaises(EditingStateError):
            self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        self.assertEqual(reading_path.read_bytes(), b"\xff")
        self.assertEqual(editing_path.read_bytes(), b"\xff")

    def test_failed_metadata_replace_preserves_confirmed_position_and_selections(self) -> None:
        self.reading.set_position(self.manga_ref, "Folder 1", "1.jpg")
        self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        reading_path = self.reading.path_for(self.manga_ref)
        editing_path = self.editing.path_for(self.manga_ref)
        before_reading = reading_path.read_bytes()
        before_editing = editing_path.read_bytes()
        with patch(
            "pocket_manga_editor.storage.os.replace",
            side_effect=PermissionError("disk refused replacement"),
        ):
            with self.assertRaises(PermissionError):
                self.reading.set_position(self.manga_ref, "Folder 2", "10.jpg")
            with self.assertRaises(PermissionError):
                self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", False)
        self.assertEqual(reading_path.read_bytes(), before_reading)
        self.assertEqual(editing_path.read_bytes(), before_editing)
        self.assertEqual(list(reading_path.parent.glob("*.tmp")), [])

    def test_unexpected_keys_fail_closed(self) -> None:
        reading_path = self.reading.path_for(self.manga_ref)
        reading_path.parent.mkdir(parents=True)
        reading_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
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
                    "last_folder": "Folder 1",
                    "last_image": "1.jpg",
                    "folders": {},
                    "exports": {},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ReadingStateError):
            self.reading.load(self.manga_ref)
        with self.assertRaises(EditingStateError):
            self.editing.load(self.manga_ref)

    def test_one_manga_state_is_isolated_from_another(self) -> None:
        self.add_image("Other", "Folder", "1.jpg")
        other = self.manga("Other")
        self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)
        self.editing.set_selection(other, "Folder", "1.jpg", True)

        first_path = self.editing.path_for(self.manga_ref)
        other_path = self.editing.path_for(other)

        metadata = self.root.resolve() / ".pocket-manga-editor"
        self.assertEqual(first_path, metadata / "Series.edit.json")
        self.assertEqual(other_path, metadata / "Other.edit.json")
        self.assertEqual(
            sorted(path.name for path in metadata.iterdir() if not path.name.startswith(".")),
            ["Other.edit.json", "Series.edit.json"],
        )

    def test_store_mutations_honor_nonblocking_library_lock(self) -> None:
        with library_mutation_lock(self.root):
            with self.assertRaises(LibraryBusyError):
                self.editing.set_selection(self.manga_ref, "Folder 1", "1.jpg", True)

    def test_store_refuses_symlinked_metadata(self) -> None:
        metadata = self.root / ".pocket-manga-editor"
        outside = self.root / "outside-metadata"
        outside.mkdir()
        try:
            metadata.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        with self.assertRaises(WorkspaceError):
            self.editing.load(self.manga_ref)

    def test_store_refuses_symlinked_metadata_file(self) -> None:
        metadata = self.root / ".pocket-manga-editor"
        metadata.mkdir()
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            (metadata / "Series.edit.json").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"File links are unavailable: {exc}")

        with self.assertRaises(WorkspaceError):
            self.editing.load(self.manga_ref)

    def test_bad_metadata_leafs_are_isolated_by_activity(self) -> None:
        workspace = manga_metadata_paths(self.root, "Series")
        workspace.metadata.mkdir()
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
