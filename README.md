# Pocket Manga Editor

Pocket Manga Editor is a local web application for reading manga and selecting
specific pages to copy into an output folder. A small Python server runs on the
Windows PC that owns the files; the same full-screen web interface works from an
iPhone Home Screen app or a browser on the PC.

There is no desktop GUI, account, cloud service, or permanent phone pairing.
Any device on the trusted private LAN can open the app, and one browser page at
a time owns the controller lease.

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

Python 3.10 or newer is required. Open PowerShell in the repository and run:

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
junction. Do not use `~`, a relative path, or a user-mapped network drive for a
server that will run as `SYSTEM`. Real Windows environment variables override
values from `.env`.

This development release intentionally does not migrate metadata created by
the former desktop/completion builds. Before its first run, move aside any old
output you want to keep and remove the old manga workspace under
`.pocket-manga-editor`; the server will create the smaller metadata format as
you use it.

For a manual foreground run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-server.ps1
```

Server output is appended under `logs\`. Open `http://127.0.0.1:8765/` on the
PC. From another trusted LAN device, use the PC's stable private IPv4 address,
for example `http://192.168.1.20:8765/`.

## Start automatically with Windows

First complete the setup above and verify `.env`. Then open PowerShell **as
Administrator** and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-startup-task.ps1
```

The installer:

- registers **Pocket Manga Editor Server** at Windows startup;
- runs it as `SYSTEM`, whether or not a user has signed in;
- prevents duplicate task instances;
- restarts the server after failures;
- permits the configured TCP port only from `LocalSubnet` on the Windows
  **Private** firewall profile; and
- starts the task immediately.

Reserve the PC's address in the router's DHCP settings so the iPhone Home
Screen URL does not change. Keep the Windows network profile set to Private.
Do not configure router port forwarding or publish the server through a VPN or
internet tunnel.

To remove automatic startup, run as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\remove-startup-task.ps1
```

Removal stops and unregisters the task and removes its firewall rule. It does
not delete `.env`, `.venv`, logs, source manga, metadata, or output.

After updating the code, rerun `bootstrap-windows.ps1` to apply any future
requirement changes, then restart the scheduled task or run the installer again.

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
prefetched neighboring image without a decorative transition. In Edit, tapping
the lower middle area toggles selection; tapping the center shows or hides the
controls.

Only one browser page can control the library at a time. Closing or navigating
away releases ownership when the browser reports it. If that signal is lost,
the in-memory heartbeat lease expires automatically after a short timeout. A
server restart also clears ownership. There is no paired-device credential;
the trusted private LAN is the authorization boundary.

See [Local web server behavior](docs/server-mode.md) for operational and
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

The suite covers scanning and ordering, reading/editing persistence, safe path
handling, whole-output export and crash recovery, controller leasing, HTTP/API
security, image delivery, and web-client behavior.
