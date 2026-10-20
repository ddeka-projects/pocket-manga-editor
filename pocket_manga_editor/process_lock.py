"""OS-owned lifetime guard for one app using one canonical data location."""

import hashlib
import os
from pathlib import Path
import stat


class ProcessLock:
    def __init__(self, data_path):
        identity = f"{__package__}:{os.path.normcase(str(Path(data_path).resolve()))}"
        self.name = "Pocket-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return self
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = self.kernel.CreateMutexW(None, False, "Global\\" + self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:
                self.kernel.CloseHandle(handle)
                raise RuntimeError(
                    "Another server already uses this app's data location. Stop it first."
                )
            self.handle = handle
        else:
            import fcntl

            path = Path("/tmp") / (self.name + ".lock")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RuntimeError("The server guard is not a regular file.")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(descriptor)
                raise RuntimeError(
                    "Another server already uses this app's data location. Stop it first."
                ) from error
            except BaseException:
                os.close(descriptor)
                raise
            self.handle = descriptor
        return self

    def close(self):
        if self.handle is not None:
            if os.name == "nt":
                self.kernel.CloseHandle(self.handle)
            else:
                os.close(self.handle)
            self.handle = None

    release = close

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
