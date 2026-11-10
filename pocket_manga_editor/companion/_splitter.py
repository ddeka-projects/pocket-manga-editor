"""Split every image of a marked manga into right-first, lossless PNG pages.

Installed beside ``_splitter.bat`` in the library's ``.pocket-manga-editor``
folder at every server start. Only source manga names starting with ``!`` are
listed. Output is ``<manga>.split/<entry>/<image stem>_1.png`` (right half) and
``_2.png`` (left half). Source images and saved app data are never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import struct
from typing import Callable


IMAGE_SUFFIXES = frozenset({".jpg", ".png"})
_NUMBER = re.compile(r"(\d+)")


@dataclass(frozen=True)
class SplitResult:
    path: Path
    image_count: int
    cleanup_warning: str | None = None


def natural_key(value: str) -> tuple[tuple[tuple[int, object], ...], str]:
    tokens = tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in _NUMBER.split(value)
        if token
    )
    return tokens, value


def find_mangas(metadata: Path) -> list[str]:
    """List marked source folders without scanning their contents."""

    return sorted(
        (
            path.name
            for path in metadata.parent.iterdir()
            if path.name.startswith("!") and _is_plain(path, directory=True)
        ),
        key=natural_key,
    )


def split_manga(
    metadata: Path,
    manga_name: str,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> SplitResult:
    """Rebuild one manga's split output, keeping the last output until success."""

    if (
        not manga_name.startswith("!")
        or any(character in manga_name for character in "/\\:\x00")
        or Path(manga_name).name != manga_name
    ):
        raise ValueError("Choose a source manga folder whose name starts with '!'.")
    source_manga = metadata.parent / manga_name
    for directory in (metadata.parent, metadata, source_manga):
        if not _is_plain(directory, directory=True):
            raise ValueError(f"Not a regular, non-link folder: {directory}")

    folders = sorted(
        (path for path in source_manga.iterdir() if _is_plain(path, directory=True)),
        key=lambda path: natural_key(path.name),
    )
    plan: list[tuple[Path, Path]] = []
    output_names: set[str] = set()
    for folder in folders:
        images = sorted(
            (
                path
                for path in folder.iterdir()
                if path.suffix.casefold() in IMAGE_SUFFIXES and _is_plain(path, directory=False)
            ),
            key=lambda path: natural_key(path.name),
        )
        for image in images:
            relative = Path(folder.name) / image.stem
            # JPG and PNG files with the same stem would overwrite the same pages.
            key = relative.as_posix().casefold()
            if key in output_names:
                raise ValueError(
                    f"Duplicate image stem in '{folder.name}': '{image.name}'. "
                    "Each image in an entry needs a distinct stem for _1/_2 output."
                )
            output_names.add(key)
            plan.append((image, relative))
    if not plan:
        raise ValueError("This manga has no JPG or PNG images inside entry folders.")

    final = metadata / f"{manga_name}.split"
    temporary = metadata / f"{manga_name}.split-tmp"
    previous = metadata / f"{manga_name}.split-old"
    for output in (final, temporary, previous):
        if os.path.lexists(output) and not _is_plain(output, directory=True):
            raise ValueError(f"Split output is not a regular, non-link folder: {output}")
    # Output is disposable; a rerun removes leftovers from an interrupted run.
    for leftover in (temporary, previous):
        if leftover.exists():
            shutil.rmtree(leftover)
    temporary.mkdir()
    for folder in folders:
        (temporary / folder.name).mkdir()
    for done, (source, relative) in enumerate(plan, start=1):
        _split_image(source, temporary / relative)
        progress(done, len(plan))

    if final.exists():
        final.rename(previous)
    temporary.rename(final)
    try:
        if previous.exists():
            shutil.rmtree(previous)
    except OSError as exc:
        return SplitResult(
            final,
            len(plan),
            f"The previous split could not be deleted ({exc}); the next run removes it.",
        )
    return SplitResult(final, len(plan))


def _split_image(source: Path, target_stem: Path) -> None:
    # Keep discovery/cancellation usable even if setup has not installed Pillow.
    from PIL import Image, ImageOps, PngImagePlugin

    try:
        with Image.open(source) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise ValueError("Animated images cannot be split into two still pages losslessly.")
            with ImageOps.exif_transpose(opened) as image:
                width, height = image.size
                if width < 2:
                    raise ValueError("The image must be at least two pixels wide.")
                # Retain PNG display metadata as well as the pixels and ICC profile.
                pnginfo = PngImagePlugin.PngInfo()
                if "gamma" in image.info:
                    pnginfo.add(b"gAMA", struct.pack(">I", round(image.info["gamma"] * 100_000)))
                if "srgb" in image.info:
                    pnginfo.add(b"sRGB", bytes([image.info["srgb"]]))
                if "chromaticity" in image.info:
                    values = [round(value * 100_000) for value in image.info["chromaticity"]]
                    pnginfo.add(b"cHRM", struct.pack(">8I", *values))
                midpoint = width // 2
                boxes = ((midpoint, 0, width, height), (0, 0, midpoint, height))
                for number, box in enumerate(boxes, start=1):
                    with image.crop(box) as half:
                        # Orientation is already applied; don't retain stale EXIF sizes.
                        half.info.pop("exif", None)
                        half.save(
                            target_stem.with_name(f"{target_stem.name}_{number}.png"),
                            format="PNG",
                            pnginfo=pnginfo,
                            dpi=image.info.get("dpi"),
                        )
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not split '{source.parent.name}/{source.name}': {exc}") from exc


def _is_plain(path: Path, *, directory: bool) -> bool:
    try:
        information = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & reparse:
        return False
    return stat.S_ISDIR(information.st_mode) if directory else stat.S_ISREG(information.st_mode)


def _choose(count: int) -> int | None:
    while True:
        try:
            answer = input(f"Choose a number from 1 to {count} (Enter to cancel): ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer)
        print(f"Please enter a number from 1 to {count}.")


def _print_progress(done: int, total: int) -> None:
    print(f"\rSplitting {done} of {total}", end="", flush=True)


def main() -> int:
    metadata = Path(__file__).resolve().parent
    print("Pocket Manga Editor splitter")
    print(f"Library: {metadata.parent}")
    print("Every image is split at its midpoint: _1 = right, _2 = left.")
    print()
    try:
        mangas = find_mangas(metadata)
        if not mangas:
            print("No manga folder names start with '!'.")
            return 0
        for number, name in enumerate(mangas, start=1):
            print(f"  {number}. {name}")
        print()
        choice = _choose(len(mangas))
        if choice is None:
            print("Cancelled. Nothing was split.")
            return 0
        name = mangas[choice - 1]
        print(f"\nSplitting {name}...")
        result = split_manga(metadata, name, _print_progress)
    except ImportError:
        print("\nPillow is missing from this Python environment.")
        print("Run scripts\\bootstrap-windows.ps1, then restart-server.cmd in the repository.")
        return 1
    except (OSError, ValueError) as exc:
        print(f"\nSplit failed: {exc}")
        print("Source images were not changed. Fix the problem and run the splitter again.")
        return 1

    label = "image" if result.image_count == 1 else "images"
    print(f"\nSplit {result.image_count} {label} into {result.image_count * 2} PNG pages:")
    print(f"  {result.path}")
    if result.cleanup_warning:
        print(f"Note: {result.cleanup_warning}")
    print("Move the finished folder into your library manually when ready.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Run the splitter again to finish it.")
        raise SystemExit(1)
