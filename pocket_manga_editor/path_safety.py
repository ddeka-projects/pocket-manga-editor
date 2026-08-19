"""Cross-version filesystem link and Windows reparse-point detection."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def is_link_or_reparse(path: str | Path) -> bool:
    """Fail closed for symlinks, junctions, and all Windows reparse points."""

    candidate = Path(path)
    try:
        information = candidate.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(information.st_mode):
        return True
    if os.name == "nt":
        attributes = getattr(information, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse_flag:
            return True
    is_junction = getattr(candidate, "is_junction", None)
    try:
        return bool(is_junction and is_junction())
    except OSError:
        return True
