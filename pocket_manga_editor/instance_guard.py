"""Process-lifetime single-instance guard for the desktop application.

The lock deliberately has no age-based expiry.  A long-running or suspended
instance must never lose its lock merely because the lock file is old.  Qt can
still reclaim a lock left by a process that it can prove is no longer running,
using the process and host metadata written by :class:`QLockFile`.

Unverifiable lock files are not removed automatically.  In that situation the
startup error identifies the lock file so a user can remove it after confirming
that no application instance is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths


INSTANCE_LOCK_FILENAME = "instance.lock"

# Zero disables time-based staleness. QLockFile still detects locks whose local
# owner process has exited, which safely covers ordinary crashes.
STALE_LOCK_TIME_MS = 0


@dataclass(frozen=True, slots=True)
class InstanceOwnerInfo:
    """Owner metadata reported by Qt for an existing instance lock."""

    pid: int
    hostname: str
    application: str


class InstanceGuardError(RuntimeError):
    """Raised when the process-lifetime instance guard cannot be acquired."""

    def __init__(
        self,
        message: str,
        *,
        lock_path: Path | None = None,
        lock_error: QLockFile.LockError | None = None,
    ) -> None:
        super().__init__(message)
        self.lock_path = lock_path
        self.lock_error = lock_error


class InstanceAlreadyRunningError(InstanceGuardError):
    """Raised when another instance owns or may own the instance lock."""

    def __init__(
        self,
        message: str,
        *,
        lock_path: Path,
        owner: InstanceOwnerInfo | None,
        lock_error: QLockFile.LockError,
    ) -> None:
        super().__init__(
            message,
            lock_path=lock_path,
            lock_error=lock_error,
        )
        self.owner = owner


class InstanceGuard:
    """An acquired guard that must be retained for the process lifetime."""

    def __init__(self, lock_file: QLockFile, lock_path: Path) -> None:
        self._lock_file = lock_file
        self._lock_path = lock_path
        self._is_acquired = True

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def is_acquired(self) -> bool:
        return self._is_acquired

    def release(self) -> None:
        """Release the guard; calling this more than once is harmless."""

        if not self._is_acquired:
            return
        self._lock_file.unlock()
        self._is_acquired = False

    def __enter__(self) -> InstanceGuard:
        if not self._is_acquired:
            raise RuntimeError("The application instance guard has been released.")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def default_instance_lock_path() -> Path:
    """Return the current OS user's application-data instance lock path."""

    app_data = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not app_data:
        raise InstanceGuardError(
            "The operating system did not provide an application-data folder; "
            "Pocket Manga Editor cannot verify that only one instance is running."
        )
    return Path(app_data) / INSTANCE_LOCK_FILENAME


def acquire_instance_guard(lock_path: str | Path | None = None) -> InstanceGuard:
    """Acquire and return a nonblocking, process-lifetime instance guard.

    The caller must keep the returned object alive until application shutdown.
    ``lock_path`` is primarily useful for isolated tests and embedding; normal
    application startup should omit it so the per-user app-data path is used.
    """

    path = (
        default_instance_lock_path()
        if lock_path is None
        else Path(lock_path).expanduser().absolute()
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstanceGuardError(
            "Pocket Manga Editor could not create its instance-lock folder at "
            f"'{path.parent}': {exc}",
            lock_path=path,
        ) from exc

    lock_file = QLockFile(str(path))
    lock_file.setStaleLockTime(STALE_LOCK_TIME_MS)
    if lock_file.tryLock(0):
        return InstanceGuard(lock_file, path)

    lock_error = lock_file.error()
    if lock_error == QLockFile.LockError.LockFailedError:
        owner = _read_owner(lock_file)
        if owner is not None:
            details = [f"PID {owner.pid}"] if owner.pid > 0 else []
            if owner.hostname:
                details.append(f"host {owner.hostname}")
            if owner.application:
                details.append(f"application {owner.application}")
            owner_description = ", ".join(details) or "an unknown owner"
            message = (
                "Pocket Manga Editor is already running "
                f"({owner_description}). Close the existing instance and try again. "
                f"Instance lock: '{path}'."
            )
        else:
            message = (
                "Pocket Manga Editor could not verify who owns its instance lock. "
                "Close every running instance and try again. If none is running, "
                f"remove the stale lock file '{path}' and restart the application."
            )
        raise InstanceAlreadyRunningError(
            message,
            lock_path=path,
            owner=owner,
            lock_error=lock_error,
        )

    if lock_error == QLockFile.LockError.PermissionError:
        message = (
            "Pocket Manga Editor cannot access its instance lock. Check the "
            f"permissions of the application-data folder '{path.parent}' and try again."
        )
    else:
        message = (
            "Pocket Manga Editor could not acquire its instance lock at "
            f"'{path}'. Close any running instance and try again."
        )
    raise InstanceGuardError(
        message,
        lock_path=path,
        lock_error=lock_error,
    )


def _read_owner(lock_file: QLockFile) -> InstanceOwnerInfo | None:
    pid, hostname, application = lock_file.getLockInfo()
    if pid <= 0 and not hostname and not application:
        return None
    return InstanceOwnerInfo(
        pid=pid,
        hostname=hostname,
        application=application,
    )
