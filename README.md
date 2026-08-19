# Pocket Manga Editor

Pocket Manga Editor is a development-stage desktop editor with an iPhone Home
Screen companion. It follows the source filesystem directly: a manga contains
arbitrarily named image folders, and each folder contains JPG or PNG images.
The app does not parse volumes or chapters and never combines folders into a
constructed reading unit.

Desktop and phone **Edit** share selections and editing position. Phone
**Read** keeps a separate bookmark and has no selection behavior. Reviewing,
reading, editing, and export do not modify source images. The explicitly
confirmed **Complete Manga** operation permanently deletes its source folder.

## Source library

Choose a working directory containing one or more manga folders:

```text
Working Directory/
└── Kimi wa 08/
    ├── V1_C01 - First Day/
    │   ├── 1.jpg
    │   ├── 2.png
    │   └── 10.jpg
    ├── Volume Two - Ch 12/
    │   ├── page 1.png
    │   └── page 2.png
    └── Bonus artwork/
        └── extra-cover.JPG
```

- Every normal direct child of the working directory is a manga, except
  `.pocket-manga-editor`. The exact manga name `.library-mutation.lock` is also
  reserved for the cross-process library lock.
- Every direct child of a manga containing at least one direct `.jpg` or `.png`
  file is an image folder.
- Extensions are case-insensitive and exact folder names and complete image
  filenames are preserved.
- Folders and filenames use deterministic, case-insensitive natural ordering,
  so `1.jpg`, `2.jpg`, and `10.jpg` appear in that order.
- Nested folders, images directly in the manga root, JPEG files using `.jpeg`,
  archives, PDFs, symlinks, and other formats are not included.

Renaming an image or folder creates a new identity. Stale saved references are
ignored; the app does not infer renames.

## Development setup

Python 3.10 or newer is required. On Windows:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

On macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

Packaging remains out of scope during development.

## Desktop editing

The desktop is always an editing surface. Choose a manga and an exact source
folder, then review and select images. Progress and selections autosave.

| Key | Action |
| --- | --- |
| `←` / `→` | Previous or next image |
| `Ctrl+←` / `Ctrl+→` | Previous or next selected image in the folder |
| `Space` | Select or deselect the current image |
| `Enter` | Select the current image and advance |
| `Home` / `End` | First or last image |
| `Ctrl+S` | Synchronize the current manga output |
| `F5` | Rescan the working directory |
| `?` / `F1` | Show keyboard help |

## App-managed workspace

Each manga has an isolated workspace:

```text
Working Directory/.pocket-manga-editor/
└── Kimi wa 08/
    ├── reading.json
    ├── editing.json
    ├── output/
    │   └── V1_C01 - First Day/
    │       └── V1_C01 - First Day__2.png
    ├── completed/
    │   ├── batch-0001/
    │   └── batch-0002/
    └── .transactions/
```

`reading.json` contains only phone reading bookmarks. `editing.json` contains
desktop/phone editing positions, exact selected filenames, and output ownership
records. The files are written atomically while holding the cross-process
library mutation lock.

This is an application-owned directory. Do not manually edit active JSON or
managed output. The exporter nevertheless preserves untracked files and refuses
to overwrite or remove a managed file whose bytes changed after export.

This release is a clean development-stage break. It does not read or migrate
the former global `selections`, `exports`, `output`, `completed`, or completion
log layout. Remove the old `.pocket-manga-editor` directory before testing this
model if the workspace was created by an earlier build.

## Whole-manga export

One **Export Manga** action reconciles every edited folder in the current manga.
After success, each app-managed output folder exactly represents that folder's
current selections:

- newly selected images are copied;
- unchanged selected images are retained and verified;
- deselected managed images are removed;
- a managed folder with no selections is removed only if it becomes empty;
- unrelated files are preserved; and
- modified managed files or name/path collisions stop the export before data is
  overwritten.

Exported names are `<exact folder name>__<exact image filename>` and retain the
original image bytes and extension. The multi-folder operation is transactional;
failed or interrupted work is rolled back or recovered before new mutations.

## Completing a manga

**Complete Manga** requires at least one verified app-managed exported image.
After destructive confirmation, it moves the complete active output into the
next immutable per-manga batch, such as:

```text
.pocket-manga-editor/Kimi wa 08/completed/batch-0001/
```

It then permanently deletes the source manga and removes active `reading.json`
and `editing.json`. Earlier batches remain unchanged. A later same-name source
manga reuses the workspace and can create `batch-0002`. No global completion
log or source/output coverage comparison is used; source folders without
selections are valid. Completion is journaled and recovered before scanning.

## Mobile Companion Mode

Companion Mode serves an installable reader/editor over the trusted local
network. After tapping a manga, the phone always asks:

- **Read** — selection-free reading using only `reading.json`.
- **Edit** — shared desktop/mobile position and selections using `editing.json`.

Returning from the reader goes back to this choice. Read and Edit can resume at
different folders and images. Export, completion, deletion, rescanning, and
working-directory changes remain desktop-only.

Setup:

1. Reserve a stable IP address for the PC in the router.
2. Open **Connection…** in the desktop Mobile Companion card and enter that
   address and a fixed port (`8765` by default).
3. Permit the application on private networks if the firewall prompts.
4. Choose **Pair Phone**, open the displayed address, and enter the one-time code.
5. In Safari, choose **Share → Add to Home Screen**.
6. On later launches, a remembered paired phone automatically puts the app into
   Companion Mode when the saved library and Companion server are available.
7. Use **End Companion Mode** to return ownership to the desktop. **Start
   Companion Mode** remains available for manual entry when no phone is remembered.

Only one desktop process and one mobile controller are allowed. Companion uses
unencrypted HTTP and is intended only for a trusted home LAN; do not expose it
with port forwarding. See [Companion Mode](docs/companion-mode.md) for details.

## Tests

Install the development requirements, then run:

```powershell
py -m unittest discover -s tests -v
```

The suite covers discovery and natural ordering, independent metadata,
transactional export and completion, crash recovery, singleton and mutation
locks, Companion authorization/activity isolation, authenticated images,
mobile asset contracts, and the desktop layout.
