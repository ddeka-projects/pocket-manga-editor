"""Fail-closed filesystem mutations used by transactional exports."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import shutil
import stat
import sys

from .path_safety import is_link_or_reparse


_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004


def rename_no_replace(source: str | Path, destination: str | Path) -> None:
    """Atomically rename without ever replacing an existing destination.

    Python's :func:`os.rename` already has no-replace behavior on Windows, but
    POSIX rename replaces an existing file or empty directory.  Linux and
    macOS expose explicit kernel flags for the required operation.  Unknown
    POSIX platforms fail closed instead of falling back to a racy check.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        os.rename(source_path, destination_path)
        return
    if sys.platform == "darwin":  # pragma: no branch - platform selection
        _darwin_rename_no_replace(source_path, destination_path)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source_path, destination_path)
        return
    raise OSError(
        errno.ENOTSUP,
        "Atomic no-replace rename is unavailable on this platform",
        str(destination_path),
    )


def remove_managed_path(path: str | Path) -> None:
    """Remove one regular app-managed file or tree, including read-only data.

    Links, reparse points, and special files are rejected.  The complete tree
    is inspected and made owner-deletable before removal begins, which covers
    Windows read-only attributes and POSIX directories without write/search
    permission.  Callers remain responsible for retaining their transaction
    marker until every payload path has been removed successfully.
    """

    target = Path(path)
    if not os.path.lexists(target):
        return
    information = prepare_managed_path(target)
    if stat.S_ISREG(information.st_mode):
        target.unlink()
    elif stat.S_ISDIR(information.st_mode):
        shutil.rmtree(target, onerror=_retry_read_only_removal)
    else:  # Defensive: _prepare_managed_path already rejects this case.
        raise OSError(errno.EPERM, "Managed path has an unsupported type", str(target))
    if os.path.lexists(target):
        raise OSError(errno.EIO, "Managed path still exists after removal", str(target))


def prepare_managed_path(path: str | Path) -> os.stat_result:
    """Validate one managed tree and make its nodes owner-deletable."""

    target = Path(path)
    if not os.path.lexists(target):
        raise FileNotFoundError(errno.ENOENT, "Managed path is missing", str(target))
    if is_link_or_reparse(target):
        raise OSError(errno.ELOOP, "Managed path cannot be a link", str(target))
    information = target.stat(follow_symlinks=False)
    parent = target.parent
    if parent != target:
        if is_link_or_reparse(parent):
            raise OSError(
                errno.ELOOP, "Managed path parent cannot be a link", str(parent)
            )
        parent_information = parent.stat(follow_symlinks=False)
        if information.st_dev != parent_information.st_dev:
            raise OSError(
                errno.EXDEV,
                "Managed path is a mounted filesystem boundary",
                str(target),
            )
    _prepare_managed_path(target, root_device=information.st_dev)
    return information


def _prepare_managed_path(path: Path, *, root_device: int) -> os.stat_result:
    if is_link_or_reparse(path):
        raise OSError(errno.ELOOP, "Managed path cannot be a link", str(path))
    try:
        information = path.stat(follow_symlinks=False)
    except OSError:
        raise
    if information.st_dev != root_device:
        raise OSError(
            errno.EXDEV,
            "Managed tree crosses a mounted filesystem boundary",
            str(path),
        )
    if stat.S_ISREG(information.st_mode):
        _add_owner_access(path, information, directory=False)
        return information
    if not stat.S_ISDIR(information.st_mode):
        raise OSError(errno.EPERM, "Managed path has an unsupported type", str(path))

    _add_owner_access(path, information, directory=True)
    try:
        children = tuple(path.iterdir())
    except OSError:
        # A platform ACL can still deny traversal after chmod.  Let the caller
        # keep its transaction marker rather than beginning partial deletion.
        raise
    for child in children:
        _prepare_managed_path(child, root_device=root_device)
    return information


def _add_owner_access(
    path: Path, information: os.stat_result, *, directory: bool
) -> None:
    current = stat.S_IMODE(information.st_mode)
    required = stat.S_IRUSR | stat.S_IWUSR
    if directory:
        required |= stat.S_IXUSR
    desired = current | required
    if desired == current:
        return
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, desired, follow_symlinks=False)
    else:  # pragma: no cover - Windows lacks chmod(..., follow_symlinks=False)
        if is_link_or_reparse(path):
            raise OSError(errno.ELOOP, "Managed path became a link", str(path))
        os.chmod(path, desired)


def _retry_read_only_removal(function, path: str, exception_info) -> None:
    """One fail-closed chmod-and-retry hook for Python 3.10 ``rmtree``."""

    candidate = Path(path)
    if not os.path.lexists(candidate):
        return
    try:
        information = candidate.stat(follow_symlinks=False)
        if is_link_or_reparse(candidate) or not (
            stat.S_ISREG(information.st_mode) or stat.S_ISDIR(information.st_mode)
        ):
            raise OSError(errno.EPERM, "Unsafe path encountered during removal", path)
        _add_owner_access(
            candidate, information, directory=stat.S_ISDIR(information.st_mode)
        )
        function(path)
    except BaseException:
        original = exception_info[1]
        raise original.with_traceback(exception_info[2])


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, "renamex_np", None)
    if operation is None:
        raise OSError(
            errno.ENOTSUP,
            "macOS atomic no-replace rename is unavailable",
            str(destination),
        )
    operation.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    operation.restype = ctypes.c_int
    _invoke_rename(
        operation,
        (os.fsencode(source), os.fsencode(destination), _DARWIN_RENAME_EXCL),
        source,
        destination,
    )


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, "renameat2", None)
    if operation is None:
        raise OSError(
            errno.ENOTSUP,
            "Linux atomic no-replace rename is unavailable",
            str(destination),
        )
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    _invoke_rename(
        operation,
        (
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _LINUX_RENAME_NOREPLACE,
        ),
        source,
        destination,
    )


def _invoke_rename(
    operation, arguments: tuple[object, ...], source: Path, destination: Path
) -> None:
    ctypes.set_errno(0)
    if operation(*arguments) == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    error = OSError(error_number, os.strerror(error_number), str(source))
    error.filename2 = str(destination)
    raise error
