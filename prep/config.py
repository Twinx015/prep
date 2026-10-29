from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Callable, Optional

from .common import write_text

APP_NAME = "prep"
APP_VERSION = "1.0.0"
ENV_PREFIX = "PREP"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
DEFAULT_OLLAMA_HOST = "127.0.0.1:11434"
DEFAULT_PREFIX = "/ollama"
DEFAULT_TITLE = "prep"
DEFAULT_SYSTEM = ""
DEFAULT_LOG_FILE = "server.log"

_FILE_CACHE: dict = {}


def env(key: str) -> str:
    return f"{ENV_PREFIX}_{key}"


def get_env(key: str, default: Any = None, cast: Callable[[str], Any] = str) -> Any:
    raw = os.environ.get(env(key))
    if raw is not None:
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return default
    value = loaded_settings().get(key.lower())
    if value is None:
        return default
    if not isinstance(value, str) or cast is str:
        return value
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def get_str(key: str, default: str = "") -> str:
    return get_env(key, default, cast=str)


def get_int(key: str, default: int = 0) -> int:
    return get_env(key, default, cast=int)


def get_bool(key: str, default: bool = False) -> bool:
    def parse(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}

    return get_env(key, default, cast=parse)


def host() -> str:
    return get_str("HOST", DEFAULT_HOST)


def port() -> int:
    return get_int("PORT", DEFAULT_PORT)


def ollama_host() -> Optional[str]:
    raw = os.environ.get("OLLAMA_HOST")
    if raw:
        return raw
    value = loaded_settings().get("ollama_host")
    return value if value else None


def prefix() -> str:
    return get_str("PREFIX", DEFAULT_PREFIX)


def title() -> str:
    return get_str("TITLE", DEFAULT_TITLE)


def system_prompt() -> str:
    return get_str("SYSTEM", DEFAULT_SYSTEM)


def dev_enabled() -> bool:
    return get_bool("DEV", False)


def settings_file(override: Optional[str] = None) -> Path:
    raw = override or os.environ.get(env("CONFIG"))
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME / "config.toml"


def read_settings(path: Optional[str] = None) -> dict:
    target = Path(path).expanduser() if path else settings_file()
    if not target.exists():
        return {}
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def write_settings(data: dict, path: Optional[str] = None) -> Path:
    target = Path(path).expanduser() if path else settings_file()
    lines = [f"{key} = {_toml_value(value)}" for key, value in sorted(data.items())]
    write_text(target, "\n".join(lines) + "\n")
    return target


def loaded_settings() -> dict:
    if not _FILE_CACHE:
        _FILE_CACHE.update(read_settings())
    return _FILE_CACHE


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def state_dir(override: Optional[str] = None) -> Path:
    raw = override or get_env("STATE_DIR", None, cast=str)
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / APP_NAME


def chats_dir(override: Optional[str] = None) -> Path:
    return state_dir(override) / "chats"


def log_file(override: Optional[str] = None) -> Path:
    raw = override or get_env("LOG_FILE", None, cast=str)
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME / DEFAULT_LOG_FILE
