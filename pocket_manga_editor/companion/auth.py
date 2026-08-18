"""Pair-once authentication that persists only a one-way credential verifier."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import Callable, Protocol


class AuthenticationError(RuntimeError):
    code = "unauthorized"


class UnpairedError(AuthenticationError):
    code = "unpaired"


class PairingClosedError(AuthenticationError):
    code = "pairing_closed"


class InvalidPairingCodeError(AuthenticationError):
    code = "invalid_pairing_code"


class PairingRateLimitedError(AuthenticationError):
    code = "pairing_rate_limited"


class CredentialPersistenceError(OSError):
    """The live credential was revoked but its persisted verifier remains."""


class CredentialStore(Protocol):
    def load(self) -> str | None: ...
    def save(self, verifier: str) -> None: ...
    def clear(self) -> None: ...


def _verifier(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _valid_verifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class CredentialVerifierStore:
    """Thread-safe atomic JSON storage for a credential hash, never its secret."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def load(self) -> str | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise OSError(f"Could not load Companion credentials: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise OSError("Companion credentials use an unsupported format.")
            value = payload.get("credential_verifier")
            if not _valid_verifier(value):
                raise OSError("Companion credentials are invalid.")
            return value

    def save(self, verifier: str) -> None:
        if not _valid_verifier(verifier):
            raise ValueError("credential verifier must be a SHA-256 hexadecimal digest")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            try:
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    try:
                        fchmod(descriptor, 0o600)
                    except OSError:
                        pass
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        {
                            "schema_version": self.SCHEMA_VERSION,
                            "credential_verifier": verifier,
                        },
                        handle,
                        indent=2,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except BaseException:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class PairingOffer:
    code: str
    expires_at: float
    attempts_remaining: int


class PairingManager:
    """Own pairing windows and device credentials without retaining plaintext."""

    def __init__(
        self,
        *,
        verifier: str | None = None,
        store: CredentialStore | None = None,
        clock: Callable[[], float] = time.time,
        code_factory: Callable[[], str] | None = None,
        credential_factory: Callable[[], str] | None = None,
    ) -> None:
        if verifier is not None and store is not None:
            raise ValueError("Pass either verifier or store, not both.")
        self._lock = threading.RLock()
        self._store = store
        loaded = store.load() if store is not None else None
        initial = verifier if verifier is not None else loaded
        if initial is not None and not _valid_verifier(initial):
            raise ValueError("Invalid persisted credential verifier.")
        self._verifier = initial
        self._revocation_pending = False
        self._clock = clock
        self._code_factory = code_factory or (
            lambda: f"{secrets.randbelow(1_000_000):06d}"
        )
        self._credential_factory = credential_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._code: str | None = None
        self._expires_at = 0.0
        self._attempts_remaining = 0

    @property
    def paired(self) -> bool:
        with self._lock:
            return self._verifier is not None or self._revocation_pending

    @property
    def revocation_pending(self) -> bool:
        with self._lock:
            return self._revocation_pending

    @property
    def paired_verifier(self) -> str | None:
        with self._lock:
            return self._verifier

    @property
    def pairing_offer(self) -> PairingOffer | None:
        with self._lock:
            if self._code is None or self._clock() >= self._expires_at:
                self._clear_offer_locked()
                return None
            return PairingOffer(
                self._code, self._expires_at, self._attempts_remaining
            )

    def open_pairing(
        self, *, ttl_seconds: float = 300.0, max_attempts: int = 5
    ) -> PairingOffer:
        if ttl_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Pairing limits must be positive.")
        code = self._code_factory()
        if len(code) != 6 or not code.isascii() or not code.isdecimal():
            raise RuntimeError("Pairing code generator returned an invalid code.")
        with self._lock:
            self._code = code
            self._expires_at = self._clock() + ttl_seconds
            self._attempts_remaining = max_attempts
            return PairingOffer(code, self._expires_at, max_attempts)

    def pair(self, code: str) -> str:
        with self._lock:
            now = self._clock()
            if self._code is None or now >= self._expires_at:
                self._clear_offer_locked()
                raise PairingClosedError("No pairing window is open.")
            if self._attempts_remaining <= 0:
                self._clear_offer_locked()
                raise PairingRateLimitedError("Pairing attempts were exhausted.")
            if not hmac.compare_digest(str(code), self._code):
                self._attempts_remaining -= 1
                if self._attempts_remaining <= 0:
                    self._clear_offer_locked()
                    raise PairingRateLimitedError("Pairing attempts were exhausted.")
                raise InvalidPairingCodeError("The pairing code is incorrect.")
            credential = self._credential_factory()
            if not isinstance(credential, str) or len(credential) < 20:
                raise RuntimeError("Credential generator returned a weak credential.")
            verifier = _verifier(credential)
            if self._store is not None:
                self._store.save(verifier)
            self._verifier = verifier
            self._revocation_pending = False
            self._clear_offer_locked()
            return credential

    def authorize(self, credential: str | None) -> None:
        with self._lock:
            if self._verifier is None:
                raise UnpairedError("This device is not paired.")
            if not isinstance(credential, str):
                raise AuthenticationError("This device credential is missing.")
            if not hmac.compare_digest(_verifier(credential), self._verifier):
                raise AuthenticationError("This device credential is invalid.")

    def forget(self) -> None:
        with self._lock:
            self._verifier = None
            self._clear_offer_locked()
            if self._store is not None:
                try:
                    self._store.clear()
                except OSError as exc:
                    self._revocation_pending = True
                    raise CredentialPersistenceError(
                        "The device is revoked for this run, but its remembered "
                        "credential could not be removed. Retry before restarting."
                    ) from exc
            self._revocation_pending = False

    def _clear_offer_locked(self) -> None:
        self._code = None
        self._expires_at = 0.0
        self._attempts_remaining = 0
