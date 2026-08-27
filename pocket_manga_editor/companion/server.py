"""Dedicated-thread stdlib HTTP server for the local web application."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import logging
import mimetypes
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from ..path_safety import is_link_or_reparse
from .api import (
    APIResponse,
    CompanionAPI,
    MAX_JSON_BODY,
    RequestError,
    StreamingBody,
)
from .coordinator import CompanionCoordinator


LOGGER = logging.getLogger(__name__)


DEFAULT_PORT = 8765
MAX_HTTP_WORKERS = 16
_CLIENT_PROTOCOL_ASSET_VERSION = "always-on-web-v1"
_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_ASSET_NAMES = frozenset(
    {
        "index.html",
        "styles.css",
        "app.js",
        "manifest.webmanifest",
        "icon.svg",
        "icon-180.png",
        "icon-512.png",
    }
)


@dataclass(frozen=True, slots=True)
class HTTPServiceStatus:
    running: bool
    host: str
    port: int
    public_host: str
    url: str
    error: str | None
    lan_address_available: bool


class _CompanionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    request_queue_size = MAX_HTTP_WORKERS

    def __init__(self, server_address: tuple[str, int], handler) -> None:
        self._worker_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        self._worker_condition = threading.Condition()
        self._active_workers = 0
        self._stopping = threading.Event()
        super().__init__(server_address, handler, bind_and_activate=False)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def begin_shutdown(self) -> None:
        self._stopping.set()

    def process_request(self, request, client_address) -> None:
        if self.stopping or not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._worker_condition:
            self._active_workers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_finished()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_finished()

    def wait_for_handlers(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._worker_condition:
            while self._active_workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._worker_condition.wait(remaining)
            return True

    def _worker_finished(self) -> None:
        self._worker_slots.release()
        with self._worker_condition:
            self._active_workers -= 1
            self._worker_condition.notify_all()

    def handle_error(self, request: object, client_address: object) -> None:
        """Log failures without recording request headers or controller IDs."""

        address = "unknown"
        if isinstance(client_address, tuple) and client_address:
            address = str(client_address[0])
        LOGGER.exception("Unhandled HTTP worker failure from %s.", address)


class CompanionHTTPService:
    """Own one stable HTTP listener without depending on Qt or asyncio."""

    def __init__(
        self,
        coordinator: CompanionCoordinator,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        public_host: str | None = None,
        assets_directory: str | Path | None = None,
        allowed_hosts: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._lock = threading.RLock()
        self._host = self._validate_host(host, allow_wildcard=True)
        self._port = self._validate_port(port)
        self._configured_public_host = self._validate_public_host(public_host)
        self._assets = (
            Path(assets_directory)
            if assets_directory is not None
            else Path(__file__).with_name("assets")
        )
        self._extra_allowed_hosts = set(allowed_hosts or ())
        self._server: _CompanionHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._public_host, self._lan_available = self._choose_public_host()

    def start(self) -> HTTPServiceStatus:
        with self._lock:
            if self._server is not None and self._thread is not None and self._thread.is_alive():
                return self._status_locked()
            self._public_host, self._lan_available = self._choose_public_host()
            allowed = set(self._extra_allowed_hosts)
            allowed.update({self._public_host, self._host})
            api = CompanionAPI(self.coordinator, allowed_hosts=allowed)
            handler = self._handler(api)
            try:
                server = _CompanionHTTPServer((self._host, self._port), handler)
            except OSError as exc:
                self._server = None
                self._thread = None
                self._error = f"Could not listen on {self._host}:{self._port}: {exc}"
                return self._status_locked()
            thread = threading.Thread(
                target=self._serve,
                args=(server,),
                name="PocketMangaHTTP",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._error = None
            try:
                thread.start()
            except RuntimeError as exc:
                self._server = None
                self._thread = None
                server.server_close()
                self._error = f"Could not start the Pocket Manga HTTP thread: {exc}"
            return self._status_locked()

    def stop(self, *, timeout: float = 5.0) -> HTTPServiceStatus:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            begin_shutdown = getattr(server, "begin_shutdown", None)
            if callable(begin_shutdown):
                begin_shutdown()
            deadline = time.monotonic() + max(timeout, 0.0)
            server.shutdown()
            wait_for_handlers = getattr(server, "wait_for_handlers", None)
            drained = (
                wait_for_handlers(max(0.0, deadline - time.monotonic()))
                if callable(wait_for_handlers)
                else True
            )
            server.server_close()
        else:
            deadline = time.monotonic() + max(timeout, 0.0)
            drained = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            if not drained or (thread is not None and thread.is_alive()):
                self._error = "Pocket Manga HTTP requests did not stop in time."
            return self._status_locked()

    def restart(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        public_host: str | None = None,
    ) -> HTTPServiceStatus:
        """Stop, optionally reconfigure, and start the listener.

        Pass an empty ``public_host`` to return to automatic LAN discovery.
        """

        validated_host = (
            self._validate_host(host, allow_wildcard=True) if host is not None else None
        )
        validated_port = self._validate_port(port) if port is not None else None
        validated_public = (
            self._validate_public_host(public_host or None)
            if public_host is not None
            else None
        )
        self.stop()
        with self._lock:
            if validated_host is not None:
                self._host = validated_host
            if validated_port is not None:
                self._port = validated_port
            if public_host is not None:
                self._configured_public_host = validated_public
            self._public_host, self._lan_available = self._choose_public_host()
        return self.start()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def url(self) -> str:
        with self._lock:
            return self._url_locked()

    def status(self) -> HTTPServiceStatus:
        with self._lock:
            return self._status_locked()

    def _serve(self, server: _CompanionHTTPServer) -> None:
        try:
            server.serve_forever(poll_interval=0.25)
        except OSError as exc:
            LOGGER.exception("Pocket Manga HTTP listener stopped with an OS error.")
            with self._lock:
                if self._server is server:
                    self._error = f"The Pocket Manga HTTP server stopped: {exc}"
        except Exception:
            LOGGER.exception("Pocket Manga HTTP listener stopped unexpectedly.")
            with self._lock:
                if self._server is server:
                    self._error = "The Pocket Manga HTTP server stopped unexpectedly."
        finally:
            try:
                server.server_close()
            except OSError:
                pass
            with self._lock:
                if self._server is server:
                    self._server = None

    def _handler(self, api: CompanionAPI) -> type[BaseHTTPRequestHandler]:
        assets = self._assets

        class Handler(BaseHTTPRequestHandler):
            server_version = "PocketManga"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(2.5)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                if self._reject_if_stopping(api) or self._reject_if_nonlocal(api):
                    return
                if urlsplit(self.path).path.startswith("/api/"):
                    self._send_api(api.handle("GET", self.path, self.headers))
                    return
                try:
                    api.validate_request(self.headers, mutating=False)
                    response = self._static_response(assets)
                except RequestError as exc:
                    response = api._exception_response(exc)
                self._send_api(response)

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch_mutation(api, "POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._reject_method(api)

            def do_PATCH(self) -> None:  # noqa: N802
                self._dispatch_mutation(api, "PATCH")

            def do_HEAD(self) -> None:  # noqa: N802
                if self._reject_if_stopping(api) or self._reject_if_nonlocal(api):
                    return
                if urlsplit(self.path).path.startswith("/api/"):
                    self._send_api(
                        api._error(405, "method_not_allowed", "This method is not allowed."),
                        head=True,
                    )
                    return
                try:
                    api.validate_request(self.headers, mutating=False)
                    response = self._static_response(assets)
                except RequestError as exc:
                    response = api._exception_response(exc)
                self._send_api(response, head=True)

            def do_DELETE(self) -> None:  # noqa: N802
                self._reject_method(api)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._reject_method(api)

            def _reject_method(self, api: CompanionAPI) -> None:
                self.close_connection = True
                if self._reject_if_nonlocal(api):
                    return
                try:
                    api.validate_request(self.headers, mutating=True)
                    response = api._error(
                        405, "method_not_allowed", "This method is not allowed."
                    )
                except RequestError as exc:
                    response = api._exception_response(exc)
                self._send_api(response)

            def _reject_if_stopping(self, api: CompanionAPI) -> bool:
                if not bool(getattr(self.server, "stopping", False)):
                    return False
                self.close_connection = True
                self._send_api(
                    api._error(
                        503,
                        "server_shutting_down",
                        "The Pocket Manga server is shutting down.",
                    )
                )
                return True

            def _dispatch_mutation(self, api: CompanionAPI, method: str) -> None:
                if self._reject_if_stopping(api) or self._reject_if_nonlocal(api):
                    return
                raw_length = self.headers.get("Content-Length", "")
                try:
                    length = int(raw_length)
                except ValueError:
                    self.close_connection = True
                    self._send_api(
                        api._error(400, "bad_request", "Content-Length is invalid.")
                    )
                    return
                if length < 0 or length > MAX_JSON_BODY:
                    self.close_connection = True
                    self._send_api(
                        api._error(413, "request_too_large", "The request body is too large.")
                    )
                    return
                body = self.rfile.read(length)
                if self._reject_if_stopping(api):
                    return
                self._send_api(api.handle(method, self.path, self.headers, body))

            def _reject_if_nonlocal(self, api: CompanionAPI) -> bool:
                try:
                    api.validate_peer(str(self.client_address[0]))
                except RequestError as exc:
                    self.close_connection = True
                    self._send_api(api._exception_response(exc))
                    return True
                return False

            def _static_response(self, assets_root: Path) -> APIResponse:
                path = urlsplit(self.path).path
                name: str | None = None
                if path in {"/", "/index.html"}:
                    name = "index.html"
                elif path == "/manifest.webmanifest":
                    name = "manifest.webmanifest"
                elif path in {"/icon.svg", "/icon-180.png", "/icon-512.png"}:
                    name = path[1:]
                elif path.startswith("/assets/"):
                    candidate = path.removeprefix("/assets/")
                    if "/" not in candidate and candidate in _ASSET_NAMES:
                        name = candidate
                if name is None or name not in _ASSET_NAMES:
                    return api._error(404, "not_found", "The requested asset does not exist.")
                source = assets_root / name
                if is_link_or_reparse(source) or not source.is_file():
                    if name == "index.html":
                        body = (
                            b"<!doctype html><html><head><meta charset=utf-8>"
                            b"<meta name=viewport content='width=device-width,initial-scale=1'>"
                            b"<title>Pocket Manga</title></head>"
                            b"<body><h1>Pocket Manga</h1>"
                            b"<p>The web application assets are unavailable.</p></body></html>"
                        )
                        return APIResponse(
                            200,
                            (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))),
                            body,
                        )
                    return api._error(404, "not_found", "The requested asset does not exist.")
                try:
                    body = source.read_bytes()
                except OSError:
                    return api._error(500, "asset_error", "The asset could not be read.")
                if name == "index.html":
                    body = body.replace(
                        b'src="/assets/app.js"',
                        (
                            b'src="/assets/app.js?v='
                            + _CLIENT_PROTOCOL_ASSET_VERSION.encode("ascii")
                            + b'"'
                        ),
                        1,
                    )
                    body = body.replace(
                        b'href="/assets/styles.css"',
                        (
                            b'href="/assets/styles.css?v='
                            + _CLIENT_PROTOCOL_ASSET_VERSION.encode("ascii")
                            + b'"'
                        ),
                        1,
                    )
                content_type = {
                    ".webmanifest": "application/manifest+json",
                    ".svg": "image/svg+xml",
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                }.get(source.suffix.casefold()) or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                return APIResponse(
                    200,
                    (
                        ("Content-Type", content_type),
                        ("Content-Length", str(len(body))),
                        (
                            "Cache-Control",
                            "no-cache"
                            if name in {"index.html", "app.js", "styles.css"}
                            else "public, max-age=3600",
                        ),
                    ),
                    body,
                )

            def _send_api(self, response: APIResponse, *, head: bool = False) -> None:
                streaming = response.body if isinstance(response.body, StreamingBody) else None
                try:
                    self.send_response(response.status)
                    seen = {name.casefold() for name, _value in response.headers}
                    for name, value in response.headers:
                        self.send_header(name, value)
                    if "content-length" not in seen:
                        length = (
                            streaming.length
                            if streaming is not None
                            else len(response.body)
                        )
                        self.send_header("Content-Length", str(length))
                    self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("X-Frame-Options", "DENY")
                    self.end_headers()
                    if not head:
                        if streaming is None:
                            if response.body:
                                self.wfile.write(response.body)
                        else:
                            remaining = streaming.length
                            while remaining:
                                chunk = streaming.stream.read(
                                    min(remaining, 256 * 1024)
                                )
                                if not chunk:
                                    self.close_connection = True
                                    return
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                except OSError:
                    self.close_connection = True
                    return
                finally:
                    if streaming is not None:
                        streaming.close()

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def _status_locked(self) -> HTTPServiceStatus:
        running = (
            self._server is not None
            and self._thread is not None
            and self._thread.is_alive()
        )
        return HTTPServiceStatus(
            running,
            self._host,
            self._port,
            self._public_host,
            self._url_locked(),
            self._error,
            self._lan_available,
        )

    def _url_locked(self) -> str:
        display = self._public_host
        try:
            if ipaddress.ip_address(display).version == 6:
                display = f"[{display}]"
        except ValueError:
            pass
        return f"http://{display}:{self._port}/"

    def _choose_public_host(self) -> tuple[str, bool]:
        if self._configured_public_host:
            return self._configured_public_host, self._configured_public_host not in {"127.0.0.1", "::1", "localhost"}
        if self._host not in {"0.0.0.0", "::", "127.0.0.1", "::1", "localhost"}:
            return self._host, True
        discovered = _discover_lan_ipv4()
        if discovered is not None:
            return discovered, True
        return "127.0.0.1", False

    @staticmethod
    def _validate_port(port: int) -> int:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("The server port must be between 1 and 65535.")
        return port

    @staticmethod
    def _validate_host(host: str, *, allow_wildcard: bool) -> str:
        if not isinstance(host, str) or not host or any(character.isspace() for character in host):
            raise ValueError("The server host is invalid.")
        value = host.strip().rstrip(".")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if not _HOSTNAME.fullmatch(value):
                raise ValueError("The server host must be a hostname or IP address.")
        else:
            if address.version == 6:
                raise ValueError(
                    "Pocket Manga currently requires an IPv4 bind/display address."
                )
            if not allow_wildcard and address.is_unspecified:
                raise ValueError("The displayed server host cannot be a wildcard address.")
        return value

    @classmethod
    def _validate_public_host(cls, host: str | None) -> str | None:
        if host is None:
            return None
        return cls._validate_host(host, allow_wildcard=False)


def _discover_lan_ipv4() -> str | None:
    """Discover a usable local address without sending application traffic."""

    candidates: list[str] = []
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 9))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for value in candidates:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and address.is_private and not address.is_loopback and not address.is_unspecified:
            return str(address)
    return None
