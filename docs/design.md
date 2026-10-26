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
4. installs the export companion (`_exporter.py`, `_exporter.bat`) into the
   metadata folder, replacing older copies; a failure is logged, not fatal;
5. lists the manga folder names in the library (their contents are scanned
   only when a manga is opened);
6. starts the HTTP listener with a public `/health` readiness endpoint; and
7. serves until Windows or the operator stops it.

An invalid configuration, unsafe metadata path, failed scan, or occupied port
is fatal. The process exits nonzero so Task
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
controller token. Every library, manga-open, reader, image, selection, and rescan
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

- Read loads and writes only `<manga>.read.json`.
- Edit loads and writes only `<manga>.edit.json` and exposes selection fields.
- Selection calls bound to Read are rejected before editing metadata is opened.
- Opaque IDs cannot be substituted across manga or folders.

Position and selection responses are returned only after the corresponding
atomic metadata write succeeds. The browser drains confirmed queued writes
before changing activities, opening a manga, or rescanning. The controller
token is held across each durable mutation or scan, so takeover
cannot split one of those operations midway through its commit.

## Scanning

Scanning happens in two steps so that the home screen stays fast regardless of
library size:

- **Library list.** At startup and on the top-right Refresh action, the server
  lists only the folder names directly inside the library. It skips the
  `.pocket-manga-editor` folder, Windows hidden or system folders, and links or
  junctions (which are logged). No manga folder is opened. Each name gets an
  opaque manga ID, and a Refresh issues new IDs.
- **Open manga.** Tapping a manga scans that one manga: its direct entry
  folders and their direct JPG/PNG files, in natural order. Entries, links, and
  file types are classified from the directory listing itself, so a large
  entry costs one listing rather than several filesystem calls per image. The
  server keeps only this one manga's scan, with fresh opaque entry and image
  IDs; opening any manga replaces it and retires the previous IDs. Choosing
  Read or Edit reuses the scan from the tap. A manga with no entry folders, or
  with entry folders but no JPG/PNG images, is reported instead of opened. The
  server log records each scan's entry and image counts and duration.

A request carrying a retired ID receives `stale_snapshot`; the browser then
drops unconfirmed writes and returns to the library. A file removed after a
scan still fails live validation before it is served or written about, rather
than being served from a stale path. A file added while a manga is open appears
the next time that manga is opened from the home screen.

## Metadata files

All app metadata is flat in `<library>\.pocket-manga-editor`. Each manga has at
most `<manga>.read.json` (latest Read entry/image) and `<manga>.edit.json`
(latest Edit entry/image plus selected image names per entry). A file appears
only after its activity first saves something.

Each write changes only what the user touched: a position save replaces
`last_folder` and `last_image`, and a selection save adds or removes one image
name in one entry. Everything else in the file, including selections whose
image or entry is currently missing from disk, is written back unchanged.
Missing selections are hidden in the reader and reported by the export
companion; only a deselection or a manual edit removes them. When the saved
position no longer exists, the reader starts at the first image of the saved
entry, or at the first image of the first entry if that entry is gone; the
file keeps the old position until the next page turn. The files carry no schema
version; unexpected keys, unsafe names, or unreadable JSON fail closed and are
left untouched. Writes go through a same-folder temporary file and an atomic
replace, so a reader never sees a half-written file.

## Export companion

Export is not part of the web app. It is a standard-library Python script with a
double-clickable `.bat` launcher, developed in `pocket_manga_editor/companion/`
and copied into the metadata folder at every server start. It locates the
library as the parent of its own folder, so it needs no configuration.

The script lists manga whose edit file has selections and exports the one you
choose. It checks that every selected image is still a regular, non-link file
in its entry folder; if any are missing it stops before changing anything.
Otherwise it copies the images into `<manga>.export-tmp`, renames any existing
`<manga>.export` to `<manga>.export-old`, renames the temporary folder into
place, and deletes the old one. Windows cannot rename onto an existing folder,
which is why the previous export is moved aside first.

The export is disposable output rebuilt entirely from the edit file and the
source images, so there is no journal or recovery: an interrupted run leaves at
most a `-tmp` or `-old` folder, which the next run deletes before starting.
Source images are opened read-only. The script reads edit files without taking
the server's lock; atomic writes make that safe while the server runs, and
selection saves are never blocked by an export.

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
and wait for HTTP readiness. Finish active saves first.
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

New or removed chapters and images appear when you return to **Your Library**
and open the manga again. New, removed, or renamed manga folders need the
top-right Refresh action. A browser reload alone is not a substitute for either
server scan.

### Export companion is missing or outdated

The server reinstalls `_exporter.py` and `_exporter.bat` at every start. If they
are missing, check `logs\server.log` for an installation warning, then restart
the server.

## Code layout

`app.py` assembles startup and shutdown; `config.py` validates frozen `Settings`;
`env.py` parses literal settings; `server.py` handles HTTP transport; `api.py`
validates routes and translates errors; `service.py` coordinates the library;
`controller.py` holds the in-memory page owner. `static/` contains the no-build
browser interface. `scanner.py` lists the library and scans one manga;
`snapshot.py` maps them to opaque IDs; `review.py` serves Read and Edit for the
open manga.
`storage.py`, `workspace.py`, `path_safety.py`, and `library_lock.py` keep the
metadata files safe. `companion_install.py` copies the export companion from
`companion/` into the metadata folder.
`process_lock.py`, `errors.py`, and `logging_setup.py` follow the same small
conventions as the other Pocket repositories without a shared runtime package.

Configuration is UTF-8, optionally with a BOM. Matching outer quotes are
removed; backslashes and `#` inside values remain literal. Duplicate assignments
and malformed values are errors. Process environment variables override `.env`.
