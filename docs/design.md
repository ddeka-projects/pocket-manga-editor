# Application design

Pocket Manga Editor has one runtime and one interface: a Python process owns the
filesystem, and browsers use the web application it serves over the trusted
private LAN. The iPhone Home Screen installation and a normal desktop browser
are equivalent clients.

## Startup lifecycle

At process startup the server:

1. loads `.env` beside the repository, with real environment variables taking
   precedence;
2. validates the absolute existing library directory and bind host/port;
3. acquires an operating-system lifetime guard for this app and library;
4. completes any interrupted export recovery work;
5. scans direct manga, entry, and image children into an immutable snapshot;
6. starts the HTTP listener with a public `/health` readiness endpoint; and
7. serves until Windows or the operator stops it.

An invalid configuration, unsafe metadata path, unrecoverable transaction,
failed scan, or occupied port is fatal. The process exits nonzero so Task
Scheduler can retry it, and diagnostic output is retained in `logs\server.log`
with bounded rotation.
The lifetime guard is outside the library and is released automatically if
the server exits or crashes. It blocks another copy even on a different port;
short mutation locks remain in place underneath it.

The scheduled task uses the script and virtual environment at their absolute
repository paths. Its current working directory is also set explicitly, so its
behavior does not depend on Task Scheduler's default directory.

## Trusted-LAN boundary

There is no account or device pairing. Any device able to reach the service on
the private local subnet may attempt to claim it. The intended protection is:

- a Windows Firewall inbound rule limited to `LocalSubnet` and the Private
  profile;
- no router port forwarding, UPnP publication, public reverse proxy, or VPN
  publication;
- host and same-origin checks in the HTTP service; and
- live path validation before image delivery or filesystem mutation.

“Same Wi-Fi” is treated as the same trusted private LAN. The PC may use Ethernet
while the phone uses Wi-Fi. Guest networks with client isolation may prevent
the phone from reaching the PC.

HTTP is intentionally unencrypted. This model is not suitable for public Wi-Fi
or internet exposure.

## Exclusive controller

The first page to open the application claims an unguessable, in-memory
controller token. Every library, reader, image, selection, rescan, and export
request must present both its page identity and token. A different tab or
device receives an “open elsewhere” response and can choose **Take Control**.

Ownership has no timer and the browser sends no heartbeat or shutdown-time
release. The library screen offers **Release Control** for an explicit release.
Ownership remains valid through phone suspension and temporary Wi-Fi loss.
An explicit takeover rotates the token immediately after any already-authorized
durable operation finishes; requests carrying the old token are then rejected.
This preserves single-controller behavior without guessing whether an absent
heartbeat means a closed page or a sleeping phone. A server restart also clears
ownership.

Ownership is deliberately not written to `.pocket-manga-editor`; a persisted
“currently open” marker could strand the app after a crash or power loss.

The browser retains its current view on a network error. Position and
selection requests set explicit values, so repeating an already-committed
request with the same token is safe. They use a 5-second request timeout and
retry quietly on network failure:

- Positions are latest-wins and best-effort. Failed saves retry in the
  background (after 1, 2, and 4 seconds) and on the next page turn, without a
  message. Draining before a view change drops a position that just failed
  rather than blocking navigation.
- A selection remains visibly pending through two quick retries. Only then is
  the connection marked interrupted, and the user retries explicitly. Draining
  still requires every selection to be confirmed.

Server answers such as a transferred controller or a stale snapshot are not
retried; they are handled immediately.

Each HTTP response is sent with `Connection: close`. Reusing an idle keep-alive
connection can race the server closing it, and browsers do not resend a failed
`PATCH` or `POST` automatically, so the app opens a fresh connection per
request. On a LAN the extra handshake is negligible. After ownership transfer, the old page
does not blindly replay uncertain writes. Taking control refreshes server state
and tells the user when an unconfirmed save needs review.

## Read and Edit activities

Choosing a manga is activity-neutral. **Read** and **Edit** bind the current page
to that manga and metadata domain before folder/image access is allowed:

- Read loads and writes only `reading.json`.
- Edit loads and writes only `editing.json` and exposes selection fields.
- Selection calls bound to Read are rejected before editing metadata is opened.
- Opaque snapshot IDs cannot be substituted across manga or folders.

Position and selection responses are returned only after the corresponding
atomic metadata write succeeds. The browser drains confirmed queued writes
before changing activities, rescanning, or starting export. The controller
token is held across each durable mutation, rescan, or export, so takeover
cannot split one of those operations midway through its commit.

## Rescan

The top-right library action is a server-side filesystem rescan, not merely an
HTTP refresh. Rescan is serialized against state writes and export. It builds a
new immutable snapshot and review service, invalidates the old opaque IDs,
clears the previous activity binding, and returns the controller to the library
screen.

Files added after startup do not appear until Rescan. Files removed after a
scan still fail live validation immediately rather than being served from a
stale path.

## Export

Export is available from the manga activity menu and uses the complete saved
Edit selection for that manga. It refuses an empty selection and leaves any
prior output unchanged.

For a non-empty selection, the server stages a complete new output tree without
modifying the active output. It verifies and synchronizes the staged bytes,
durably records the transaction, moves the prior output aside, and installs the
new tree. The prior tree is deleted only after commit. Recovery uses the journal
and tree identities to either restore the old output or finish a committed new
one without relying on export-history metadata.

Windows cannot atomically exchange two populated directories in one filesystem
call. The durable move/install protocol therefore provides transactional
all-or-prior-output behavior across ordinary errors and process interruption,
with recovery before the next scan.

The old output is wholly application-managed. Before replacement, the server
can recognize structural content that does not correspond to the current source
and require confirmation. Once confirmed, all old output is removed, including
unknown regular files. Links, junctions, mounted boundaries, and special files
remain safety errors rather than content that the application follows or adopts.

## Windows Task Scheduler

`scripts/install-startup-task.ps1` registers **Pocket Manga Editor** at the
installing user's logon, with an interactive, limited-privilege principal,
duplicate instances ignored, unlimited runtime, and retry after failure.
Normal installation and removal use ordinary PowerShell. Add `-ConfigureFirewall`
to installation or `-RemoveFirewall` to removal from an elevated PowerShell under
that same Windows account to change this app's Private/LocalSubnet firewall rule.
Otherwise firewall rules are left untouched. `scripts/run-dev.ps1` uses the
repository's `.venv` in the foreground. Scheduled startup uses a console-free
Python launcher and records diagnostics in `logs\server.log`.
Double-click `restart-server.cmd` after code updates to restart the exact task
and wait for HTTP readiness. Finish active saves and exports first.
`scripts/remove-startup-task.ps1` removes the task without touching application data.

The server is available after that user signs in, while the PC remains awake.
Prefer an always-available local absolute library path. If the repository itself
is moved, reinstall the task so its absolute action path is updated.

## Troubleshooting

### Connection refused

Confirm the task is running, inspect `logs\server.log`, check that
the PC still owns the address used by the Home Screen shortcut, and ensure the
network profile is Private. `127.0.0.1` works only on the PC itself.

### App is open elsewhere

Use **Take Control** on the waiting page. Any filesystem mutation already being
committed is allowed to finish first, then the previous page's token becomes
invalid. Stopping and starting the server also clears ownership.

### Port already in use

Stop the other listener or choose a different
`POCKET_MANGA_EDITOR_PORT`. Rerun the startup-task installer with
`-ConfigureFirewall` from elevated PowerShell so the Private-profile firewall rule
follows the configured port, and update the Home Screen URL.

### Library does not reflect filesystem changes

Return to **Your Library** and use the top-right Rescan action. A browser reload
alone is not a substitute for a server scan.

### Export recovery blocks startup or rescan

Do not manually move transaction payloads while recovery is possible. Stop
duplicate processes, inspect the exact error in the server log, and preserve the
reported manga workspace until its old/new output state can be reconciled.

## Code layout

`app.py` assembles startup and shutdown; `config.py` validates frozen `Settings`;
`env.py` parses literal settings; `server.py` handles HTTP transport; `api.py`
validates routes and translates errors; `service.py` coordinates the library;
`controller.py` holds the in-memory page owner. `static/` contains the no-build
browser interface. `snapshot.py` and `review.py` support reader operations.
`storage.py`, `workspace.py`, `path_safety.py`, `library_lock.py`, and
`exporter.py` retain the separate metadata and transactional-export guarantees.
`process_lock.py`, `errors.py`, and `logging_setup.py` follow the same small
conventions as the other Pocket repositories without a shared runtime package.

Configuration is UTF-8, optionally with a BOM. Matching outer quotes are
removed; backslashes and `#` inside values remain literal. Duplicate assignments
and malformed values are errors. Process environment variables override `.env`.
