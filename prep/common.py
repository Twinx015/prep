from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

NO_COLOR = "NO_COLOR"

COLORS = {
    "reset": 0,
    "bold": 1,
    "dim": 2,
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}


def color_enabled(stream: Any = None, force: bool = False) -> bool:
    if force:
        return True
    if os.environ.get(NO_COLOR) or os.environ.get("TERM") == "dumb":
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: Any, name: str = "reset", force: bool = False) -> str:
    code = COLORS.get(name)
    if code is None or not color_enabled(force=force):
        return str(text)
    return f"\x1b[{code}m{text}\x1b[0m"


def cprint(
    text: Any,
    name: str = "reset",
    end: str = "\n",
    stream: Any = None,
    force: bool = False,
    flush: bool = False,
) -> None:
    stream = stream or sys.stdout
    print(paint(text, name, force=force), end=end, file=stream, flush=flush)


def print_bold(text: Any, **kwargs: Any) -> None:
    cprint(text, "bold", **kwargs)


def print_dim(text: Any, **kwargs: Any) -> None:
    cprint(text, "dim", **kwargs)


def print_ok(text: Any, **kwargs: Any) -> None:
    cprint(text, "green", **kwargs)


def print_warn(text: Any, **kwargs: Any) -> None:
    cprint(text, "yellow", **kwargs)


def print_err(text: Any, **kwargs: Any) -> None:
    cprint(text, "red", stream=sys.stderr, **kwargs)


def print_info(text: Any, **kwargs: Any) -> None:
    cprint(text, "cyan", **kwargs)


def _to_path(path: Any) -> Path:
    return path if isinstance(path, Path) else Path(path).expanduser()


def ensure_dir(path: Any) -> Path:
    target = _to_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_text(path: Any, default: str = "") -> str:
    try:
        return _to_path(path).read_text(encoding="utf-8")
    except OSError:
        return default


def write_text(path: Any, text: str) -> Path:
    target = _to_path(path)
    ensure_dir(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target


def append_text(path: Any, text: str) -> Path:
    target = _to_path(path)
    ensure_dir(target.parent)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(text)
    return target


def read_json(path: Any, default: Any = None) -> Any:
    try:
        return json.loads(_to_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Any, data: Any, indent: int = 2) -> Path:
    return write_text(path, json.dumps(data, indent=indent, ensure_ascii=False))


def touch(path: Any) -> Path:
    target = _to_path(path)
    ensure_dir(target.parent)
    target.touch(exist_ok=True)
    return target


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log_line(path: Any, line: str, stamp: bool = True) -> Path:
    text = f"[{timestamp()}] {line}\n" if stamp else f"{line}\n"
    return append_text(path, text)