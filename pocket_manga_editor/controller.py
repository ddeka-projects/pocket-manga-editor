"""One explicit browser owner; ownership never expires or polls."""

from contextlib import contextmanager
import re
import secrets
import threading

from .errors import (
    ControllerError,
    ControllerOccupiedError,
    ControllerRequiredError,
    ControllerTransferredError,
)


class ControllerOwnership:
    def __init__(self):
        self.lock = threading.RLock()
        self.page = None
        self.token = None

    @contextmanager
    def claimed(self, page_id, *, takeover=False):
        if not isinstance(page_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", page_id):
            raise ControllerError("Invalid page identity.", 400, "invalid_request")
        with self.lock:
            if self.page is not None and self.page != page_id and not takeover:
                raise ControllerOccupiedError()
            self.page = page_id
            self.token = secrets.token_urlsafe(32)
            yield {"token": self.token}

    def claim(self, page_id, *, takeover=False):
        with self.claimed(page_id, takeover=takeover) as grant:
            return grant

    def takeover(self, page_id):
        return self.claim(page_id, takeover=True)

    def _authorize(self, page_id, token):
        if self.page is None:
            raise ControllerRequiredError()
        if (
            page_id != self.page
            or not isinstance(token, str)
            or not token.isascii()
            or not secrets.compare_digest(token, self.token)
        ):
            raise ControllerTransferredError()

    def authorize(self, page_id, token):
        with self.lock:
            self._authorize(page_id, token)

    @contextmanager
    def authorized(self, page_id, token):
        # The same lock protects takeover and the entire authorized operation.
        with self.lock:
            self._authorize(page_id, token)
            yield

    operation = authorized

    def release(self, page_id, token):
        with self.lock:
            if self.page is not None:
                self._authorize(page_id, token)
                self.page = self.token = None

    def disconnect(self):
        with self.lock:
            self.page = self.token = None

    def snapshot(self):
        with self.lock:
            return {"claimed": self.page is not None}
