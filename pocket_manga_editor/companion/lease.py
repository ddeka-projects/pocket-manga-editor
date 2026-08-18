"""Single-controller lease management for Companion Mode."""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Callable


class LeaseError(RuntimeError):
    code = "lease_error"


class LeaseConflictError(LeaseError):
    code = "lease_conflict"


class LeaseExpiredError(LeaseError):
    code = "lease_expired"


_CLIENT_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_PAGE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    instance_id: str | None
    connected: bool
    lease_expires_at: float | None
    grace_expires_at: float | None
    page_instance_id: str | None = None


class ControllerLease:
    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        grace_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or grace_seconds < 0:
            raise ValueError("Lease durations are invalid.")
        self._lock = threading.RLock()
        self._ttl = ttl_seconds
        self._grace = grace_seconds
        self._clock = clock
        self._instance_id: str | None = None
        self._page_instance_id: str | None = None
        self._lease_expires = 0.0
        self._grace_expires = 0.0

    def claim(
        self, instance_id: str, page_instance_id: str | None = None
    ) -> LeaseSnapshot:
        page_instance_id = self._validated_identity(instance_id, page_instance_id)
        with self._lock:
            now = self._clock()
            if self._instance_id is not None and now > self._grace_expires:
                self._clear_locked()
            if self._instance_id is None:
                self._assign_locked(instance_id, page_instance_id, now)
                return self._snapshot_locked(now)

            exact_owner = (
                self._instance_id == instance_id
                and self._page_instance_id == page_instance_id
            )
            same_client_after_live_ttl = (
                self._instance_id == instance_id and now > self._lease_expires
            )
            if not exact_owner and not same_client_after_live_ttl:
                raise LeaseConflictError("Another Companion client controls this library.")
            self._assign_locked(instance_id, page_instance_id, now)
            return self._snapshot_locked(now)

    def heartbeat(
        self, instance_id: str, page_instance_id: str | None = None
    ) -> LeaseSnapshot:
        page_instance_id = self._validated_identity(instance_id, page_instance_id)
        with self._lock:
            now = self._clock()
            if self._instance_id is not None and now > self._grace_expires:
                self._clear_locked()
            if self._instance_id is None:
                raise LeaseExpiredError("The controller lease has expired.")
            if (
                self._instance_id != instance_id
                or self._page_instance_id != page_instance_id
            ):
                raise LeaseConflictError("Another Companion client owns the lease.")
            self._assign_locked(instance_id, page_instance_id, now)
            return self._snapshot_locked(now)

    def authorize(
        self, instance_id: str, page_instance_id: str | None = None
    ) -> LeaseSnapshot:
        page_instance_id = self._validated_identity(instance_id, page_instance_id)
        with self._lock:
            now = self._clock()
            if self._instance_id is not None and now > self._grace_expires:
                self._clear_locked()
            if self._instance_id is None:
                raise LeaseExpiredError("No live controller lease exists.")
            if (
                self._instance_id != instance_id
                or self._page_instance_id != page_instance_id
            ):
                raise LeaseConflictError("Another Companion client owns the lease.")
            if now > self._lease_expires:
                raise LeaseExpiredError("The controller must reconnect or heartbeat.")
            return self._snapshot_locked(now)

    def release(self, instance_id: str, page_instance_id: str | None = None) -> None:
        page_instance_id = self._validated_identity(instance_id, page_instance_id)
        with self._lock:
            now = self._clock()
            if self._instance_id is not None and now > self._grace_expires:
                self._clear_locked()
            if self._instance_id is None:
                return
            if (
                self._instance_id != instance_id
                or self._page_instance_id != page_instance_id
            ):
                raise LeaseConflictError("Another Companion client owns the lease.")
            self._clear_locked()

    def disconnect(self) -> None:
        with self._lock:
            self._clear_locked()

    def snapshot(self) -> LeaseSnapshot:
        with self._lock:
            now = self._clock()
            if self._instance_id is not None and now > self._grace_expires:
                self._clear_locked()
            return self._snapshot_locked(now)

    @staticmethod
    def _validated_identity(
        instance_id: str, page_instance_id: str | None
    ) -> str | None:
        if not isinstance(instance_id, str) or not _CLIENT_ID.fullmatch(instance_id):
            raise LeaseError("client_id must be 1-128 URL-safe characters.")
        if page_instance_id is None:
            return None
        if (
            not isinstance(page_instance_id, str)
            or not _PAGE_INSTANCE_ID.fullmatch(page_instance_id)
        ):
            raise LeaseError("page_id must be 1-128 URL-safe characters.")
        return page_instance_id

    def _assign_locked(
        self, instance_id: str, page_instance_id: str | None, now: float
    ) -> None:
        self._instance_id = instance_id
        self._page_instance_id = page_instance_id
        self._lease_expires = now + self._ttl
        self._grace_expires = self._lease_expires + self._grace

    def _snapshot_locked(self, now: float) -> LeaseSnapshot:
        return LeaseSnapshot(
            self._instance_id,
            self._instance_id is not None and now <= self._lease_expires,
            self._lease_expires if self._instance_id is not None else None,
            self._grace_expires if self._instance_id is not None else None,
            self._page_instance_id,
        )

    def _clear_locked(self) -> None:
        self._instance_id = None
        self._page_instance_id = None
        self._lease_expires = 0.0
        self._grace_expires = 0.0
