# Pocket Manga Editor

Pocket Manga Editor is a local web application for reading manga and selecting
specific pages. A small Python server runs on the Windows PC that owns the
files; the same full-screen web interface works from an iPhone Home Screen app
or a browser on the PC. Selected pages are copied into an export folder by a
double-click companion script on the PC (see
[Exporting selected pages](#exporting-selected-pages)).

There is no desktop GUI, account, cloud service, or permanent phone pairing.
Any device on the trusted private LAN can open the app, and one browser page at
a time owns an explicit controller token.

## Source library

The configured working directory is the library. Each direct child folder is a
manga, each direct child folder of a manga is an entry (a chapter, volume, part,
or anything else), and each entry contains direct JPG or PNG files:

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
- Images must be inside an entry folder. Images directly inside a manga, nested
  folders, `.jpeg`, archives, PDFs, symbolic links, junctions, and other file
  types are not included. Entry folders without JPG or PNG images are left out
  of the entry list.
- Windows hidden or system folders in the library (such as `$RECYCLE.BIN`) and
  the app's own `.pocket-manga-editor` folder are not listed as manga.
- Renaming a folder or image creates a new identity; saved references to the old
  name are kept but not inferred as renames.

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
metadata, and exports.

After updating the code, rerun `bootstrap-windows.ps1` to apply any future
requirement changes, then double-click `restart-server.cmd` in the repository
root. It stops and waits for **Pocket Manga Editor**, starts it again, and
checks `/health`. It asks
for administrator access only if the task permissions require it and keeps
the result visible. Finish active saves before restarting. The restart also
reinstalls the updated export companion.
Reinstall the task after changing setup scripts or moving the repository.

## Web application

The home and feedback screens share the **Pocket Manga Editor** heading. The
home screen offers **Recommend** and **Explore**, with **Reset Preferences**
and **Clean Up** grouped under **Maintenance**. Its footer shows the configured
manga library's absolute path on the PC. Explore shows full-width title cards, one per row, with compact
padding and heights that grow only as needed for their contents. Long names
wrap onto extra lines. An initial `[author]` prefix appears as a
smaller rounded tag before the title, displaying just the author name without
the brackets. The server
reads the library folder's names when it starts, and the top-right button
lists them again, so adding, removing, or renaming a manga folder needs a
Refresh. Listing names only is quick even for a large library, because no
chapter or image is inspected at this point.

Explore puts manga with saved **Editing** activity first, then **Reading**,
then the remaining titles. Each group keeps the case-insensitive natural name
order, ignoring the brackets around an initial author prefix: `[Kino] Night`
sorts as `Kino Night`, alongside ordinary titles. The author still comes before
the manga name in the sort key. A title with both `<manga>.edit.json` and
`<manga>.read.json` appears once, with Editing taking precedence. These labels
mean saved activity, not a live
session: they appear after the first confirmed position or selection save and
remain while that metadata file exists. Returning from the reader updates
the label and ordering without another source scan.

Search filters the entire folder name, including `[author]`, as you type,
ignoring case and surrounding spaces. The compact **All / Normal only** radio
control includes or hides 18+ titles; **All** is the default and the choice is
remembered per browser. This is independent of the recommendation pool. The
count shows **N manga**, or **N of M manga** when a search or filter is active.
Search and ordering run locally and keep their state while you visit a manga
and return; search clears on a page reload.

Tapping a manga scans that manga's entry folders and images on the PC. This
happens every time you open it from Explore, so chapters added since
your last visit appear without a Refresh. A manga that cannot be read explains
why instead of opening:

- it has no entry folders (for example, its images sit directly inside it); or
- it has entry folders, but none of them contain JPG or PNG images.

Once scanned, the manga presents two equally prominent buttons side by side,
below its title and badges:

- **Read** — selection-free reading with an independent saved position.
- **Edit** — reading plus page selection controls and a saved editing position.

### Recommendations and preferences

**Recommend** chooses a readable title using your preferences and immediately
opens its reader. Tap the reader's back arrow to reach its feedback screen,
which offers **Read** to continue reading, an independent **Favorite** toggle,
and these feedback options:

| Choice | Effect |
| --- | --- |
| Enjoyed it | Adds one enjoyment and gives the title some breathing room. |
| Too soon | Excludes the title for 72 hours. |
| Not in the mood | Excludes the title for 48 hours. |
| Less often | Persistently reduces its recommendation weight. |
| Not for me | Excludes it until restored in Explore; also removes Favorite. |
| No feedback | Resolves the offer without changing preferences or enjoyment counts. |

Favorite increases a title's weight but does not resolve its feedback. You must
explicitly choose one of the six outcomes to continue to the home screen,
Explore, or another recommendation. There is no Something Else action.
Recommendations open in **Read** only, at the saved reading position. Use
Explore to reach Edit. Returning from recommended reading shows its feedback
screen. Reloading, restarting the server, or taking control from another page
also restores any pending feedback. A renamed or removed title can still
receive feedback even though it can no longer be opened.

The recommendation pool uses three directly tappable options: **All**
(the default), **Normal only**, and **18+ only**. A manga is 18+ when its exact
folder name starts with a nonempty `[author]` prefix, such as
`[Kino Hitoshi] Sample name`.
Each browser or Home Screen installation remembers its own recommendation
filter; Reset Preferences leaves it unchanged. Explore has its own independent
All / Normal only control.

Explore shows Favorite as a green **★**, Less often as a muted coral **↓**, and
Not for me with a red dashed outline and a muted, readable title. The symbols
have accessible descriptions. Compact **Editing** / **Reading** and violet
**18+** labels can appear alongside them. Open a
title's Read/Edit menu to change or remove its recommendation preferences,
including restoring a Not for me title.

Recommendation picks use the current top-level folder names and scan only
chosen candidates. Empty or unreadable candidates are skipped. If no readable
title is eligible, try another filter, Explore, or wait for a cooldown.

**Reset Preferences** asks for confirmation and clears all recommendation
preferences and timing information. Reading and editing data are preserved.
**Clean Up** lists recommendation records whose exact manga names are absent
from the source library. Select the records to remove and confirm. It checks
the live folder names again before removing them, creates no backup, and leaves
all `.read.json`, `.edit.json`, exports, and source files in place. Renaming a
manga gives it a fresh recommendation identity; the old preferences stay until
Clean Up removes them. Resolve any pending feedback before using either action.

### Reader behavior

Read and Edit each remember one latest entry/image pair per manga. Opening
either starts there. If that image is no longer on disk, it starts at the first
image of the saved entry; if the entry is gone too, at the first image of the
first entry. With nothing saved yet, it starts at the first image of the first
entry. Choosing an entry from the picker starts at its first image. Crossing
forward into the next entry starts at its first image; crossing backward into
the previous entry starts at its last image.

In Edit, the **Selected · N** list shows the selected images in the current
entry that exist on disk; only those can be navigated to. Files deleted on the
PC while you are reading stay listed until you return to Explore and
open the manga again; opening one in the meantime fails to load the image.

The web reader preserves the immediate navigation behavior: it displays the
prefetched neighboring image without a decorative transition. In Edit, the
full-width strip along the bottom of the screen is tinted light green; tapping it
toggles selection. The tint stays visible even when the controls are hidden and
is faint enough that a long page remains readable through it. Tapping the center
shows or hides the controls. Read has no selection strip.

Only one browser page can control the library at a time. Closing or navigating
away does not depend on an unreliable browser shutdown signal: ownership stays
with that page until another page chooses **Take Control** or the server
restarts. There is no manual release button; closing the Home Screen app keeps
ownership. Other pages show a waiting screen until they take control.
Takeover is immediate after any save or rescan already being committed
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
  **Retry** once the PC is reachable. Leaving the view, opening a manga, and
  refreshing the library still wait until every selection is confirmed.

If control moved to another page or the library changed, that is reported
immediately as before. The old page must explicitly take control back; the app
then refreshes authoritative state instead of blindly replaying uncertain
writes.

See [Application design](docs/design.md) for operational and
security details.

## App-managed metadata

The server keeps a flat folder inside the library. Each manga's files share its
exact folder name, so they sort together:

```text
C:\Manga Library\.pocket-manga-editor\
├── Kimi wa 08.read.json
├── Kimi wa 08.edit.json
├── Kimi wa 08.export\
│   └── V1_C01 - First Day\
│       └── V1_C01 - First Day__2.png
├── _exporter.bat
├── _exporter.py
└── _recommendations.json
```

- `<manga>.read.json` is created the first time you turn a page in **Read** for
  that manga and stores only the latest Read entry/image pair.
- `<manga>.edit.json` is created the first time you turn a page or select an
  image in **Edit**, and stores the latest Edit pair and the selected images per
  entry.
- `<manga>.export\` exists only after you export that manga with the companion
  script.
- `_exporter.bat` and `_exporter.py` are reinstalled, replacing older copies,
  every time the server starts.
- `_recommendations.json` is created on the first recommendation or preference
  change. It stores preferences and aggregate timing/counts by exact manga
  folder name, one pending recommendation, and the last feedback resolution for
  safe retries. It keeps no full recommendation history or catalog of pages.
  The 18+ classification is derived from folder names; the remembered filter
  lives in browser storage.

Most manga have only one of the two JSON files; both may exist. Everything in
`.pocket-manga-editor` is application-managed. Do not add files that need to be
preserved, and edit the JSON files only to remove a selection whose image is
gone (see [Exporting selected pages](#exporting-selected-pages)).

The app writes only what you change. A page turn replaces the saved position,
and selecting or deselecting an image adds or removes that one name. A selection
is never removed automatically, even when its image or entry is missing from
disk: it stays in `<manga>.edit.json`, is hidden from the **Selected** list, and
shows again if the file returns. Only a deselection in the app or a manual edit
removes it. Move or copy a finished export elsewhere if you want to
keep it; the next export of that manga replaces the folder.

For a backup, stop the server and copy the `.pocket-manga-editor` folder to a
location outside the library, together with `.env`. To restore, stop the server
and restore that folder into the same library. Source images need their own
backup; Git does not contain them or your metadata. Malformed reading or editing
metadata produces an error and remains untouched. Restore a known-good copy
before continuing. Malformed recommendation metadata likewise produces an error
and is left untouched. A saved position that refers to a removed image or entry
falls back as described in [Web application](#web-application).

There is intentionally no **Complete Manga** operation. Exporting never deletes
source files or clears reading positions or selections.

## Exporting selected pages

Export runs on the PC, not in the web app:

1. Open `C:\Manga Library\.pocket-manga-editor` in File Explorer and
   double-click `_exporter.bat`.
2. The window lists every manga with at least one selected page and its image
   count. Type the number of the manga to export and press Enter, or press Enter
   alone to cancel.
3. The script shows `Copying 37 of 120` as it copies, then prints the export
   folder and waits for a key press before closing.

The export folder is `<manga>.export\<entry>\<entry>__<image>`, with the exact
entry and image names and unchanged image bytes. Each export rebuilds that
folder from the current selections: the script copies into
`<manga>.export-tmp`, swaps it into place, and deletes the previous export, so
deselected pages disappear. If a run is interrupted, run it again; it removes
any leftover `-tmp` or `-old` folder first. Source images are only ever read.

If a selected image no longer exists in the library, the script lists it and
stops without changing the previous export. Restore the image, or open
`<manga>.edit.json` in a text editor and delete that name from its entry's
`selected_images` list (delete the whole entry if the list becomes empty), then
export again. The web app cannot deselect an image it cannot show. Editing the
file is safe while the server runs; the app picks up the change the next time
you open that manga from Explore.

The script needs Python 3.11 or newer from python.org; the `.bat` file uses the
`py` launcher and falls back to `python`. It does not use the repository
`.venv` and can run while the server is running. Its source lives in
`pocket_manga_editor/companion/`.

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
handling, the export companion and its installation, token ownership, HTTP/API
security, image delivery, recommendation weighting and recovery, cleanup,
Explore search/filtering and saved-activity ordering, and web-client behavior.
Optional JavaScript behavior checks use Node.js on Windows or JavaScriptCore on
macOS; the app itself requires neither runtime.
