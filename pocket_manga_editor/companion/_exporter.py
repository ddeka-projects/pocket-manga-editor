"""Export one manga's selected pages for Pocket Manga Editor.

The server copies this script and ``_exporter.bat`` into the library's
``.pocket-manga-editor`` folder at every start. Double-click the .bat file to
run it. It lists manga with selections in their ``<manga>.edit.json`` file,
copies the chosen manga's selected images into
``<manga>.export\\<entry>\\<entry>__<image>``, and replaces any previous
export. Source images are only read. Uses only the Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Callable

EDIT_SUFFIX = ".edit.json"
EXPORT_SUFFIX = ".export"
TEMPORARY_SUFFIX = ".export-tmp"
PREVIOUS_SUFFIX = ".export-old"
IMAGE_SUFFIXES = frozenset({".jpg", ".png"})
_NUMBER = re.compile(r"(\d+)")


class MissingSourcesError(Exception):
    """Selected source images are missing; nothing was changed."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"{len(missing)} selected image(s) are missing")
        self.missing = missing


@dataclass(frozen=True)
class MangaSelection:
    name: str
    folders: dict[str, list[str]]

    @property
    def image_count(self) -> int:
        return sum(len(images) for images in self.folders.values())


@dataclass(frozen=True)
class ExportResult:
    path: Path
    image_count: int
    cleanup_warning: str | None = None


def natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in _NUMBER.split(value)
        if token
    )


def find_selections(metadata: Path) -> tuple[list[MangaSelection], list[str]]:
    """Return manga with at least one selection, plus unreadable edit files."""

    selections: list[MangaSelection] = []
    problems: list[str] = []
    for path in metadata.iterdir():
        if not path.name.endswith(EDIT_SUFFIX) or not _is_plain(path, directory=False):
            continue
        name = path.name[: -len(EDIT_SUFFIX)]
        try:
            if not _safe_name(name):
                raise ValueError("unsafe manga name")
            folders = _read_selected(path)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if folders:
            selections.append(MangaSelection(name, folders))
    selections.sort(key=lambda selection: natural_key(selection.name))
    return selections, problems


def export_manga(
    metadata: Path,
    selection: MangaSelection,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> ExportResult:
    """Rebuild ``<manga>.export`` from the selection, replacing any previous export."""

    source_manga = metadata.parent / selection.name
    plan: list[tuple[Path, Path]] = []
    missing: list[str] = []
    for folder in sorted(selection.folders, key=natural_key):
        for image in sorted(selection.folders[folder], key=natural_key):
            source = source_manga / folder / image
            if (
                _is_plain(source_manga, directory=True)
                and _is_plain(source.parent, directory=True)
                and _is_plain(source, directory=False)
            ):
                plan.append((source, Path(folder) / f"{folder}__{image}"))
            else:
                missing.append(f"{folder}/{image}")
    if missing:
        raise MissingSourcesError(missing)

    final = metadata / f"{selection.name}{EXPORT_SUFFIX}"
    temporary = metadata / f"{selection.name}{TEMPORARY_SUFFIX}"
    previous = metadata / f"{selection.name}{PREVIOUS_SUFFIX}"
    # Leftovers exist only if an earlier run was interrupted.
    _remove(temporary)
    _remove(previous)

    temporary.mkdir()
    for done, (source, relative) in enumerate(plan, start=1):
        target = temporary / relative
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        progress(done, len(plan))

    if os.path.lexists(final):
        os.rename(final, previous)
    os.rename(temporary, final)
    try:
        _remove(previous)
    except OSError as exc:
        return ExportResult(
            final,
            len(plan),
            f"The previous export could not be deleted ({exc}); the next export removes it.",
        )
    return ExportResult(final, len(plan))


def main() -> int:
    metadata = Path(__file__).resolve().parent
    print("Pocket Manga Editor export")
    print(f"Library: {metadata.parent}")
    print()
    selections, problems = find_selections(metadata)
    for problem in problems:
        print(f"Skipped unreadable selection file {problem}")
    if not selections:
        print("No manga has selected images. Select pages in Edit first.")
        return 0

    print("Manga with selected images:")
    for number, selection in enumerate(selections, start=1):
        print(f"  {number}. {selection.name} ({_images(selection.image_count)})")
    print()
    choice = _choose(len(selections))
    if choice is None:
        print("Cancelled. Nothing was exported.")
        return 0

    selection = selections[choice - 1]
    print()
    print(f"Exporting {selection.name}...")
    try:
        result = export_manga(metadata, selection, _print_progress)
    except MissingSourcesError as exc:
        print("Export stopped: these selected images no longer exist in the library:")
        for item in exc.missing:
            print(f"  {item}")
        print("Nothing was changed. Restore the images, or open this manga in Edit")
        print("and change any selection so stale entries are removed, then export again.")
        return 1
    except OSError as exc:
        print()
        print(f"Export failed: {exc}")
        print("Source images were not changed. Run the export again.")
        return 1

    print()
    print(f"Exported {_images(result.image_count)} to:")
    print(f"  {result.path}")
    if result.cleanup_warning:
        print(f"Note: {result.cleanup_warning}")
    return 0


def _read_selected(path: Path) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    folders = payload.get("folders") if isinstance(payload, dict) else None
    if not isinstance(folders, dict):
        raise ValueError("unrecognized format")
    selected: dict[str, list[str]] = {}
    for folder, value in folders.items():
        images = value.get("selected_images") if isinstance(value, dict) else None
        if (
            not _safe_name(folder)
            or not isinstance(images, list)
            or not all(_safe_name(image) for image in images)
            or any(Path(image).suffix.casefold() not in IMAGE_SUFFIXES for image in images)
        ):
            raise ValueError(f"unrecognized or unsafe entry '{folder}'")
        if images:
            selected[folder] = list(images)
    return selected


def _safe_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not any(character in value for character in "/\\:\x00")
        and Path(value).name == value
    )


def _is_plain(path: Path, *, directory: bool) -> bool:
    """True for an existing directory or regular file that is not a link."""

    try:
        information = os.lstat(path)
    except OSError:
        return False
    if _is_link(information):
        return False
    return stat.S_ISDIR(information.st_mode) if directory else stat.S_ISREG(information.st_mode)


def _is_link(information: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & reparse)


def _remove(path: Path) -> None:
    """Delete a leftover export folder without following links out of it."""

    try:
        information = os.lstat(path)
    except FileNotFoundError:
        return
    if _is_link(information) or not stat.S_ISDIR(information.st_mode):
        os.unlink(path)
        return

    def make_writable_and_retry(function, failed_path, _error) -> None:
        # Windows refuses to delete read-only files copied from read-only sources.
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


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
    print(f"\rCopying {done} of {total}", end="", flush=True)


def _images(count: int) -> str:
    return "1 image" if count == 1 else f"{count} images"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        print("Cancelled. Run the export again to finish it.")
        raise SystemExit(1)
