"""The installed PC splitter, including real PNG/JPEG pixels and safe reruns."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from pocket_manga_editor.companion_install import COMPANION_SOURCE, install_companion


_SPEC = importlib.util.spec_from_file_location("_splitter", COMPANION_SOURCE / "_splitter.py")
splitter = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = splitter
_SPEC.loader.exec_module(splitter)


class SplitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.metadata = self.root / ".pocket-manga-editor"
        self.metadata.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_image(
        self,
        name="page.png",
        *,
        manga="!Series",
        entry="Chapter 1",
        size=(5, 3),
        mode="RGB",
        **save_options,
    ) -> Path:
        path = self.root / manga / entry / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new(mode, size) as image:
            for y in range(size[1]):
                for x in range(size[0]):
                    value = x * 29 + y * 11
                    pixel = (value, 255 - value, y * 35)
                    if mode == "RGBA":
                        pixel = (*pixel, value)
                    elif mode in {"1", "L", "P", "I;16"}:
                        pixel = value if mode != "I;16" else value * 257
                    elif mode == "CMYK":
                        pixel = (*pixel, 15)
                    image.putpixel((x, y), pixel)
            image.save(path, **save_options)
        return path

    @staticmethod
    def snapshot(path: Path) -> dict[str, bytes]:
        return {
            str(item.relative_to(path)): item.read_bytes()
            for item in path.rglob("*")
            if item.is_file()
        }

    def assert_halves(self, source: Path, result: Path) -> None:
        with Image.open(source) as original:
            width, height = original.size
            cut = width // 2
            for number, offset, expected_width in ((1, cut, width - cut), (2, 0, cut)):
                output = result / source.parent.name / f"{source.stem}_{number}.png"
                with Image.open(output) as half:
                    self.assertEqual(half.format, "PNG")
                    self.assertEqual(half.size, (expected_width, height))
                    for y in range(height):
                        for x in range(expected_width):
                            self.assertEqual(
                                half.getpixel((x, y)), original.getpixel((x + offset, y))
                            )

    def test_lists_only_marked_plain_source_folders_in_natural_order(self) -> None:
        for name in ("!Series 10", "!series 2", "Normal", "Mark!Later"):
            (self.root / name).mkdir()
        (self.root / "!file").write_text("not a manga")

        self.assertEqual(splitter.find_mangas(self.metadata), ["!series 2", "!Series 10"])

    def test_all_shapes_and_odd_widths_split_without_losing_or_changing_pixels(self) -> None:
        sources = [
            self.add_image("landscape.png", size=(6, 2)),
            self.add_image("odd.png", size=(5, 3)),
            self.add_image("portrait.png", size=(2, 6)),
            self.add_image("square.png", size=(4, 4)),
        ]
        original = self.snapshot(self.root / "!Series")
        (self.metadata / "!Series.edit.json").write_bytes(b"saved selections")
        (self.metadata / "!Series.read.json").write_bytes(b"saved reading")
        progress = []

        result = splitter.split_manga(self.metadata, "!Series", lambda *item: progress.append(item))

        self.assertEqual(result.path, self.metadata / "!Series.split")
        self.assertEqual(result.image_count, 4)
        self.assertEqual(progress, [(1, 4), (2, 4), (3, 4), (4, 4)])
        for source in sources:
            self.assert_halves(source, result.path)
        self.assertEqual(self.snapshot(self.root / "!Series"), original)
        self.assertEqual((self.metadata / "!Series.edit.json").read_bytes(), b"saved selections")
        self.assertEqual((self.metadata / "!Series.read.json").read_bytes(), b"saved reading")

    def test_preserves_jpeg_decoded_pixels_alpha_grayscale_palette_and_16_bit_png(self) -> None:
        sources = [self.add_image("photo.JPG", quality=91)]
        for mode in ("RGBA", "L", "P", "I;16"):
            sources.append(self.add_image(f"{mode}.png", mode=mode))

        result = splitter.split_manga(self.metadata, "!Series")

        for source in sources:
            with self.subTest(source=source.name):
                self.assert_halves(source, result.path)

    def test_preserves_png_color_profile_and_palette_transparency(self) -> None:
        source = self.add_image(
            "palette.png", mode="P", transparency=0, icc_profile=b"test profile"
        )

        result = splitter.split_manga(self.metadata, "!Series")

        for number in (1, 2):
            with Image.open(result.path / "Chapter 1" / f"palette_{number}.png") as output:
                self.assertEqual(output.info["icc_profile"], b"test profile")
                self.assertEqual(output.info["transparency"], 0)
        self.assert_halves(source, result.path)

    def test_applies_orientation_before_splitting(self) -> None:
        source = self.root / "!Series" / "Chapter 1" / "rotated.png"
        source.parent.mkdir(parents=True)
        with Image.new("RGB", (4, 2), "red") as image:
            for x in range(4):
                image.putpixel((x, 1), (0, 0, 255))
            exif = Image.Exif()
            exif[274] = 6  # Rotate 90 degrees clockwise when displayed.
            image.save(source, exif=exif)

        result = splitter.split_manga(self.metadata, "!Series")

        for number, color in ((1, (255, 0, 0)), (2, (0, 0, 255))):
            with Image.open(result.path / "Chapter 1" / f"rotated_{number}.png") as half:
                self.assertEqual(half.size, (1, 4))
                self.assertEqual(half.getpixel((0, 0)), color)
                self.assertIsNone(half.getexif().get(274))

    def test_preserves_png_gamma_chromaticity_srgb_and_density(self) -> None:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add(b"gAMA", struct.pack(">I", 45455))
        pnginfo.add(b"sRGB", b"\x00")
        chromaticity = (31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)
        pnginfo.add(b"cHRM", struct.pack(">8I", *chromaticity))
        source = self.add_image(pnginfo=pnginfo, dpi=(300, 300))

        result = splitter.split_manga(self.metadata, "!Series")

        with Image.open(source) as original:
            for number in (1, 2):
                with Image.open(result.path / "Chapter 1" / f"page_{number}.png") as half:
                    for key in ("gamma", "srgb", "chromaticity", "dpi"):
                        self.assertEqual(half.info[key], original.info[key])

    def test_animated_png_is_reported_instead_of_silently_losing_frames(self) -> None:
        source = self.add_image()
        with Image.new("RGB", (5, 3), "red") as first, Image.new("RGB", (5, 3), "blue") as second:
            first.save(source, save_all=True, append_images=[second])

        with self.assertRaisesRegex(ValueError, "Chapter 1/page.png.*Animated"):
            splitter.split_manga(self.metadata, "!Series")
        self.assertFalse((self.metadata / "!Series.split").exists())

    def test_keeps_entry_structure_and_only_processes_direct_jpg_png_images(self) -> None:
        self.add_image("001.part.png", entry="Volume 2")
        self.add_image("002.JPG", entry="Volume 10")
        source = self.root / "!Series"
        (source / "Empty").mkdir()
        (source / "cover.jpg").write_bytes(b"not inside an entry")
        (source / "Volume 2" / "notes.txt").write_text("ignored")
        (source / "Volume 2" / "page.jpeg").write_bytes(b"unsupported")
        (source / "Volume 2" / "Nested").mkdir()
        (source / "Volume 2" / "Nested" / "page.png").write_bytes(b"nested")

        result = splitter.split_manga(self.metadata, "!Series")

        paths = {item.relative_to(result.path).as_posix() for item in result.path.rglob("*")}
        self.assertEqual(
            paths,
            {
                "Empty",
                "Volume 2",
                "Volume 10",
                "Volume 2/001.part_1.png",
                "Volume 2/001.part_2.png",
                "Volume 10/002_1.png",
                "Volume 10/002_2.png",
            },
        )

    def test_rerun_rebuilds_output_and_removes_interrupted_leftovers(self) -> None:
        first = self.add_image("first.png")
        self.add_image("second.png")
        result = splitter.split_manga(self.metadata, "!Series")
        (result.path / "stale.txt").write_text("disposable")
        for suffix in ("tmp", "old"):
            leftover = self.metadata / f"!Series.split-{suffix}"
            leftover.mkdir()
            (leftover / "partial.png").write_bytes(b"disposable")
        first.unlink()

        result = splitter.split_manga(self.metadata, "!Series")

        self.assertEqual(result.image_count, 1)
        self.assertEqual(
            set(self.snapshot(result.path)),
            {
                str(Path("Chapter 1") / "second_1.png"),
                str(Path("Chapter 1") / "second_2.png"),
            },
        )
        self.assertFalse((self.metadata / "!Series.split-tmp").exists())
        self.assertFalse((self.metadata / "!Series.split-old").exists())

    def test_bad_image_keeps_last_output_and_next_run_recovers(self) -> None:
        source = self.add_image()
        result = splitter.split_manga(self.metadata, "!Series")
        before = self.snapshot(result.path)
        broken = source.with_name("broken.png")
        broken.write_bytes(b"not an image")

        with self.assertRaisesRegex(ValueError, "Chapter 1/broken.png"):
            splitter.split_manga(self.metadata, "!Series")
        self.assertEqual(self.snapshot(result.path), before)
        self.assertEqual(broken.read_bytes(), b"not an image")

        broken.unlink()
        self.assertEqual(splitter.split_manga(self.metadata, "!Series").image_count, 1)
        self.assertFalse((self.metadata / "!Series.split-tmp").exists())

    def test_rejects_duplicate_output_stems_before_changing_existing_output(self) -> None:
        self.add_image("same.png")
        result = splitter.split_manga(self.metadata, "!Series")
        before = self.snapshot(result.path)
        self.add_image("SAME.JPG")

        with self.assertRaisesRegex(ValueError, "Duplicate image stem"):
            splitter.split_manga(self.metadata, "!Series")
        self.assertEqual(self.snapshot(result.path), before)
        self.assertFalse((self.metadata / "!Series.split-tmp").exists())

    def test_rejects_empty_manga_narrow_images_and_unsupported_png_color_modes(self) -> None:
        (self.root / "!Series").mkdir()
        with self.assertRaisesRegex(ValueError, "no JPG or PNG"):
            splitter.split_manga(self.metadata, "!Series")
        narrow = self.add_image(size=(1, 5))
        with self.assertRaisesRegex(ValueError, "at least two pixels wide"):
            splitter.split_manga(self.metadata, "!Series")
        narrow.unlink()
        self.add_image("cmyk.jpg", mode="CMYK")
        with self.assertRaisesRegex(ValueError, "cannot write mode CMYK as PNG"):
            splitter.split_manga(self.metadata, "!Series")
        self.assertFalse((self.metadata / "!Series.split").exists())

    def test_rejects_unsafe_names_and_linked_inputs_or_output(self) -> None:
        source = self.add_image()
        for name in ("Series", "!../Series", "!Series\\other", "!Series:other"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                splitter.split_manga(self.metadata, name)
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"keep")
        try:
            (self.root / "!Linked").symlink_to(source.parent.parent, target_is_directory=True)
            (source.parent / "linked.png").symlink_to(source)
            (source.parent.parent / "linked-entry").symlink_to(outside, target_is_directory=True)
            output = self.metadata / "!Series.split-tmp"
            output.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Links are unavailable: {exc}")

        self.assertEqual(splitter.find_mangas(self.metadata), ["!Series"])
        with self.assertRaisesRegex(ValueError, "non-link folder"):
            splitter.split_manga(self.metadata, "!Linked")
        with self.assertRaisesRegex(ValueError, "non-link folder"):
            splitter.split_manga(self.metadata, "!Series")
        self.assertEqual(sentinel.read_bytes(), b"keep")
        output.unlink()

        result = splitter.split_manga(self.metadata, "!Series")
        self.assertEqual(result.image_count, 1)
        self.assertFalse((result.path / "linked-entry").exists())

    def test_installed_script_runs_from_an_unrelated_working_directory(self) -> None:
        self.add_image("001.png", manga="!日本語 & (Series)")
        install_companion(self.root)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()

        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(self.metadata / "_splitter.py")],
            cwd=unrelated,
            input="invalid\n9\n1\n",
            text=True,
            capture_output=True,
            timeout=20,
            encoding="utf-8",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Please enter a number from 1 to 1", result.stdout)
        self.assertIn("Split 1 image into 2 PNG pages", result.stdout)
        self.assertTrue(
            (self.metadata / "!日本語 & (Series).split" / "Chapter 1" / "001_1.png").is_file()
        )

    def test_console_handles_no_marked_manga_cancellation_and_missing_dependency(self) -> None:
        with patch.object(splitter, "__file__", str(self.metadata / "_splitter.py")):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(splitter.main(), 0)
            self.assertIn("No manga folder names start", output.getvalue())
            self.add_image()
            with (
                patch("builtins.input", return_value=""),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(splitter.main(), 0)
            self.assertFalse((self.metadata / "!Series.split").exists())
            with (
                patch("builtins.input", return_value="1"),
                patch.object(splitter, "_split_image", side_effect=ModuleNotFoundError("PIL")),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(splitter.main(), 1)
            self.assertIn("bootstrap-windows.ps1", output.getvalue())
            self.assertFalse((self.metadata / "!Series.split").exists())
