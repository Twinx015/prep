from __future__ import annotations

import argparse
from typing import Any, Optional

from .common import print_bold, print_dim, print_err, print_ok, print_warn
from .config import (
    APP_NAME,
    DEFAULT_HOST,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_PORT,
    DEFAULT_PREFIX,
    DEFAULT_SYSTEM,
    DEFAULT_TITLE,
    read_settings,
    settings_file,
    write_settings,
)

SETTINGS = [
    ("host", "listen address", "str", DEFAULT_HOST),
    ("port", "listen port", "int", DEFAULT_PORT),
    ("prefix", "url prefix", "str", DEFAULT_PREFIX),
    ("title", "page title", "str", DEFAULT_TITLE),
    ("system", "system prompt", "str", DEFAULT_SYSTEM),
    ("ollama_host", "ollama server", "str", DEFAULT_OLLAMA_HOST),
    ("dev", "developer mode", "bool", False),
    ("state_dir", "state directory (empty = default)", "str", ""),
    ("log_file", "log file (empty = default)", "str", ""),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prep-config", description="change prep settings interactively"
    )
    parser.add_argument(
        "--file",
        default=None,
        help="config file path (default: ~/.config/prep/config.toml)",
    )
    return parser


def defaults() -> dict:
    return {key: default for key, _label, _kind, default in SETTINGS}


def merged() -> dict:
    data = dict(defaults())
    data.update(read_settings())
    return data


def render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_value(kind: str, raw: str) -> Any:
    text = raw.strip()
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{raw!r} is not a valid integer")
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"{raw!r} is not a valid boolean (y/n)")
    return text


def to_index(choice: str) -> Optional[int]:
    if not choice.isdigit():
        return None
    index = int(choice)
    return index if 1 <= index <= len(SETTINGS) else None


def prompt_choice() -> str:
    try:
        return input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


def ask_save() -> bool:
    try:
        return input("save changes? [y/N] > ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def edit_value(key: str, kind: str, current: Any) -> Any:
    raw = input(f"  {key} [{render_value(current)}] > ").strip()
    if not raw:
        return current
    return parse_value(kind, raw)


def show(data: dict) -> None:
    print_bold(f"{APP_NAME} settings {settings_file()}")
    for index, (key, label, _kind, _default) in enumerate(SETTINGS, start=1):
        print(f"  {index}) {key:<12} {render_value(data.get(key))}   ({label})")


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if not settings_file(args.file).exists():
        print_dim("no config file yet - defaults are shown")
    data = merged()
    dirty = False
    show(data)
    while True:
        choice = prompt_choice()
        if choice == "q":
            break
        if choice == "s":
            path = write_settings(data, args.file)
            dirty = False
            print_ok(f"saved {path}")
            show(data)
            continue
        index = to_index(choice)
        if index is None:
            print_warn("enter a row number, 's' to save, or 'q' to quit")
            continue
        key, _label, kind, _default = SETTINGS[index - 1]
        try:
            new_value = edit_value(key, kind, data.get(key))
        except ValueError as exc:
            print_err(str(exc))
            continue
        data[key] = new_value
        dirty = True
        print_ok(f"{key} = {render_value(new_value)}")
    if dirty and ask_save():
        path = write_settings(data, args.file)
        print_ok(f"saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
