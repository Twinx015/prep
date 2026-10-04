# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the `arc` executable.

    pyinstaller arc.spec       # -> ./dist/arc

Single-file build. arc is an interactive TUI, so it has to keep its console.
arc shells out to pacman/yay/paru and is therefore Linux-only; there is nothing
to bundle as data.
"""

import importlib.util
import os

from PyInstaller.utils.hooks import collect_all, copy_metadata

# arc imports textual lazily inside run_tui(). Static analysis does see those
# imports, but textual resolves widgets, CSS and its own version metadata at
# runtime, so collect the package wholesale instead of relying on the hook.
t_datas, t_binaries, t_hidden, t_metadata = [], [], [], []
if importlib.util.find_spec("textual"):
    t_datas, t_binaries, t_hidden = collect_all("textual")
    t_metadata = copy_metadata("textual")
else:
    print("arc.spec: textual is not installed -- the executable will exit with "
          "`missing dep` unless textual is installed in the build environment.")

a = Analysis(
    [os.path.join(SPECPATH, "arc", "main.py")],
    pathex=[SPECPATH],
    binaries=t_binaries,
    datas=t_datas + t_metadata,
    hiddenimports=t_hidden,
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
    name="arc",
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