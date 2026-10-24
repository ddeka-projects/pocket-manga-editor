"""Install the PC-side export companion into the library metadata folder.

The export script is developed in ``companion/`` and copied over any older
copy at every server start, so the double-clickable version always matches
this repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .path_safety import is_link_or_reparse
from .workspace import WorkspaceError, metadata_directory


COMPANION_SOURCE = Path(__file__).with_name("companion")
COMPANION_FILES = ("_exporter.py", "_exporter.bat")


def install_companion(working_directory: str | Path) -> Path:
    """Copy the export companion files into the metadata folder, replacing old copies."""

    metadata = metadata_directory(working_directory)
    try:
        metadata.mkdir(exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"Could not create the app metadata folder: {exc}") from exc
    for name in COMPANION_FILES:
        target = metadata / name
        if os.path.lexists(target) and (is_link_or_reparse(target) or not target.is_file()):
            raise WorkspaceError(f"The companion path '{name}' is not a safe regular file.")
        content = (COMPANION_SOURCE / name).read_bytes()
        if name.endswith(".bat"):
            # cmd.exe needs CRLF regardless of how the repository was checked out.
            content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        _replace_file(target, content)
    return metadata


def _replace_file(target: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
