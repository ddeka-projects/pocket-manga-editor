"""Literal UTF-8 .env settings; real environment values take precedence."""

import os
from pathlib import Path
import re
import stat


def read_env_file(path):
    path = Path(path)
    if not os.path.lexists(path):
        return {}
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if not stat.S_ISREG(info.st_mode) or reparse:
        raise ValueError(f"Configuration must be a regular file: {path}")
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment on line {number} of {path}.")
        if key in values:
            raise ValueError(f"Duplicate .env variable {key} on line {number} of {path}.")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed quoted value on line {number} of {path}.")
            value = value[1:-1]
        values[key] = value
    return values


def load_env(path, *, environ=None):
    values = read_env_file(path)
    values.update(os.environ if environ is None else environ)
    return values
