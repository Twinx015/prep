# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the `sys` executable.

    pyinstaller spec_files/sys.spec    # -> ./dist/sys

Single-file build. sys is an interactive TUI, so it has to keep its console.
The web UI's assets are bundled as data (`sys/static` -> `<tmp>/static`, which
is where sys/main.py looks for them when frozen); everything else lives under
$XDG_CONFIG_HOME/sys or $XDG_DATA_HOME/sys and stays outside the bundle.
FastAPI and uvicorn register routes through decorators that static analysis
cannot follow, and Textual imports its widgets lazily from
textual.widgets.__getattr__(), so all three are collected by hand.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

hiddenimports = (
    collect_submodules("fastapi")
    + collect_submodules("uvicorn")
    + collect_submodules("textual.widgets")
)

a = Analysis(
    [os.path.join(ROOT, "sys", "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "sys" / "static"), "static")],
    hiddenimports=hiddenimports,
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
    name="sys",
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
