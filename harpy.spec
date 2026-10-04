# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the `harpy` executable.

    pyinstaller harpy.spec     # -> ./dist/harpy

Single-file build. The entry script is harpy/__main__.py rather than
harpy/main.py: a frozen entry script runs as __main__ without package context,
so the package has to be imported by absolute name (see harpy/__main__.py).

Nothing is bundled as data -- harpy reads and writes cheat sheets in the
directory given by --dir, ~/.harpy by default. The sample sheets in docs/ are
development data, not part of the program.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    [os.path.join(SPECPATH, "harpy", "__main__.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    # main.py imports display/parser/search/store at module level; listing them
    # keeps the graph complete if any of those imports ever move into a function.
    hiddenimports=collect_submodules("harpy"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="harpy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)