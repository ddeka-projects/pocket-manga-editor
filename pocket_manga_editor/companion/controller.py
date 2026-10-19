"""Explicit in-memory ownership for the web application's active page."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import re
import secrets
import threading
from typing import Iterator


class ControllerError(RuntimeError):
    code = "controller_error"


class ControllerOccupiedError(ControllerError):
    code = "controller_occupied"


class ControllerRequiredError(ControllerError):
    code = "controller_required"


class ControllerTransferredError(ControllerError):
    code = "controller_transferred"


_PAGE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True, slots=True)
class ControllerGrant:
    page_id: str
    token: str


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    connected: bool
    page_id: str | None


class ControllerOwnership:
    """Serialize controller changes and authorized work under one lock.

    Ownership does not expire.  A different page can replace it explicitly by
    claiming with ``takeover=True``.  Holding the same lock while authorized
    work executes means a takeover cannot invalidate a mutation halfway
    through its durable commit.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._page_id: str | None = None
        self._token: str | None = None

    def claim(self, page_id: str, *, takeover: bool = False) -> ControllerGrant:
        with self.claimed(page_id, takeover=takeover) as grant:
            return grant

    @contextmanager
    def claimed(
        self, page_id: str, *, takeover: bool = False
    ) -> Iterator[ControllerGrant]:
        self._validate_page_id(page_id)
        with self._lock:
            if (
                self._page_id is not None
                and self._page_id != page_id
                and not takeover
            ):
                raise ControllerOccupiedError(
                    "Another page currently controls this library."
                )
            token = secrets.token_urlsafe(32)
            self._page_id = page_id
            self._token = token
            yield ControllerGrant(page_id, token)

    @contextmanager
    def authorized(self, page_id: str, token: str) -> Iterator[None]:
        with self._lock:
            self._authorize_locked(page_id, token)
            yield

    def release(self, page_id: str, token: str) -> None:
        with self._lock:
            self._authorize_locked(page_id, token)
            self._clear_locked()

    def disconnect(self) -> None:
        with self._lock:
            self._clear_locked()

    def snapshot(self) -> ControllerSnapshot:
        with self._lock:
            return ControllerSnapshot(self._page_id is not None, self._page_id)

    def _authorize_locked(self, page_id: str, token: str) -> None:
        self._validate_page_id(page_id)
        if self._page_id is None or self._token is None:
            raise ControllerRequiredError(
                "Claim controller ownership before using the library."
            )
        if (
            self._page_id != page_id
            or not isinstance(token, str)
            or not _TOKEN.fullmatch(token)
            or not hmac.compare_digest(self._token, token)
        ):
            raise ControllerTransferredError(
                "Controller ownership has been transferred to another page."
            )

    @staticmethod
    def _validate_page_id(page_id: str) -> None:
        if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
            raise ControllerError("page_id must be 1-128 URL-safe characters.")

    def _clear_locked(self) -> None:
        self._page_id = None
        self._token = None
