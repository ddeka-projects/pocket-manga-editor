"""Framework-independent HTTP API dispatch and request security policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
from typing import BinaryIO, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from .auth import (
    AuthenticationError,
    InvalidPairingCodeError,
    PairingClosedError,
    PairingRateLimitedError,
)
from .coordinator import CompanionCoordinator
from .lease import LeaseConflictError, LeaseError, LeaseExpiredError
from .review import (
    PositionMutation,
    ReviewError,
    ReviewLoadError,
    ReviewSaveError,
    SelectionMutation,
)
from .snapshot import MissingImageError, SnapshotError
from .state import (
    CompanionActivity,
    CompanionState,
    CompanionStateError,
    ShutdownTransitionError,
)


COOKIE_NAME = "pme_device"
MAX_JSON_BODY = 16 * 1024


@dataclass(frozen=True, slots=True)
class APIResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes | StreamingBody

    def read_body(self) -> bytes:
        """Materialize a response for framework-independent tests or adapters."""

        if isinstance(self.body, bytes):
            return self.body
        try:
            remaining = self.body.length
            chunks: list[bytes] = []
            while remaining:
                chunk = self.body.stream.read(min(remaining, 256 * 1024))
                if not chunk:
                    raise OSError("The streamed response ended before its declared length.")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            self.body.close()


@dataclass(slots=True)
class StreamingBody:
    """An already-open, validated file body owned by the response consumer."""

    stream: BinaryIO
    length: int

    def close(self) -> None:
        self.stream.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass


class RequestError(RuntimeError):
    code = "bad_request"
    http_status = 400


class RequestForbidden(RequestError):
    code = "forbidden_request"
    http_status = 403


class UnsupportedContentType(RequestError):
    code = "unsupported_media_type"
    http_status = 415


class CompanionAPI:
    def __init__(
        self,
        coordinator: CompanionCoordinator,
        *,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._allowed_hosts = {
            value.casefold().rstrip(".") for value in (allowed_hosts or ()) if value
        }
        self._allowed_hosts.update({"localhost", "127.0.0.1", "::1"})
        try:
            self._allowed_hosts.add(socket.gethostname().casefold().rstrip("."))
            self._allowed_hosts.add(socket.getfqdn().casefold().rstrip("."))
        except OSError:
            pass

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> APIResponse:
        try:
            method = method.upper()
            self.validate_request(headers, mutating=method in {"POST", "PUT", "PATCH", "DELETE"})
            parsed_target = urlsplit(target)
            path = parsed_target.path
            segments = self._segments(path)

            if method == "GET" and segments == ["api", "status"]:
                return self._json(200, {"status": self._public_status()})

            if method == "POST" and segments == ["api", "pair"]:
                payload = self._json_body(headers, body)
                self._require_keys(payload, {"code"})
                code = self._required_string(payload, "code", maximum=32)
                credential = self.coordinator.pair(code)
                cookie = (
                    f"{COOKIE_NAME}={credential}; Path=/; HttpOnly; "
                    "SameSite=Strict; Max-Age=31536000"
                )
                return self._json(
                    200, {"paired": True}, extra_headers=(("Set-Cookie", cookie),)
                )

            credential = self._credential(headers)

            if method == "POST" and segments == ["api", "controller", "claim"]:
                payload = self._json_body(headers, body)
                self._require_keys(payload, {"client_id", "page_id"})
                client_id = self._client_id_from_payload(payload)
                page_instance_id = self._page_instance_id_from_payload(payload)
                lease = self.coordinator.claim_controller(
                    credential, client_id, page_instance_id
                )
                return self._json(
                    200,
                    {
                        "controller": {
                            "claimed": True,
                            "client_id": client_id,
                            "page_id": page_instance_id,
                            "lease_expires_at": lease.lease_expires_at,
                        },
                        "snapshot_id": self.coordinator.status().snapshot_id,
                    },
                )

            if method == "POST" and segments == ["api", "controller", "heartbeat"]:
                payload = self._json_body(headers, body)
                self._require_keys(payload, {"client_id", "page_id"})
                client_id = self._client_id_from_payload(payload)
                page_instance_id = self._page_instance_id_from_payload(payload)
                lease = self.coordinator.heartbeat_controller(
                    credential, client_id, page_instance_id
                )
                return self._json(
                    200,
                    {
                        "controller": {
                            "claimed": True,
                            "client_id": client_id,
                            "page_id": page_instance_id,
                            "lease_expires_at": lease.lease_expires_at,
                        },
                        "snapshot_id": self.coordinator.status().snapshot_id,
                    },
                )

            if method == "POST" and segments == ["api", "controller", "release"]:
                payload = self._json_body(headers, body)
                self._require_keys(payload, {"client_id", "page_id"})
                client_id = self._client_id_from_payload(payload)
                page_instance_id = self._page_instance_id_from_payload(payload)
                self.coordinator.release_controller(
                    credential, client_id, page_instance_id
                )
                return self._json(
                    200,
                    {
                        "released": True,
                        "client_id": client_id,
                        "page_id": page_instance_id,
                    },
                )

            is_library = method == "GET" and segments == ["api", "library"]
            is_manga = (
                method == "GET"
                and len(segments) == 3
                and segments[:2] == ["api", "manga"]
            )
            is_folder = (
                method == "GET"
                and len(segments) == 3
                and segments[:2] == ["api", "folder"]
            )
            is_image = (
                method == "GET"
                and len(segments) == 3
                and segments[:2] == ["api", "image"]
            )
            is_position_write = (
                method == "PUT"
                and len(segments) == 5
                and segments[0] == "api"
                and segments[1] in {"read", "edit"}
                and segments[2] == "folder"
                and segments[4] == "position"
            )
            is_selection_write = (
                method == "PUT"
                and len(segments) == 5
                and segments[:3] == ["api", "edit", "folder"]
                and segments[4] == "selection"
            )
            if not any(
                (
                    is_library,
                    is_manga,
                    is_folder,
                    is_image,
                    is_position_write,
                    is_selection_write,
                )
            ):
                return self._error(
                    404, "not_found", "The requested API route does not exist."
                )

            client_id, page_instance_id = self._controller_from_headers(headers)
            if is_library:
                return self._json(
                    200,
                    self.coordinator.library(
                        credential, client_id, page_instance_id
                    ),
                )
            if is_manga:
                activity = self._activity_query(parsed_target.query)
                return self._json(
                    200,
                    self.coordinator.open_manga(
                        credential,
                        client_id,
                        segments[2],
                        activity,
                        page_instance_id,
                    ),
                )
            if is_folder:
                activity = self._activity_query(parsed_target.query)
                return self._json(
                    200,
                    self.coordinator.folder(
                        credential,
                        client_id,
                        segments[2],
                        activity,
                        page_instance_id,
                    ),
                )
            if is_image:
                return self._image(
                    credential,
                    client_id,
                    page_instance_id,
                    segments[2],
                    headers,
                )
            if is_position_write or is_selection_write:
                payload = self._json_body(headers, body)
                self._require_keys(
                    payload,
                    {"image_id"}
                    if is_position_write
                    else {"image_id", "selected"},
                )
                image_id = self._required_string(payload, "image_id", maximum=256)
                folder_id = segments[3]
                activity = CompanionActivity(segments[1])
                if is_position_write:
                    mutation = self.coordinator.set_position(
                        credential,
                        client_id,
                        activity,
                        folder_id,
                        image_id,
                        page_instance_id,
                    )
                    return self._json(
                        200, {"position": self._position(mutation)}
                    )
                else:
                    selected = payload.get("selected")
                    if not isinstance(selected, bool):
                        raise RequestError("selected must be a boolean.")
                    mutation = self.coordinator.set_selection(
                        credential,
                        client_id,
                        activity,
                        folder_id,
                        image_id,
                        selected,
                        page_instance_id,
                    )
                    return self._json(
                        200, {"selection": self._selection(mutation)}
                    )

            return self._error(404, "not_found", "The requested API route does not exist.")
        except Exception as exc:
            return self._exception_response(exc)

    def validate_request(
        self, headers: Mapping[str, str], *, mutating: bool
    ) -> None:
        host_value = self._header(headers, "Host")
        if not host_value:
            raise RequestForbidden("A valid Host header is required.")
        host_name = self._host_name(host_value)
        if not self._host_allowed(host_name):
            raise RequestForbidden("This Host is not allowed.")
        if mutating:
            origin = self._header(headers, "Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme.casefold() != "http" or not parsed.netloc:
                    raise RequestForbidden("This request Origin is not allowed.")
                if self._canonical_authority(parsed.netloc) != self._canonical_authority(host_value):
                    raise RequestForbidden("Cross-origin state changes are not allowed.")

    def _public_status(self) -> dict[str, object]:
        status = self.coordinator.status()
        return {
            "server": "available",
            "paired": status.paired,
            "pairing_open": status.pairing_open,
            "companion_active": status.state is CompanionState.COMPANION_ACTIVE,
        }

    def _image(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str,
        image_id: str,
        headers: Mapping[str, str],
    ) -> APIResponse:
        snapshot, image = self.coordinator.image_for_delivery(
            credential, client_id, image_id, page_instance_id
        )
        source = Path(image.ref.path)
        extension = source.suffix.casefold()
        if extension not in {".jpg", ".png"}:
            raise MissingImageError("The source image type is not supported.")
        body, information = _open_validated_image(snapshot, image.id)
        mime = "image/jpeg" if extension == ".jpg" else "image/png"
        etag_value = hashlib.sha256(
            f"{image.id}:{information.st_size}:{information.st_mtime_ns}".encode("ascii")
        ).hexdigest()
        etag = f'"{etag_value}"'
        common = (
            ("ETag", etag),
            (
                "Last-Modified",
                format_datetime(
                    datetime.fromtimestamp(information.st_mtime, timezone.utc),
                    usegmt=True,
                ),
            ),
            ("Cache-Control", "private, max-age=3600, must-revalidate"),
            ("X-Content-Type-Options", "nosniff"),
        )
        if self._header(headers, "If-None-Match") == etag:
            body.close()
            return APIResponse(304, common, b"")
        return APIResponse(
            200,
            (("Content-Type", mime), ("Content-Length", str(body.length)), *common),
            body,
        )

    def _json_body(
        self, headers: Mapping[str, str], body: bytes
    ) -> dict[str, object]:
        content_type = self._header(headers, "Content-Type").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise UnsupportedContentType("State-changing requests must use application/json.")
        if len(body) > MAX_JSON_BODY:
            raise RequestError("The JSON request body is too large.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("The JSON request body is invalid.") from exc
        if not isinstance(payload, dict):
            raise RequestError("The JSON request body must be an object.")
        return payload

    @staticmethod
    def _segments(path: str) -> list[str]:
        try:
            segments = [unquote(part, errors="strict") for part in path.split("/") if part]
        except UnicodeError as exc:
            raise RequestError("The request path is invalid.") from exc
        if any(part in {".", ".."} or "/" in part or "\\" in part for part in segments):
            raise RequestError("The request path is unsafe.")
        return segments

    @staticmethod
    def _activity_query(query: str) -> CompanionActivity:
        try:
            values = parse_qsl(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
            )
        except ValueError as exc:
            raise RequestError("The activity query is invalid.") from exc
        if len(values) != 1 or values[0][0] != "activity":
            raise RequestError("Exactly one activity query is required.")
        try:
            return CompanionActivity(values[0][1])
        except ValueError as exc:
            raise RequestError("activity must be read or edit.") from exc

    def _credential(self, headers: Mapping[str, str]) -> str | None:
        raw = self._header(headers, "Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel is not None else None

    def _controller_from_headers(
        self, headers: Mapping[str, str]
    ) -> tuple[str, str]:
        client_id = self._header(headers, "X-Companion-Instance")
        if not client_id:
            raise RequestError("X-Companion-Instance is required.")
        page_instance_id = self._header(headers, "X-Companion-Page")
        if not page_instance_id:
            raise RequestError("X-Companion-Page is required.")
        return client_id, page_instance_id

    def _client_id_from_payload(self, payload: Mapping[str, object]) -> str:
        value = payload.get("client_id")
        if not isinstance(value, str) or not value:
            raise RequestError("client_id is required.")
        return value

    @staticmethod
    def _require_keys(
        payload: Mapping[str, object], expected: set[str]
    ) -> None:
        if set(payload) != expected:
            names = ", ".join(sorted(expected))
            raise RequestError(f"The JSON body must contain exactly: {names}.")

    def _page_instance_id_from_payload(
        self, payload: Mapping[str, object]
    ) -> str:
        return self._required_string(payload, "page_id", maximum=128)

    @staticmethod
    def _required_string(
        payload: Mapping[str, object], key: str, *, maximum: int
    ) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise RequestError(f"{key} is required.")
        return value

    def _host_allowed(self, hostname: str) -> bool:
        folded = hostname.casefold().rstrip(".")
        if folded in self._allowed_hosts:
            return True
        try:
            address = ipaddress.ip_address(folded)
        except ValueError:
            return False
        return (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        )

    @staticmethod
    def _host_name(authority: str) -> str:
        try:
            parsed = urlsplit(f"//{authority}")
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as exc:
            raise RequestForbidden("The Host header is invalid.") from exc
        if not hostname or parsed.username is not None or parsed.password is not None:
            raise RequestForbidden("The Host header is invalid.")
        return hostname

    @classmethod
    def _canonical_authority(cls, authority: str) -> tuple[str, int | None]:
        hostname = cls._host_name(authority).casefold().rstrip(".")
        parsed = urlsplit(f"//{authority}")
        return hostname, parsed.port

    @staticmethod
    def _header(headers: Mapping[str, str], key: str) -> str:
        target = key.casefold()
        for name, value in headers.items():
            if name.casefold() == target:
                return value.strip()
        return ""

    @staticmethod
    def _position(mutation: PositionMutation) -> dict[str, object]:
        return asdict(mutation)

    @staticmethod
    def _selection(mutation: SelectionMutation) -> dict[str, object]:
        return asdict(mutation)

    @staticmethod
    def _json(
        status: int,
        payload: dict[str, object],
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> APIResponse:
        encoded = json.dumps({"ok": True, **payload}, ensure_ascii=False).encode("utf-8")
        return APIResponse(
            status,
            (
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                *extra_headers,
            ),
            encoded,
        )

    @staticmethod
    def _error(status: int, code: str, message: str) -> APIResponse:
        encoded = json.dumps(
            {"ok": False, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        return APIResponse(
            status,
            (
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ),
            encoded,
        )

    def _exception_response(self, exc: BaseException) -> APIResponse:
        if isinstance(exc, RequestError):
            return self._error(exc.http_status, exc.code, str(exc))
        if isinstance(exc, PairingRateLimitedError):
            return self._error(429, exc.code, str(exc))
        if isinstance(exc, PairingClosedError):
            return self._error(409, exc.code, str(exc))
        if isinstance(exc, InvalidPairingCodeError):
            return self._error(401, exc.code, str(exc))
        if isinstance(exc, AuthenticationError):
            return self._error(401, exc.code, str(exc))
        if isinstance(exc, LeaseConflictError):
            return self._error(423, exc.code, str(exc))
        if isinstance(exc, LeaseExpiredError):
            return self._error(409, exc.code, str(exc))
        if isinstance(exc, LeaseError):
            return self._error(400, exc.code, str(exc))
        if isinstance(exc, SnapshotError):
            return self._error(404, exc.code, str(exc))
        if isinstance(exc, ReviewSaveError):
            return self._error(
                503,
                exc.code,
                "Review state could not be saved. Check Pocket Manga Editor on the PC.",
            )
        if isinstance(exc, ReviewLoadError):
            return self._error(409, exc.code, str(exc))
        if isinstance(exc, ReviewError):
            return self._error(400, exc.code, str(exc))
        if isinstance(exc, ShutdownTransitionError):
            return self._error(409, exc.code, str(exc))
        if isinstance(exc, CompanionStateError):
            return self._error(409, exc.code, str(exc))
        return self._error(500, "internal_error", "The Companion service could not process this request.")


def _open_validated_image(snapshot, image_id: str) -> tuple[StreamingBody, os.stat_result]:
    """Open once, then prove the handle still names the validated mapped image."""

    try:
        image = snapshot.validate_live_image(image_id)
        source = Path(image.ref.path)
        resolved_source = source.resolve(strict=True)
        before = resolved_source.stat()
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved_source, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise OSError("source changed before it could be opened")
            snapshot.validate_live_image(image_id)
            after_path = source.resolve(strict=True)
            after = after_path.stat()
            if after_path != resolved_source or not os.path.samestat(opened, after):
                raise OSError("source changed during validation")
            stream = os.fdopen(descriptor, "rb")
            descriptor = -1
            return StreamingBody(stream, opened.st_size), opened
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, ValueError, SnapshotError) as exc:
        raise MissingImageError(
            "The source image is no longer safely available."
        ) from exc
