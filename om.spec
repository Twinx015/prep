# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the `om` executable.

    pyinstaller om.spec        # -> ./dist/om

Single-file build. om is an interactive TUI, so it has to keep its console.
There is nothing to bundle as data: om keeps its config, history and logs under
$XDG_STATE_HOME/om (%LOCALAPPDATA% on Windows) and spawns the real `ollama`
binary that it finds on PATH.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# prompt_toolkit looks up its key bindings with dynamic imports inside
# prompt_toolkit.key_binding.key_bindings.load_key_bindings(), which static
# analysis cannot follow.
hiddenimports = collect_submodules("prompt_toolkit.key_binding.bindings")

a = Analysis(
    [os.path.join(SPECPATH, "om", "main.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
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
    name="om",
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