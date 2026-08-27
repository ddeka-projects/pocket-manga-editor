"""Headless server configuration loaded from environment variables and ``.env``."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .path_safety import is_link_or_reparse


WORKING_DIRECTORY_VARIABLE = "POCKET_MANGA_EDITOR_WORKING_DIRECTORY"
HOST_VARIABLE = "POCKET_MANGA_EDITOR_HOST"
PORT_VARIABLE = "POCKET_MANGA_EDITOR_PORT"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


class ConfigurationError(RuntimeError):
    """Raised when the headless server configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ServerConfiguration:
    working_directory: Path
    host: str
    port: int


def default_env_path() -> Path:
    """Return the repository-level ``.env`` path, independent of process CWD."""

    return Path(__file__).resolve().parent.parent / ".env"


def load_configuration(
    env_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ServerConfiguration:
    """Load configuration, with real environment variables overriding ``.env``."""

    source = default_env_path() if env_path is None else Path(env_path)
    file_values = _read_env_file(source)
    process_values = os.environ if environ is None else environ

    def value(name: str, default: str | None = None) -> str | None:
        if name in process_values:
            return process_values[name]
        return file_values.get(name, default)

    raw_working_directory = value(WORKING_DIRECTORY_VARIABLE)
    if raw_working_directory is None or not raw_working_directory.strip():
        raise ConfigurationError(
            f"{WORKING_DIRECTORY_VARIABLE} must contain an absolute existing directory."
        )
    working_directory = _working_directory(raw_working_directory.strip())

    raw_host = value(HOST_VARIABLE, DEFAULT_HOST)
    assert raw_host is not None
    host = _host(raw_host.strip())

    raw_port = value(PORT_VARIABLE, str(DEFAULT_PORT))
    assert raw_port is not None
    port = _port(raw_port.strip())
    return ServerConfiguration(working_directory, host, port)


def _read_env_file(path: Path) -> dict[str, str]:
    if not os.path.lexists(path):
        return {}
    if is_link_or_reparse(path):
        raise ConfigurationError(f"The environment file cannot be a link: '{path}'.")
    try:
        information = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(information.st_mode):
            raise ConfigurationError(
                f"The environment path is not a regular file: '{path}'."
            )
        text = path.read_text(encoding="utf-8-sig")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            f"Could not read the environment file '{path}': {exc}"
        ) from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ConfigurationError(
                f"Invalid .env assignment on line {line_number} of '{path}'."
            )
        if name in values:
            raise ConfigurationError(
                f"Duplicate .env variable '{name}' on line {line_number} of '{path}'."
            )
        values[name] = _env_value(raw_value.strip(), path, line_number)
    return values


def _env_value(value: str, path: Path, line_number: int) -> str:
    if not value or value[0] not in {"'", '"'}:
        return value
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise ConfigurationError(
            f"Unclosed quoted value on line {line_number} of '{path}'."
        )
    return value[1:-1]


def _working_directory(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ConfigurationError(
            f"{WORKING_DIRECTORY_VARIABLE} must be an absolute path: '{value}'."
        )
    if is_link_or_reparse(candidate):
        raise ConfigurationError(
            f"{WORKING_DIRECTORY_VARIABLE} cannot be a symbolic link or junction."
        )
    try:
        information = candidate.stat(follow_symlinks=False)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError(
            f"Could not inspect {WORKING_DIRECTORY_VARIABLE} '{candidate}': {exc}"
        ) from exc
    if not stat.S_ISDIR(information.st_mode):
        raise ConfigurationError(
            f"{WORKING_DIRECTORY_VARIABLE} is not a directory: '{candidate}'."
        )
    return resolved


def _host(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise ConfigurationError(f"{HOST_VARIABLE} is not a valid host.")
    normalized = value.rstrip(".")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", normalized) or not _HOSTNAME.fullmatch(
            normalized
        ):
            raise ConfigurationError(
                f"{HOST_VARIABLE} must be an IPv4 address or hostname."
            )
    else:
        if address.version != 4:
            raise ConfigurationError(f"{HOST_VARIABLE} currently requires IPv4.")
    return normalized


def _port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise ConfigurationError(
            f"{PORT_VARIABLE} must be an integer from 1 through 65535."
        ) from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError(
            f"{PORT_VARIABLE} must be an integer from 1 through 65535."
        )
    return port
