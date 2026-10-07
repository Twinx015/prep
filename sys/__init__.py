"""sys — one TUI (plus a tiny web UI) for Ollama, packages and cheat sheets.

The package lives in `sys/` on disk but is imported as `sysapp`, because a
package named `sys` would be shadowed by the standard library's `sys`.
"""

from .main import APP_NAME, APP_VERSION, main

__all__ = ["APP_NAME", "APP_VERSION", "main"]
