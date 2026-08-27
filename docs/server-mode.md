# Local web server behavior

Pocket Manga Editor has one runtime and one interface: a Python process owns the
filesystem, and browsers use the web application it serves over the trusted
private LAN. The iPhone Home Screen installation and a normal desktop browser
are equivalent clients.

## Startup lifecycle

At process startup the server:

1. loads `.env` beside the repository, with real environment variables taking
   precedence;
2. validates the absolute existing library directory and bind host/port;
3. acquires and completes any interrupted export recovery work;
4. scans direct manga, entry, and image children into an immutable snapshot;
5. starts the HTTP listener; and
6. remains in the foreground until Windows or the operator stops it.

An invalid configuration, unsafe metadata path, unrecoverable transaction,
failed scan, or occupied port is fatal. The process exits nonzero so Task
Scheduler can retry it, and diagnostic output is retained under `logs\` by the
Windows launcher.

The server-first metadata schema is a deliberate development clean break. It
does not import desktop-era export history or completion state.

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
controller lease. Every library, reader, image, selection, rescan, and export
request must present that page identity. A different tab or device receives an
“open elsewhere” response.

The owning page sends periodic heartbeats. Closing or navigating away sends a
best-effort release, but browsers cannot guarantee delivery during termination
or suspension. A missed release is therefore recovered by the short lease
timeout. Reload/reconnect by the same live page can reclaim normally, and a
server restart clears all lease state.

The lease is deliberately not written to `.pocket-manga-editor`; a persisted
“currently open” marker could strand the app after a crash or power loss.

## Read and Edit activities

Choosing a manga is activity-neutral. **Read** and **Edit** bind the current page
to that manga and metadata domain before folder/image access is allowed:

- Read loads and writes only `reading.json`.
- Edit loads and writes only `editing.json` and exposes selection fields.
- Selection calls bound to Read are rejected before editing metadata is opened.
- Opaque snapshot IDs cannot be substituted across manga or folders.

Position and selection responses are returned only after the corresponding
atomic metadata write succeeds. The browser drains confirmed queued writes
before changing activities, rescanning, or starting export.

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

`scripts/install-startup-task.ps1` registers the server as `SYSTEM` at machine
startup with duplicate instances ignored, unlimited runtime, and retry after
failure. `scripts/run-server.ps1` uses the repository's `.venv` and appends
output to a dated log file. `scripts/remove-startup-task.ps1` removes the task
and firewall rule without touching user or application data.

A task running as `SYSTEM` normally cannot use drive letters mapped only in a
signed-in user session. Prefer an always-available local absolute path. If the
repository itself is moved, reinstall the task so its absolute action path is
updated.

## Troubleshooting

### Connection refused

Confirm the task is running, inspect the newest file under `logs\`, check that
the PC still owns the address used by the Home Screen shortcut, and ensure the
network profile is Private. `127.0.0.1` works only on the PC itself.

### App is open elsewhere

Close or navigate away from the owning page. If it disappeared without sending
release, wait briefly for its heartbeat lease to expire and try again. Stopping
and starting the server also clears the lease.

### Port already in use

Stop the other listener or choose a different
`POCKET_MANGA_EDITOR_PORT`. Rerun the startup-task installer afterward so the
Private-profile firewall rule follows the configured port, and update the Home
Screen URL.

### Library does not reflect filesystem changes

Return to **Your Library** and use the top-right Rescan action. A browser reload
alone is not a substitute for a server scan.

### Export recovery blocks startup or rescan

Do not manually move transaction payloads while recovery is possible. Stop
duplicate processes, inspect the exact error in the server log, and preserve the
reported manga workspace until its old/new output state can be reconciled.
