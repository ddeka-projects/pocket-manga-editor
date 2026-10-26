"""The PC export companion and its installation into the metadata folder."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pocket_manga_editor.companion_install import COMPANION_SOURCE, install_companion
from pocket_manga_editor.storage import EditingStore
from pocket_manga_editor.scanner import scan_manga
from pocket_manga_editor.workspace import WorkspaceError

_SPEC = importlib.util.spec_from_file_location("_exporter", COMPANION_SOURCE / "_exporter.py")
exporter = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = exporter
_SPEC.loader.exec_module(exporter)


class CompanionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.metadata = self.root / ".pocket-manga-editor"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_image(self, manga: str, folder: str, image: str, content: bytes) -> Path:
        path = self.root / manga / folder / image
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def select(self, manga_name: str, *identities: tuple[str, str]) -> None:
        manga = scan_manga(self.root, manga_name).manga
        store = EditingStore(self.root)
        for folder, image in identities:
            store.set_selection(manga, folder, image, True)

    @staticmethod
    def tree(path: Path) -> dict[str, bytes | None]:
        return {
            entry.relative_to(path).as_posix(): None if entry.is_dir() else entry.read_bytes()
            for entry in sorted(path.rglob("*"))
        }


class InstallTests(CompanionFixture):
    def test_installs_both_files_and_overwrites_old_copies(self) -> None:
        self.metadata.mkdir()
        (self.metadata / "_exporter.py").write_text("old", encoding="utf-8")

        installed = install_companion(self.root)

        self.assertEqual(installed, self.metadata)
        self.assertEqual(
            (self.metadata / "_exporter.py").read_bytes(),
            (COMPANION_SOURCE / "_exporter.py").read_bytes(),
        )
        batch = (self.metadata / "_exporter.bat").read_bytes()
        self.assertIn(b"_exporter.py", batch)
        self.assertNotIn(b"\n", batch.replace(b"\r\n", b""))
        self.assertEqual(sorted(path.name for path in self.metadata.glob("*.tmp")), [])

    def test_creates_the_metadata_folder_when_missing(self) -> None:
        install_companion(self.root)
        self.assertTrue((self.metadata / "_exporter.bat").is_file())

    def test_refuses_to_replace_a_linked_companion_path(self) -> None:
        self.metadata.mkdir()
        outside = self.root / "outside.py"
        outside.write_text("keep", encoding="utf-8")
        try:
            (self.metadata / "_exporter.py").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"File links are unavailable: {exc}")
        with self.assertRaises(WorkspaceError):
            install_companion(self.root)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")


class ExportScriptTests(CompanionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.add_image("Series", "Chapter 1", "1.jpg", b"one")
        self.add_image("Series", "Chapter 1", "2.png", b"two")
        self.add_image("Series", "Chapter 10", "1.jpg", b"ten")
        self.add_image("Other", "Bonus", "a.png", b"bonus")

    def test_lists_only_manga_with_selections_in_natural_order(self) -> None:
        self.select("Series", ("Chapter 10", "1.jpg"), ("Chapter 1", "2.png"))
        self.metadata.joinpath("Other.edit.json").write_text(
            json.dumps({"last_folder": "Bonus", "last_image": "a.png", "folders": {}}),
            encoding="utf-8",
        )
        self.metadata.joinpath("Series.read.json").write_text("{}", encoding="utf-8")

        selections, problems = exporter.find_selections(self.metadata)

        self.assertEqual(problems, [])
        self.assertEqual([selection.name for selection in selections], ["Series"])
        self.assertEqual(selections[0].image_count, 2)

    def test_unreadable_or_unsafe_edit_files_are_reported_not_exported(self) -> None:
        self.metadata.mkdir()
        self.metadata.joinpath("Broken.edit.json").write_bytes(b"\xff")
        self.metadata.joinpath("Escape.edit.json").write_text(
            json.dumps({"folders": {"..": {"selected_images": ["1.jpg"]}}}),
            encoding="utf-8",
        )

        selections, problems = exporter.find_selections(self.metadata)

        self.assertEqual(selections, [])
        self.assertEqual(len(problems), 2)

    def test_export_copies_selected_images_with_entry_prefixed_names(self) -> None:
        self.select("Series", ("Chapter 1", "2.png"), ("Chapter 10", "1.jpg"))
        selection = exporter.find_selections(self.metadata)[0][0]
        progress: list[tuple[int, int]] = []

        result = exporter.export_manga(
            self.metadata, selection, lambda done, total: progress.append((done, total))
        )

        self.assertEqual(result.path, self.metadata / "Series.export")
        self.assertEqual(result.image_count, 2)
        self.assertEqual(
            self.tree(result.path),
            {
                "Chapter 1": None,
                "Chapter 1/Chapter 1__2.png": b"two",
                "Chapter 10": None,
                "Chapter 10/Chapter 10__1.jpg": b"ten",
            },
        )
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual((self.root / "Series" / "Chapter 1" / "2.png").read_bytes(), b"two")
        self.assertEqual((self.root / "Series" / "Chapter 10" / "1.jpg").read_bytes(), b"ten")

    def test_export_replaces_the_previous_export_and_leftovers(self) -> None:
        self.select("Series", ("Chapter 1", "1.jpg"))
        selection = exporter.find_selections(self.metadata)[0][0]
        final = self.metadata / "Series.export"
        (final / "Old Chapter").mkdir(parents=True)
        (final / "Old Chapter" / "stale.jpg").write_bytes(b"stale")
        (self.metadata / "Series.export-tmp" / "partial").mkdir(parents=True)
        (self.metadata / "Series.export-old").mkdir()

        exporter.export_manga(self.metadata, selection)

        self.assertEqual(
            self.tree(final),
            {"Chapter 1": None, "Chapter 1/Chapter 1__1.jpg": b"one"},
        )
        self.assertFalse((self.metadata / "Series.export-tmp").exists())
        self.assertFalse((self.metadata / "Series.export-old").exists())

    def test_missing_source_stops_before_touching_the_previous_export(self) -> None:
        self.select("Series", ("Chapter 1", "1.jpg"), ("Chapter 1", "2.png"))
        selection = exporter.find_selections(self.metadata)[0][0]
        final = self.metadata / "Series.export"
        final.mkdir()
        (final / "keep.jpg").write_bytes(b"keep")
        (self.root / "Series" / "Chapter 1" / "2.png").unlink()

        with self.assertRaises(exporter.MissingSourcesError) as stopped:
            exporter.export_manga(self.metadata, selection)

        self.assertEqual(stopped.exception.missing, ["Chapter 1/2.png"])
        self.assertEqual(self.tree(final), {"keep.jpg": b"keep"})
        self.assertFalse((self.metadata / "Series.export-tmp").exists())

    def test_linked_source_counts_as_missing(self) -> None:
        self.select("Series", ("Chapter 1", "1.jpg"))
        selection = exporter.find_selections(self.metadata)[0][0]
        source = self.root / "Series" / "Chapter 1" / "1.jpg"
        source.unlink()
        try:
            source.symlink_to(self.root / "Other" / "Bonus" / "a.png")
        except OSError as exc:
            self.skipTest(f"File links are unavailable: {exc}")

        with self.assertRaises(exporter.MissingSourcesError):
            exporter.export_manga(self.metadata, selection)

    def test_interactive_run_exports_the_chosen_manga(self) -> None:
        self.select("Series", ("Chapter 1", "1.jpg"))
        self.select("Other", ("Bonus", "a.png"))
        script = self.metadata / "_exporter.py"
        install_companion(self.root)

        with (
            patch.object(exporter, "__file__", str(script)),
            patch("builtins.input", side_effect=["9", "1"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(exporter.main(), 0)

        text = output.getvalue()
        self.assertIn("1. Other (1 image)", text)
        self.assertIn("2. Series (1 image)", text)
        self.assertIn("Please enter a number from 1 to 2.", text)
        self.assertIn("Exported 1 image to:", text)
        self.assertTrue((self.metadata / "Other.export" / "Bonus" / "Bonus__a.png").is_file())
        self.assertFalse((self.metadata / "Series.export").exists())

    def test_interactive_run_can_be_cancelled(self) -> None:
        self.select("Series", ("Chapter 1", "1.jpg"))
        with (
            patch.object(exporter, "__file__", str(self.metadata / "_exporter.py")),
            patch("builtins.input", return_value=""),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(exporter.main(), 0)
        self.assertIn("Cancelled", output.getvalue())
        self.assertFalse((self.metadata / "Series.export").exists())


if __name__ == "__main__":
    unittest.main()
