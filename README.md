# Pocket Manga Editor

Pocket Manga Editor is a development-stage, keyboard-first desktop tool for reviewing manga JPG and PNG images and exporting a hand-picked set of pages. It reads chapter folders directly, so it does not need a separate volume-preparation phase and never modifies source images.

The original `manga_image_saver.ipynb` is retained as a proof of concept. The desktop application is implemented separately in the `pocket_manga_editor` Python package.

## Expected folders

Choose a working directory containing one or more manga folders:

```text
Working Directory/
└── Manga Name/
    ├── Vol. 01 Ch. 000.01/
    │   ├── 001.jpg
    │   ├── 002.png
    │   └── 003.jpg
    ├── Vol. 01 Ch. 001 - Another title/
        ├── 001.jpg
        └── 002.jpg
    └── Vol. 02.5/
        ├── 001.jpg
        ├── 002.png
        └── 054.18.png
```

Folder and page numbers are parsed numerically. The current development version intentionally accepts only these conventions:

- Required chapter folder portion: `Vol. <digits> Ch. <digits or decimal>`
- Optional suffix: ` - <chapter name>`
- Direct volume folder: `Vol. <digits or decimal>`
- Page file: `<3 digits>[.<decimal digits>].jpg` or `.png`, such as `054.png` or `054.18.png` (extensions are case-insensitive)

All matching chapters for one volume appear in the viewer as a single continuous virtual volume. A direct volume folder is already one volume, so its pages are read directly without a preparation step. The two storage styles can be mixed for different volumes in one manga. If the same numeric volume appears in both styles, that volume is skipped and reported as a scan issue rather than being combined ambiguously.

Volume, chapter, and page identifiers are sorted numerically, including decimals. The optional chapter name has no effect on sorting or export. Original number spelling is preserved for display and generated paths. Numerically equivalent identifiers such as `000.1` and `000.10` are reported as an ambiguous duplicate instead of being silently combined.

## Development setup

Python 3.10 or newer is required. From the repository root on Windows:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

The package entry point also works: `.venv\Scripts\python -m pocket_manga_editor`.

Packaging is deliberately out of scope during this development phase.

## Review controls

The viewer gives portrait pages most of the window while library controls, page
details, review actions, and export controls stay together in a scrollable
sidebar on the right. Essential keycap hints remain pinned at the bottom. Drag
the divider to resize either side; the chosen split is remembered. Press `?` or
`F1` at any time to see the complete help dialog.

| Key | Action |
| --- | --- |
| `←` / `→` | Previous or next page |
| `Ctrl+←` / `Ctrl+→` | Previous or next selected page |
| `Space` | Select or deselect the current page |
| `Enter` | Select the current page and advance |
| `Home` / `End` | First or last page |
| `Ctrl+S` | Export selected pages |
| `F5` | Rescan the working directory |
| `?` / `F1` | Show keyboard help |

The same primary actions are available as buttons. A selected page is indicated with a green border, a checkmark badge, and an updated selected count.

## Saved progress and export

Selection changes and the current page are saved automatically as relative paths under:

```text
<working-directory>/.pocket-manga-editor/selections/
```

Choosing **Export Selected** copies the current selection to:

```text
<working-directory>/.pocket-manga-editor/output/<manga-name>/Vol.01/
```

This keeps selections, export bookkeeping, and exported images together under
`.pocket-manga-editor`; source manga folders are not given generated `Output`
subdirectories.

For chapter-based sources, exported names include both chapter and page numbers, such as `C002_P017.jpg`. Direct-volume pages use names such as `P017.png`. The source image format is preserved. Repeat exports remove only stale files recorded as app-created; unrelated files in the output folder are preserved. If an exported image has been edited since the last export, the app refuses to overwrite or remove it. Selections remain available after export until the user explicitly clears them.

## Run tests

The test suite covers scanning, saved sessions, exporting, and the responsive
desktop layout. Install the development requirements first; the GUI checks use
PySide6's offscreen platform and do not open a window:

```powershell
py -m unittest discover -s tests -v
```
