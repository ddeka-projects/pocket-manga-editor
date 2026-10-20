"""Bounded application logs shared by foreground and scheduled startup."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading


_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(*, console=True, root=None):
    directory = (Path(root) if root is not None else Path(__file__).resolve().parents[1]) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "server.log"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if not any(
        getattr(handler, "_pocket_log_path", None) == str(path) for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler._pocket_log_path = str(path)
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    if (
        console
        and sys.stderr is not None
        and not isinstance(sys.stderr, LogStream)
        and not any(getattr(handler, "_pocket_console", False) for handler in logger.handlers)
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler._pocket_console = True
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    return path


class LogStream:
    def __init__(self, name, level):
        self.logger = logging.getLogger(name)
        self.level = level
        self.buffer = ""
        self.lock = threading.RLock()
        self.encoding = "utf-8"

    def write(self, value):
        with self.lock:
            self.buffer += value
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        return len(value)

    def flush(self):
        with self.lock:
            if self.buffer.strip():
                self.logger.log(self.level, self.buffer.rstrip())
            self.buffer = ""

    def isatty(self):
        return False


def redirect_standard_streams():
    sys.stdout = LogStream(__package__ + ".stdout", logging.INFO)
    sys.stderr = LogStream(__package__ + ".stderr", logging.ERROR)
