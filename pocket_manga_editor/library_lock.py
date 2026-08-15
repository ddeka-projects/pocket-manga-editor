"""Cross-process serialization for mutations within one manga library."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
from typing import Iterator

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised on POSIX
    import fcntl


LOCK_FILENAME = ".library-mutation.lock"


class LibraryLockError(OSError):
    """Raised when a library mutation lock cannot be acquired safely."""


class LibraryBusyError(LibraryLockError):
    """Raised when another process currently owns the mutation lock."""


@contextmanager
def library_mutation_lock(working_directory: str | Path) -> Iterator[Path]:
    """Yield the resolved library root while holding its nonblocking lock."""

    raw_root = Path(working_directory).expanduser()
    if raw_root.is_symlink():
        raise LibraryLockError("The working directory cannot be a symbolic link.")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise LibraryLockError(
            f"The working directory could not be resolved: {exc}"
        ) from exc
    if not root.is_dir():
        raise LibraryLockError(f"The working directory is not a folder: {root}")

    metadata = root / ".pocket-manga-editor"
    if os.path.lexists(metadata):
        if metadata.is_symlink() or not metadata.is_dir():
            raise LibraryLockError("The app metadata folder is not a safe directory.")
        if not metadata.resolve().is_relative_to(root):
            raise LibraryLockError(
                "The app metadata folder points outside the working directory."
            )
    try:
        metadata.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LibraryLockError(f"Could not create the app metadata folder: {exc}") from exc

    lock_path = metadata / LOCK_FILENAME
    descriptor: int | None = None
    try:
        if os.path.lexists(lock_path) and (
            lock_path.is_symlink() or not lock_path.is_file()
        ):
            raise LibraryLockError("The library mutation lock is not a safe file.")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LibraryLockError("The library mutation lock is not a regular file.")

        if os.name == "nt":  # pragma: no cover - exercised on Windows
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on POSIX
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except LibraryLockError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        busy_errors = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
        if hasattr(errno, "EWOULDBLOCK"):
            busy_errors.add(errno.EWOULDBLOCK)
        if exc.errno in busy_errors:
            raise LibraryBusyError(
                "Another library mutation is already in progress."
            ) from exc
        raise LibraryLockError(f"Could not acquire the library mutation lock: {exc}") from exc

    try:
        yield root
    finally:
        try:
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised on POSIX
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor releases either OS lock. An explicit
                # unlock failure must never replace a successful mutation with
                # an apparent failure after its commit point.
                pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                # The mutation result is already determined; there is no safe
                # rollback to perform for a close failure.
                pass
