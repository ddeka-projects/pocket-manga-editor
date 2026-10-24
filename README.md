# Pocket Manga Editor

Pocket Manga Editor is a local web application for reading manga and selecting
specific pages to copy into an output folder. A small Python server runs on the
Windows PC that owns the files; the same full-screen web interface works from an
iPhone Home Screen app or a browser on the PC.

There is no desktop GUI, account, cloud service, or permanent phone pairing.
Any device on the trusted private LAN can open the app, and one browser page at
a time owns an explicit controller token.

## Source library

The configured working directory is the library. Each direct child is a manga,
each direct child of a manga containing supported images is an entry, and each
entry contains direct JPG or PNG files:

```text
C:\Manga Library\
├── Kimi wa 08\
│   ├── V1_C01 - First Day\
│   │   ├── 1.jpg
│   │   ├── 2.png
│   │   └── 10.jpg
│   ├── Volume Two - Ch 12\
│   │   ├── page 1.png
│   │   └── page 2.png
│   └── Bonus artwork\
│       └── extra-cover.JPG
└── Another Manga\
    └── Chapter 1\
        └── 001.jpg
```

- Exact manga, entry, and image names are preserved.
- Ordering is case-insensitive and natural, so `1.jpg`, `2.jpg`, and `10.jpg`
  appear in that order.
- `.jpg` and `.png` extensions are supported case-insensitively.
- Nested folders, images directly inside a manga, `.jpeg`, archives, PDFs,
  symbolic links, junctions, and other file types are not included.
- Renaming a folder or image creates a new identity; stale saved references are
  ignored rather than inferred as renames.

## First-time Windows setup

Python 3.11 or newer is required. Open PowerShell in the repository and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-windows.ps1
```

The script creates `.venv`, installs the current requirements, and creates a
local `.env` from `.env.example` when needed. Edit `.env` and set the required
absolute path:

```env
POCKET_MANGA_EDITOR_WORKING_DIRECTORY=C:\Manga Library
POCKET_MANGA_EDITOR_HOST=0.0.0.0
POCKET_MANGA_EDITOR_PORT=8765
```

The working directory must already exist and cannot be a symbolic link or
junction. Do not use `~` or a relative path. Prefer a local disk that is
available as soon as you sign in. Real Windows environment variables override
values from `.env`.

For a manual foreground run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-dev.ps1
```

Server output is written to `logs\server.log` (5 MiB per file, with three
rotated backups). Open `http://127.0.0.1:8765/` on the PC. From another trusted
LAN device, use the PC's stable private IPv4 address,
for example `http://192.168.1.20:8765/`.

## Start automatically at your logon

Complete setup and verify `.env`, then run from ordinary PowerShell in the repository:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-startup-task.ps1
```

This installs and immediately starts **Pocket Manga Editor** at your user logon
with limited privileges, a console-free Python process, no runtime limit,
and restart after failure. A second server using the same library is refused,
even when launched manually on another port. The PC must remain signed in and awake.

For first-time phone access or after changing the port, run the following from
**PowerShell as Administrator using the same Windows account**:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-startup-task.ps1 -ConfigureFirewall
```

This allows the configured TCP port only from `LocalSubnet` on the **Private**
firewall profile. Without `-ConfigureFirewall`, the installer leaves firewall
rules untouched. The server always runs with limited privileges.

Reserve the PC's address in the router's DHCP settings so the iPhone Home
Screen URL does not change. Keep the Windows network profile set to Private.
Do not configure router port forwarding or publish the server through a VPN or
internet tunnel.

To remove automatic startup from ordinary PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\remove-startup-task.ps1
# Optional, from elevated PowerShell: also remove this app's firewall rule.
powershell -ExecutionPolicy Bypass -File .\scripts\remove-startup-task.ps1 -RemoveFirewall
```

Removal stops and unregisters the task. Firewall rules stay in place unless
`-RemoveFirewall` is supplied. It keeps `.env`, `.venv`, logs, source manga,
metadata, and output.

After updating the code, rerun `bootstrap-windows.ps1` to apply any future
requirement changes, then double-click `restart-server.cmd` in the repository
root. It stops and waits for **Pocket Manga Editor**, starts it again, and
checks `/health`. It asks
for administrator access only if the task permissions require it and keeps
the result visible. Finish active saves and exports before restarting.
Reinstall the task after changing setup scripts or moving the repository.

## Web application

The home screen lists the current library. Its top-right button performs a real
filesystem rescan. Selecting a manga presents three actions:

- **Read** — selection-free reading with an independent saved position.
- **Edit** — reading plus page selection controls and a saved editing position.
- **Export** — replace that manga's output with all currently selected pages.

Read and Edit each remember one latest entry/image pair per manga. Choosing an
entry from the picker starts at its first image. Crossing forward into the next
entry starts at its first image; crossing backward into the previous entry
starts at its last image.

The web reader preserves the immediate navigation behavior: it displays the
prefetched neighboring image without a decorative transition. In Edit, the
full-width strip along the bottom of the screen is tinted light green; tapping it
toggles selection. The tint stays visible even when the controls are hidden and
is faint enough that a long page remains readable through it. Tapping the center
shows or hides the controls. Read has no selection strip.

Only one browser page can control the library at a time. Closing or navigating
away does not depend on an unreliable browser shutdown signal: ownership stays
with that page until another page chooses **Take Control** or the server
restarts. **Release Control** on the library screen gives up ownership
deliberately. Other pages show a waiting screen until they take control.
Takeover is immediate after any save or export already being committed
finishes. There is no heartbeat timeout, paired-device credential, or permanent
ownership file; the trusted private LAN is the authorization boundary.

A brief Wi-Fi interruption does not close the reader or surrender control. The
browser keeps the current screen and retries quietly. Repeating these writes is
safe because they set an explicit value and are idempotent:

- **Reading position** is best-effort. A missed save is retried in the
  background and never shows a message; the next page turn saves the newer
  position anyway. Leaving the entry does not wait for it, so after a real
  outage the app may reopen a page or two earlier than where you stopped.
- **Page selection** stays at `…` while it is retried for a few seconds. Only if
  it still cannot be confirmed does **Connection interrupted** appear; tap
  **Retry** once the PC is reachable. Leaving the view, rescanning, and export
  still wait until every selection is confirmed.

If control moved to another page or the library changed, that is reported
immediately as before. The old page must explicitly take control back; the app
then refreshes authoritative state instead of blindly replaying uncertain
writes.

See [Application design](docs/design.md) for operational and
security details.

## App-managed workspace

The server creates one isolated workspace per manga inside the library:

```text
C:\Manga Library\.pocket-manga-editor\
└── Kimi wa 08\
    ├── reading.json
    ├── editing.json
    ├── output\
    │   └── V1_C01 - First Day\
    │       └── V1_C01 - First Day__2.png
    └── .transactions\
```

`reading.json` stores only the latest Read entry/image pair. `editing.json`
stores only the latest Edit pair and sparse exact selections. Export history and
per-file export digests are not retained. `.transactions` contains temporary
crash-recovery state only while an export is being committed or cleaned up.

Everything under `.pocket-manga-editor` is application-managed. Do not edit it
or add files that need to be preserved. The user may move a completed `output`
folder elsewhere and may manually delete source or workspace folders.

For a backup, stop the server and copy the entire `.pocket-manga-editor`
folder to a location outside the library, together with `.env`. To restore,
stop the server and restore that folder into the same library. Source images
need their own backup; Git does not contain them or your workspace. Export
transaction recovery protects interrupted replacements; it is not a backup.
Malformed reading or editing metadata produces an error and remains untouched.
Restore a known-good copy before continuing; stale positions referring to
removed images instead return to the first available image with a warning.

There is intentionally no **Complete Manga** operation. Export does not delete
source files or clear reading/selections.

## Whole-manga export

Export builds a fresh output from the complete current selection and then
transactionally replaces the previous output:

- zero selections are refused without changing existing output;
- every selected image is copied into a newly staged tree;
- the previous output remains recoverable until the new output commits;
- a failed or interrupted pre-commit operation restores the previous output;
- a subsequent server start completes transaction recovery before scanning;
- deselected images and entries disappear because the old output is replaced;
  and
- exported bytes and extensions are unchanged.

If existing output contains an unknown folder, filename, nesting level, special
file, or file type that does not correspond to the current source structure,
the app warns before replacement. Confirmation deletes the entire old output,
including that unrecognized content. A manually added file that exactly mimics
a valid source-derived output path is inherently indistinguishable from an
app-created file.

Exported filenames remain `<exact entry name>__<exact image filename>` inside
the corresponding exact entry folder.

## Development and tests

The runtime uses only the Python standard library. Run the application directly
from an activated environment with:

```powershell
.venv\Scripts\python -m pocket_manga_editor
```

Run the test suite with:

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

Install `requirements-dev.txt` for development tools. Check the common Python
format with `python -m black --check pocket_manga_editor scripts tests` from
the activated virtual environment. PowerShell lifecycle checks use `powershell`
or `pwsh` when available; real Task Scheduler, UAC, and iPhone behavior still
need verification on Windows and the phone.

The suite covers scanning and ordering, reading/editing persistence, safe path
handling, whole-output export and crash recovery, token ownership, HTTP/API
security, image delivery, and web-client behavior. Optional JavaScript behavior
checks use Node.js on Windows or JavaScriptCore on macOS; the app itself requires
neither runtime.
