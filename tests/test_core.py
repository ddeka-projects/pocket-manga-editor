from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pocket_manga_editor.exporter import ExportConflict, export_selected_pages
from pocket_manga_editor.scanner import scan_working_directory
from pocket_manga_editor.storage import SessionStore


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_page(
        self,
        manga: str,
        chapter_folder: str,
        page_name: str,
        content: bytes | None = None,
    ) -> Path:
        chapter = self.root / manga / chapter_folder
        chapter.mkdir(parents=True, exist_ok=True)
        page = chapter / page_name
        page.write_bytes(content if content is not None else page_name.encode("utf-8"))
        return page


class ScannerTests(RepositoryFixture):
    def test_scans_a_numeric_virtual_volume_in_order(self) -> None:
        self.add_page("Series", "Vol. 02 Ch. 010 - Later", "010.jpg")
        self.add_page("Series", "Vol. 01 Ch. 002 - Second", "002.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "010.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "002.JPG")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "thumbnail.png")

        result = scan_working_directory(self.root)

        self.assertEqual([manga.name for manga in result.mangas], ["Series"])
        manga = result.mangas[0]
        self.assertEqual([volume.number for volume in manga.volumes], [1, 2])
        self.assertEqual(
            [page.relative_path for page in manga.volumes[0].pages],
            [
                "Vol. 01 Ch. 001 - First/002.JPG",
                "Vol. 01 Ch. 001 - First/010.jpg",
                "Vol. 01 Ch. 002 - Second/002.jpg",
            ],
        )
        self.assertEqual(
            [page.output_filename for page in manga.volumes[0].pages],
            ["C001_P002.jpg", "C001_P010.jpg", "C002_P002.jpg"],
        )

    def test_reports_malformed_and_ambiguous_chapters_without_crashing(self) -> None:
        self.add_page("Series", "Vol.01 Ch. 001 - Missing space", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - Duplicate A", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - Duplicate B", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 002 - Valid", "001.jpg")

        result = scan_working_directory(self.root)

        self.assertEqual(len(result.mangas), 1)
        pages = result.mangas[0].volumes[0].pages
        self.assertEqual([page.chapter_number for page in pages], [2])
        messages = " ".join(issue.message for issue in result.issues)
        self.assertIn("does not match", messages)
        self.assertIn("Duplicate Vol. 01 Ch. 001", messages)

    def test_reports_a_misnamed_jpg_even_when_the_chapter_has_valid_pages(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "02.jpg")

        result = scan_working_directory(self.root)

        self.assertEqual(len(result.mangas[0].volumes[0].pages), 1)
        self.assertIn("does not match", result.issues[0].message)

    def test_sorts_decimal_chapter_identifiers_numerically(self) -> None:
        chapter_labels = (
            "068",
            "067.5",
            "001",
            "000.2",
            "067",
            "000.03",
            "000.1",
            "000.02",
            "000.01",
        )
        for label in chapter_labels:
            self.add_page(
                "Series",
                f"Vol. 1 Ch. {label} - Chapter {label}",
                "001.jpg",
            )

        volume = scan_working_directory(self.root).mangas[0].volumes[0]

        self.assertEqual(
            [page.chapter_label for page in volume.pages],
            [
                "000.01",
                "000.02",
                "000.03",
                "000.1",
                "000.2",
                "001",
                "067",
                "067.5",
                "068",
            ],
        )
        self.assertEqual(
            [page.output_filename for page in volume.pages],
            [
                "C000.01_P001.jpg",
                "C000.02_P001.jpg",
                "C000.03_P001.jpg",
                "C000.1_P001.jpg",
                "C000.2_P001.jpg",
                "C001_P001.jpg",
                "C067_P001.jpg",
                "C067.5_P001.jpg",
                "C068_P001.jpg",
            ],
        )

    def test_equivalent_decimal_chapter_identifiers_are_ambiguous(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 000.1 - First", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 000.10 - Same number", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - Valid", "001.jpg")

        result = scan_working_directory(self.root)

        pages = result.mangas[0].volumes[0].pages
        self.assertEqual([page.chapter_label for page in pages], ["001"])
        self.assertIn("Duplicate Vol. 01 Ch. 000.1", result.issues[0].message)


class SessionStoreTests(RepositoryFixture):
    def test_round_trips_selection_and_current_page(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "002.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        store = SessionStore(self.root)

        selected = {volume.pages[1].relative_path}
        store.save(volume, 1, selected)
        restored = store.load(volume)

        self.assertEqual(restored.current_index, 1)
        self.assertEqual(restored.selected_paths, frozenset(selected))
        payload = json.loads(store.path_for(volume).read_text(encoding="utf-8"))
        self.assertEqual(payload["current_page"], volume.pages[1].relative_path)
        self.assertEqual(payload["selected_pages"], [volume.pages[1].relative_path])

    def test_ignores_unsafe_and_stale_saved_paths(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        store = SessionStore(self.root)
        path = store.path_for(volume)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "manga": "Series",
                    "volume": 1,
                    "current_index": 99,
                    "selected_pages": ["../outside.jpg", "missing/001.jpg"],
                }
            ),
            encoding="utf-8",
        )

        restored = store.load(volume)

        self.assertEqual(restored.current_index, 0)
        self.assertEqual(restored.selected_paths, frozenset())
        self.assertEqual(len(restored.warnings), 2)

    def test_invalid_utf8_state_is_ignored_with_a_warning(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        store = SessionStore(self.root)
        path = store.path_for(volume)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff")

        restored = store.load(volume)

        self.assertEqual(restored.selected_paths, frozenset())
        self.assertEqual(len(restored.warnings), 1)


class ExporterTests(RepositoryFixture):
    def test_repeat_export_updates_only_managed_files(self) -> None:
        self.add_page(
            "Series", "Vol. 01 Ch. 001 - First", "001.jpg", content=b"chapter-one"
        )
        self.add_page(
            "Series", "Vol. 01 Ch. 002 - Second", "001.jpg", content=b"chapter-two"
        )
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        first, second = volume.pages

        initial = export_selected_pages(
            self.root, volume, {first.relative_path, second.relative_path}
        )
        unknown_file = initial.output_directory / "notes.txt"
        unknown_file.write_text("leave me alone", encoding="utf-8")

        repeated = export_selected_pages(self.root, volume, {second.relative_path})

        self.assertEqual(repeated.copied_count, 1)
        self.assertEqual(repeated.removed_count, 1)
        self.assertFalse((repeated.output_directory / "C001_P001.jpg").exists())
        self.assertEqual(
            (repeated.output_directory / "C002_P001.jpg").read_bytes(), b"chapter-two"
        )
        self.assertEqual(unknown_file.read_text(encoding="utf-8"), "leave me alone")

    def test_refuses_to_overwrite_an_untracked_collision(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        page = volume.pages[0]
        output = volume.manga_path / "Output" / "Vol.01"
        output.mkdir(parents=True)
        (output / page.output_filename).write_bytes(b"user-owned")

        with self.assertRaises(ExportConflict):
            export_selected_pages(self.root, volume, {page.relative_path})

        self.assertEqual((output / page.output_filename).read_bytes(), b"user-owned")

    def test_adopts_identical_output_left_by_an_interrupted_first_export(self) -> None:
        source = self.add_page(
            "Series", "Vol. 01 Ch. 001 - First", "001.jpg", b"same-content"
        )
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        page = volume.pages[0]
        output = volume.manga_path / "Output" / "Vol.01"
        output.mkdir(parents=True)
        existing = output / page.output_filename
        existing.write_bytes(source.read_bytes())

        result = export_selected_pages(self.root, volume, {page.relative_path})

        self.assertEqual(result.copied_count, 1)
        self.assertEqual(existing.read_bytes(), b"same-content")

    def test_refuses_to_overwrite_or_remove_an_edited_managed_file(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg", b"source-one")
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "002.jpg", b"source-two")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        first, second = volume.pages
        result = export_selected_pages(
            self.root, volume, {first.relative_path, second.relative_path}
        )
        edited_target = result.output_directory / first.output_filename
        edited_target.write_bytes(b"user edit")

        with self.assertRaises(ExportConflict):
            export_selected_pages(self.root, volume, {second.relative_path})

        self.assertEqual(edited_target.read_bytes(), b"user edit")
        self.assertTrue((result.output_directory / second.output_filename).exists())

    def test_invalid_utf8_manifest_aborts_without_touching_output(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        page = volume.pages[0]
        manifest = (
            self.root
            / ".pocket-manga-editor"
            / "exports"
            / "Series"
            / "Vol.01.json"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(b"\xff")

        with self.assertRaisesRegex(Exception, "manifest"):
            export_selected_pages(self.root, volume, {page.relative_path})

        self.assertFalse((volume.manga_path / "Output" / "Vol.01").exists())

    def test_rejects_a_volume_from_outside_the_working_directory(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg")
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        other_root = self.root / "another-root"
        other_root.mkdir()

        with self.assertRaisesRegex(Exception, "directly inside"):
            export_selected_pages(other_root, volume, {volume.pages[0].relative_path})

    def test_manifest_failure_rolls_the_output_back_for_a_clean_retry(self) -> None:
        self.add_page("Series", "Vol. 01 Ch. 001 - First", "001.jpg", b"old-one")
        second_source = self.add_page(
            "Series", "Vol. 01 Ch. 001 - First", "002.jpg", b"old-two"
        )
        volume = scan_working_directory(self.root).mangas[0].volumes[0]
        first, second = volume.pages
        initial = export_selected_pages(
            self.root, volume, {first.relative_path, second.relative_path}
        )
        manifest = (
            self.root
            / ".pocket-manga-editor"
            / "exports"
            / "Series"
            / "Vol.01.json"
        )
        original_manifest = manifest.read_bytes()
        second_source.write_bytes(b"new-two")

        with patch(
            "pocket_manga_editor.exporter.atomic_write_json",
            side_effect=OSError("simulated manifest failure"),
        ):
            with self.assertRaisesRegex(Exception, "save the export manifest"):
                export_selected_pages(self.root, volume, {second.relative_path})

        self.assertEqual(
            (initial.output_directory / first.output_filename).read_bytes(), b"old-one"
        )
        self.assertEqual(
            (initial.output_directory / second.output_filename).read_bytes(), b"old-two"
        )
        self.assertEqual(manifest.read_bytes(), original_manifest)

        retried = export_selected_pages(self.root, volume, {second.relative_path})
        self.assertEqual(retried.removed_count, 1)
        self.assertFalse((initial.output_directory / first.output_filename).exists())
        self.assertEqual(
            (initial.output_directory / second.output_filename).read_bytes(), b"new-two"
        )


if __name__ == "__main__":
    unittest.main()
