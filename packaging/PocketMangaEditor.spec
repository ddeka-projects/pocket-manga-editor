# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


SPEC_DIRECTORY = Path(SPECPATH)
REPOSITORY_ROOT = SPEC_DIRECTORY.parent
COMPANION_ASSETS = REPOSITORY_ROOT / "pocket_manga_editor" / "companion" / "assets"
WINDOWS_ICON = COMPANION_ASSETS / "icon-512.png"


analysis = Analysis(
    [str(REPOSITORY_ROOT / "run.py")],
    pathex=[str(REPOSITORY_ROOT)],
    binaries=[],
    datas=[
        (
            str(COMPANION_ASSETS),
            "pocket_manga_editor/companion/assets",
        ),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Pocket Manga Editor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(WINDOWS_ICON),
    contents_directory="_internal",
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Pocket Manga Editor",
)
