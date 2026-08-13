# Pocket Manga Editor

Pocket Manga Editor is a development-stage, keyboard-first desktop tool for reviewing manga JPGs and exporting a hand-picked set of pages. It reads chapter folders directly, so it does not need a separate volume-preparation phase and never modifies source images.

The original `manga_image_saver.ipynb` is retained as a proof of concept. The desktop application is implemented separately in the `pocket_manga_editor` Python package.

## Expected folders

Choose a working directory containing one or more manga folders:

```text
Working Directory/
└── Manga Name/
    ├── Vol.01 Ch.001 - Chapter title/
    │   ├── 001.jpg
    │   ├── 002.jpg
    │   └── 003.jpg
    └── Vol.01 Ch.002 - Another title/
        ├── 001.jpg
        └── 002.jpg
```

Folder and page numbers are parsed numerically. The current development version intentionally accepts only these conventions:

- Chapter folder: `Vol.<2 digits> Ch.<3 digits> - <chapter name>`
- Page file: `<3 digits>.jpg` (the `.jpg` extension is case-insensitive)

All matching chapters for one volume appear in the viewer as a single continuous virtual volume.

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

The application keeps a shortcut hint visible below the viewer. Press `?` or `F1` at any time to see the complete help dialog.

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
<manga-folder>/Output/Vol.01/
```

Exported names include both chapter and page numbers, such as `C002_P017.jpg`, so page `001.jpg` from different chapters cannot collide. Repeat exports remove only stale files recorded as app-created; unrelated files in the output folder are preserved. If an exported JPG has been edited since the last export, the app refuses to overwrite or remove it. Selections remain available after export until the user explicitly clears them.

## Run tests

The scanner, saved-session, and exporter tests use only Python's standard library:

```powershell
py -m unittest discover -s tests -v
```
