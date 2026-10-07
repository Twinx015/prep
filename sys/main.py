#!/usr/bin/env python3
"""sys - one TUI (plus a tiny web UI) for Ollama, packages and cheat sheets.

    sys                        open the TUI
    sys --web                  run the web UI instead
    sys --tab cheats           start on a given tab
    sys --cheat "git rebase"   one-shot cheat-sheet search, then exit
    sys --init-config          (re)write ~/.config/sys/config.toml
    sys --set web.port=9000    override any option for this run
    sys --show-config          print the effective configuration

Everything lives in this single file, in sections:

    1 config      2 helpers     3 cheats     4 packages   5 ollama
    6 render      7 dialogs     8 app        9 web       10 cli

Reuses the code of the earlier `arc` (pacman/AUR jobs), `harpy` (markdown
cheat sheets) and `om-tui` (Ollama chat) projects, so the old projects can be
deleted without losing anything.

Deps: textual, rich, httpx, rapidfuzz, fastapi, uvicorn   (Python 3.10+)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import difflib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional
from urllib.parse import quote
from urllib.request import urlopen

import httpx
from pydantic import BaseModel
from rapidfuzz import fuzz
from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)
from textual.widgets.option_list import Option

APP_NAME = "sys"
APP_VERSION = "0.2.0"

# config

CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / APP_NAME
CONFIG_PATH = CONFIG_HOME / "config.toml"

DEFAULTS: dict[str, dict[str, Any]] = {
    "app": {
        "theme": "sys-dark",
        "accent": "#4da3ff",
        "background": "#0e0e0e",
        "surface": "#141414",
        "panel": "#1a1a1a",
        "foreground": "#dcdcdc",
        "dim": "#7d7d7d",
        "line": "#333333",
        "log_bg": "#0b0b0b",
        "input_bg": "#101010",
        "code_theme": "monokai",
        "code_bg": "#121212",
        "show_thinking": True,
        "poll_running": 4.0,
    },
    "ollama": {"host": "", "keep_alive": ""},
    "cheats": {"dir": "~/.harpy"},
    "packages": {"helper": "auto", "jobs": ""},
    "web": {
        "host": "127.0.0.1",
        "port": 8787,
        "allow_root_actions": True,
        "open_browser": False,
        "title": APP_NAME,
        "assets": "",
        "readme": "",
    },
    "data": {"dir": ""},
}

PATH_KEYS = {"cheats.dir", "packages.jobs", "data.dir", "web.assets", "web.readme"}
ENV_KEYS = {
    "SYS_CONFIG": "config.path",
    "SYS_OLLAMA_HOST": "ollama.host",
    "SYS_ACCENT": "app.accent",
    "SYS_THEME": "app.theme",
    "SYS_CHEAT_DIR": "cheats.dir",
    "SYS_DATA_DIR": "data.dir",
    "SYS_WEB_HOST": "web.host",
    "SYS_WEB_PORT": "web.port",
    "SYS_AUR": "packages.helper",
    "SYS_THINKING": "app.show_thinking",
    "SYS_NO_ROOT": "web.allow_root_actions",
}


def parse_value(raw: str) -> Any:
    """`"true"` -> True, `"80"` -> 80, anything else stays a string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge `extra` into a copy of `base`."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def toml_dump(data: dict) -> str:
    """Small TOML writer: only the flat tables this config uses."""
    def val(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    chunks = []
    for section, values in data.items():
        if not isinstance(values, dict):
            chunks.append(f"{section} = {val(values)}\n")
            continue
        body = "".join(f"{k} = {val(v)}\n" for k, v in values.items())
        chunks.append(f"[{section}]\n{body}\n")
    return "\n".join(chunks)


def load_toml(path: Path) -> dict:
    """Read a TOML file; missing/invalid files fall back to an empty dict."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class Config:
    """Effective configuration plus the file it came from."""

    data: dict
    path: Path

    @classmethod
    def load(
        cls,
        path: str = "",
        overrides: Optional[dict] = None,
        use_env: bool = True,
    ) -> "Config":
        cfg_path = Path(
            path or os.environ.get("SYS_CONFIG") or CONFIG_PATH
        ).expanduser()
        data = deep_merge(DEFAULTS, load_toml(cfg_path))
        if use_env:
            data = deep_merge(data, cls._from_env())
        if overrides:
            data = deep_merge(data, overrides)
        for dotted in PATH_KEYS & set(flatten_keys(data)):
            raw = str(dig(data, dotted, ""))
            if raw:
                setdig(data, dotted, expand_path(raw))
        return cls(data=data, path=cfg_path)

    @classmethod
    def _from_env(cls) -> dict:
        """Turn SYS_* environment variables into a nested dict."""
        out: dict = {}
        for var, dotted in ENV_KEYS.items():
            if var not in os.environ or dotted == "config.path":
                continue
            value: Any = os.environ[var]
            if var == "SYS_NO_ROOT":
                value = value.strip().lower() not in ("0", "no", "false", "")
            setdig(out, dotted, parse_value(str(value)))
        return out

    def get(self, dotted: str, default: Any = None) -> Any:
        """`cfg.get("web.port")` -> the value or `default`."""
        return dig(self.data, dotted, default)

    def set(self, dotted: str, value: Any) -> None:
        setdig(self.data, dotted, value)

    def section(self, name: str) -> dict:
        """All keys of one table, e.g. `cfg.section("web")`."""
        return dict(self.data.get(name) or {})

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(toml_dump(self.data), encoding="utf-8")
        return self.path

    def dumps(self) -> str:
        return toml_dump(self.data)


def flatten_keys(data: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, val in data.items():
        dotted = f"{prefix}{key}"
        out.add(dotted)
        if isinstance(val, dict):
            out |= flatten_keys(val, dotted + ".")
    return out


def dig(data: dict, dotted: str, default: Any = None) -> Any:
    """Dict lookup by dotted path ("web.port")."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def setdig(data: dict, dotted: str, value: Any) -> None:
    """Set a value by dotted path, creating tables as needed."""
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


CONFIG_TEMPLATE = """\
# sys configuration - every key is optional, this file lists the defaults.
# Overrides for one run: --set web.port=9000   or   SYS_WEB_PORT=9000

[app]
theme = "sys-dark"          # registered textual theme name
accent = "#4da3ff"          # primary colour used everywhere
background = "#0e0e0e"
surface = "#141414"         # sidebars, inputs
panel = "#1a1a1a"
foreground = "#dcdcdc"
dim = "#7d7d7d"             # secondary text
line = "#333333"            # bubble borders
log_bg = "#0b0b0b"          # log panes
input_bg = "#101010"        # option lists, trees
code_theme = "monokai"      # any rich pygments theme
code_bg = "#121212"
show_thinking = true        # show model reasoning by default
poll_running = 4.0          # seconds between "loaded models" refreshes

[ollama]
host = ""                   # empty -> $OLLAMA_HOST, else 127.0.0.1:11434
keep_alive = ""             # default keep_alive for new chats, e.g. "5m"

[cheats]
dir = "~/.harpy"            # markdown cheat sheets

[packages]
helper = "auto"             # auto|yay|paru|none   (AUR helper for arc)
jobs = ""                   # empty -> $XDG_STATE_HOME/sys/jobs

[web]
host = "127.0.0.1"          # bind 0.0.0.0 only behind a trusted proxy
port = 8787
allow_root_actions = true   # allow install/remove/chat-run from the web UI
open_browser = false
title = "sys"
assets = ""                 # dir with index.html / app.js / app.css overrides
readme = ""                 # markdown shown on the web landing page

[data]
dir = ""                    # empty -> $XDG_DATA_HOME/sys  (chats.json lives here)
"""


# palette, filled from [app] at startup

BG = DEFAULTS["app"]["background"]
SURFACE = DEFAULTS["app"]["surface"]
PANEL = DEFAULTS["app"]["panel"]
ACCENT = DEFAULTS["app"]["accent"]
DIM = DEFAULTS["app"]["dim"]
FG = DEFAULTS["app"]["foreground"]
CODE_THEME = DEFAULTS["app"]["code_theme"]
CODE_BG = DEFAULTS["app"]["code_bg"]


def apply_theme(cfg: Config) -> None:
    """Copy [app] colours into the module globals the widgets read."""
    global BG, SURFACE, PANEL, ACCENT, DIM, FG, CODE_THEME, CODE_BG
    app = cfg.section("app")
    BG = app.get("background", BG)
    SURFACE = app.get("surface", SURFACE)
    PANEL = app.get("panel", PANEL)
    ACCENT = app.get("accent", ACCENT)
    DIM = app.get("dim", DIM)
    FG = app.get("foreground", FG)
    CODE_THEME = app.get("code_theme", CODE_THEME)
    CODE_BG = app.get("code_bg", CODE_BG)


def theme_variables(cfg: Config) -> dict[str, str]:
    """Extra CSS variables for the hard-to-theme parts of the stylesheet."""
    app = cfg.section("app")
    return {
        "sys-line": app.get("line", "#333333"),
        "sys-dim": DIM,
        "sys-log-bg": app.get("log_bg", "#0b0b0b"),
        "sys-input-bg": app.get("input_bg", "#101010"),
        "sys-code-bg": CODE_BG,
    }


def build_theme(cfg: Config):
    """textual Theme built from [app]; falls back to a builtin one."""
    try:
        from textual.theme import Theme
    except ModuleNotFoundError:
        return None
    app = cfg.section("app")
    return Theme(
        name=app.get("theme", "sys-dark"),
        primary=ACCENT,
        secondary=ACCENT,
        accent=ACCENT,
        warning="#ffb454",
        error="#ff5f5f",
        success="#5fd75f",
        background=BG,
        surface=SURFACE,
        panel=PANEL,
        foreground=FG,
        dark=True,
        variables=theme_variables(cfg),
    )


# helpers


def expand_path(raw: str) -> str:
    """`~/x` and `$HOME/x` -> an absolute path string."""
    return str(Path(os.path.expandvars(str(raw))).expanduser())


def data_dir(cfg: Config) -> Path:
    """Where chats.json and friends live."""
    return Path(cfg.get("data.dir") or (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        / APP_NAME
    ))


def jobs_dir(cfg: Config) -> Path:
    """Where detached package/command jobs keep their logs."""
    raw = cfg.get("packages.jobs")
    if raw:
        return Path(raw)
    return (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / APP_NAME
        / "jobs"
    )


def cheat_dir(cfg: Config) -> Path:
    return Path(cfg.get("cheats.dir", "~/.harpy"))


def normalize_host(raw: Optional[str]) -> str:
    """Accept `1.2.3.4`, `host:port`, `http://host` -> a base URL."""
    raw = (raw or "127.0.0.1:11434").strip()
    if "://" not in raw:
        raw = "http://" + raw
    rest = raw.split("://", 1)[1].split("/")[0]
    if ":" not in rest:
        raw = raw.rstrip("/") + ":11434"
    return raw.rstrip("/")


def ollama_host(cfg: Config) -> str:
    return normalize_host(
        cfg.get("ollama.host") or os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    )


def fmt_bytes(n: Any) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_dur(s: Any) -> str:
    s = float(s or 0)
    if s < 1:
        return f"{s * 1000:.0f} ms"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def ago(s: Optional[str]) -> str:
    """'3 h ago' style relative time."""
    ts = parse_ts(s)
    if ts is None:
        return "—"
    d = max(0.0, time.time() - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 86400 * 30:
        return f"{int(d // 86400)} d ago"
    if d < 86400 * 365:
        return f"{int(d // (86400 * 30))} mo ago"
    return f"{int(d // (86400 * 365))} y ago"


def shorten(text: str, n: int = 40) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "New chat"
    return line if len(line) <= n else line[: n - 1] + "…"


def slugify(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "chat"


def model_name_from_path(p: Path) -> str:
    stem = p.stem if p.is_file() else p.name
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-")


def parse_keep_alive(raw: str) -> Any:
    """'5m' stays a string, '300' becomes an int, '-1' stays -1."""
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return raw


def norm(s: str) -> str:
    """Lowercase alphanumerics only - the fuzzy-match key."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


FENCE = re.compile(
    r"^[ \t]*```[ \t]*([\w+#.\-]*)[^\n]*\n(.*?)(?:^[ \t]*```[ \t]*$|\Z)",
    re.M | re.S,
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """(lang, code) for every fenced block in `text`."""
    return [
        ((m.group(1) or "text"), m.group(2).rstrip("\n")) for m in FENCE.finditer(text)
    ]


def last_segment(line: str) -> str:
    """Terminal-style: text after the last carriage return."""
    parts = [p for p in line.split("\r") if p.strip()]
    return parts[-1] if parts else ""


def trim_lines(s: str, n: int) -> str:
    lines = s.strip().splitlines()
    return "\n".join(lines[:n]) + (
        f"\n… (+{len(lines) - n} lines)" if len(lines) > n else ""
    )


def system_copy(text: str) -> bool:
    """Put `text` on the system clipboard with whatever tool exists."""
    cands: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        cands.append(["wl-copy"])
    cands += [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ]
    for cmd in cands:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(
                    cmd,
                    input=text.encode(),
                    timeout=3,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                continue
    return False


def run_cmd(cmd: list[str], timeout: float = 60) -> list[str]:
    """Run a command, return stdout words; [] on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.split() if r.returncode == 0 else []
    except Exception:
        return []


def output_of(cmd: list[str], timeout: float = 60) -> str:
    """Run a command, return its stdout as text ('' on failure)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def find_llama_cpp() -> str:
    """Guess where llama.cpp is checked out."""
    env = os.environ.get("LLAMA_CPP_DIR")
    cands = [Path(env)] if env else []
    cands += [
        Path.home() / "llama.cpp",
        Path.home() / "src" / "llama.cpp",
        Path("/opt/llama.cpp"),
    ]
    for c in cands:
        if (c / "convert_hf_to_gguf.py").exists():
            return str(c)
    which = shutil.which("convert_hf_to_gguf.py")
    return (
        str(Path(which).parent)
        if which
        else str(cands[0] if cands else Path.home() / "llama.cpp")
    )


def inspect_source(p: str) -> tuple[str, str]:
    """Classify a model folder/file: (kind, human description)."""
    path = Path(p).expanduser()
    if path.is_file():
        if path.suffix.lower() == ".gguf":
            return "gguf", f"GGUF file · {fmt_bytes(path.stat().st_size)}"
        return "bad", "Not a .gguf file"
    if path.is_dir():
        st = list(path.glob("*.safetensors"))
        gg = sorted(path.glob("*.gguf"))
        bits = []
        if st:
            bits.append(f"{len(st)} .safetensors")
        if gg:
            bits.append(f"{len(gg)} .gguf")
        if (path / "config.json").exists():
            bits.append("config.json")
        if any((path / n).exists() for n in ("tokenizer.json", "tokenizer.model")):
            bits.append("tokenizer")
        kind = "safetensors" if st else ("gguf-dir" if gg else "dir")
        return kind, "Folder · " + (", ".join(bits) if bits else "no model files found")
    return "none", "Path not found"


def open_in_editor(path: Path) -> bool:
    """Open a file in $EDITOR; False when there is no usable editor."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    parts = shlex.split(editor)
    if not parts or not shutil.which(parts[0]):
        return False
    try:
        subprocess.Popen([*parts, str(path)])
    except OSError:
        return False
    return True


# cheats
# Markdown cheat sheets: `# tool` / `## category` / `command  # description`.


TOOL_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
CATEGORY_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
CHEAT_LINE_RE = re.compile(r"^(?P<command>.*?\S)\s{2,}#\s*(?P<description>.*)$")
DEFAULT_CATEGORY = "general"


@dataclass
class Cheat:
    """One cheat-sheet line."""

    tool: str
    category: str
    command: str
    description: str
    source: Path


@dataclass
class CheatMatch:
    cheat: Cheat
    score: float


def parse_sheet(path: Path) -> list[Cheat]:
    """Read one markdown sheet into cheats."""
    tool, category = path.stem, DEFAULT_CATEGORY
    entries: list[Cheat] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line, stripped = raw.rstrip(), raw.strip()
        if not stripped:
            continue
        if m := TOOL_HEADING_RE.match(line):
            tool = m.group(1).strip()
            continue
        if m := CATEGORY_HEADING_RE.match(line):
            category = m.group(1).strip()
            continue
        if stripped.startswith("#"):
            continue  # comment/note line, not a cheat
        if m := CHEAT_LINE_RE.match(line):
            command, description = m.group("command").strip(), m.group("description")
        else:
            command, description = stripped, ""
        entries.append(Cheat(tool, category, command, description.strip(), path))
    return entries


def load_cheats(directory: Path) -> list[Cheat]:
    """Every cheat in every *.md sheet, sorted by filename."""
    if not directory.exists():
        return []
    out: list[Cheat] = []
    for path in sorted(directory.glob("*.md")):
        try:
            out.extend(parse_sheet(path))
        except OSError:
            continue
    return out


def cheat_tools(entries: Iterable[Cheat]) -> list[str]:
    return sorted({c.tool for c in entries})


WORD_RE = re.compile(r"[a-z0-9]+")
COVERAGE_FLOOR = 0.5
FUZZY_MIN_SCORE = 70.0


def _words(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def cheat_search(entries: list[Cheat], query: str, limit: int = 40) -> list[CheatMatch]:
    """Rank cheats for `query`, best first.

    Two modes: when the first word exactly names a known tool ("gh repo") every
    command of that tool/category wins; otherwise words are matched against
    tool/category/command/description with a typo-tolerant fallback.
    """
    if not query.strip():
        return [CheatMatch(c, 0.0) for c in entries[:limit]]

    query = query.lower()
    all_words = query.split()
    words = [w for w in all_words if len(w) >= 3] or all_words  # "a"/"to" are noise

    scoped = _scoped_cheats(entries, all_words)
    if scoped is not None:
        return [CheatMatch(c, 100.0) for c in scoped[:limit]]

    scored: list[CheatMatch] = []
    for c in entries:
        blob = f"{c.tool} {c.category} {c.command} {c.description}"
        entry_words = _words(blob)
        hits = sum(
            1
            for w in words
            if w in entry_words or (len(w) >= 4 and any(w in ew for ew in entry_words))
        )
        coverage = hits / len(words)
        if coverage >= COVERAGE_FLOOR:
            score = 60.0 + 40.0 * coverage
        else:
            content = f"{c.command} {c.description}"
            score = fuzz.WRatio(query, content)
            if score < FUZZY_MIN_SCORE:
                continue
        scored.append(CheatMatch(c, score))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:limit]


def _scoped_cheats(entries: list[Cheat], words: list[str]) -> Optional[list[Cheat]]:
    tools = {c.tool.lower() for c in entries}
    if not words or words[0] not in tools:
        return None
    tool, rest = words[0], words[1:]
    if not rest:
        return [c for c in entries if c.tool.lower() == tool]
    tail = " ".join(rest)
    hits = [
        c for c in entries if c.tool.lower() == tool and tail in c.category.lower()
    ]
    return hits or None


# sheet mutations


def sheet_path(directory: Path, tool: str) -> Path:
    """The .md file that holds `tool`'s cheats."""
    return directory / f"{tool}.md"


def sheet_new(directory: Path, tool: str) -> Path:
    """Create an empty sheet (returns the existing one if present)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = sheet_path(directory, tool)
    if not path.exists():
        path.write_text(f"# {tool}\n\n## {DEFAULT_CATEGORY}\n", encoding="utf-8")
    return path


def cheat_add(
    directory: Path,
    tool: str,
    command: str,
    description: str = "",
    category: Optional[str] = None,
) -> tuple[Path, bool, str]:
    """Append a cheat, creating the file/section as needed -> (path, created, cat)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = sheet_path(directory, tool)
    created = not path.exists()
    if created:
        cat = category or DEFAULT_CATEGORY
        lines = [f"# {tool}", "", f"## {cat}"]
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
        cat = category or _last_category(lines) or DEFAULT_CATEGORY
        lines = _insert_cheat(lines, cat, _format_cheat(command, description))
    if created:
        lines.append(_format_cheat(command, description))
    _write_lines(path, lines)
    return path, created, cat


def cheat_delete_cmd(directory: Path, tool: str, command: str) -> tuple[Path, int]:
    """Drop every line whose command equals `command` -> (path, n_removed)."""
    path = sheet_path(directory, tool)
    if not path.exists():
        return path, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    needle = command.strip().lower()
    kept = [ln for ln in lines if not _is_command_line(ln, needle)]
    removed = len(lines) - len(kept)
    if removed:
        _write_lines(path, kept)
    return path, removed


def cheat_delete_cat(directory: Path, tool: str, category: str) -> tuple[Path, int]:
    """Remove a whole `## category` section -> (path, n_removed)."""
    path = sheet_path(directory, tool)
    if not path.exists():
        return path, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    needle = category.strip().lower()
    kept: list[str] = []
    removed = 0
    in_section = False
    for ln in lines:
        if in_section:
            if _is_heading(ln):
                in_section = False
            else:
                removed += 1
                continue
        cat = _category_heading(ln)
        if cat is not None and cat.lower() == needle:
            in_section = True
            removed += 1
            continue
        kept.append(ln)
    if removed:
        _write_lines(path, kept)
    return path, removed


def sheet_delete(directory: Path, tool: str) -> tuple[Path, bool]:
    """Remove a whole sheet -> (path, deleted)."""
    path = sheet_path(directory, tool)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def sheet_edit(directory: Path, tool: str) -> tuple[Path, bool]:
    """Open a sheet in $EDITOR, creating it if needed -> (path, created)."""
    path = sheet_new(directory, tool)
    return path, open_in_editor(path)


def _category_heading(line: str) -> Optional[str]:
    m = CATEGORY_HEADING_RE.match(line)
    return m.group(1).strip() if m else None


def _is_heading(line: str) -> bool:
    return bool(TOOL_HEADING_RE.match(line) or CATEGORY_HEADING_RE.match(line))


def _last_category(lines: list[str]) -> Optional[str]:
    last = None
    for ln in lines:
        if cat := _category_heading(ln):
            last = cat
    return last


def _cheat_command(line: str) -> Optional[str]:
    """The command part of a cheat line, or None if it isn't one."""
    m = CHEAT_LINE_RE.match(line)
    if m:
        return m.group("command").strip()
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped


def _is_command_line(line: str, needle: str) -> bool:
    cmd = _cheat_command(line)
    return cmd is not None and cmd.lower() == needle


def _format_cheat(command: str, description: str) -> str:
    command, description = command.strip(), description.strip()
    return f"{command}  # {description}" if description else command


def _insert_cheat(lines: list[str], category: str, cheat: str) -> list[str]:
    """`lines` with `cheat` appended to the `category` section."""
    cat_idx = next(
        (i for i, ln in enumerate(lines) if _category_heading(ln) == category), None
    )
    out = list(lines)
    if cat_idx is None:
        if out and out[-1]:
            out.append("")
        return out + [f"## {category}", cheat]
    end = next(
        (i for i in range(cat_idx + 1, len(lines)) if _is_heading(lines[i])),
        len(lines),
    )
    while end - 1 > cat_idx and not lines[end - 1].strip():
        end -= 1
    out.insert(end, cheat)
    return out


def _write_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines).rstrip()
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def cheat_script(cheat: Cheat) -> str:
    """The shell line to run for a cheat (its command, verbatim)."""
    return cheat.command.strip()


def render_cheats(matches: list[CheatMatch], query: str) -> RenderableType:
    """Grouped tables for the one-shot `sys --cheat` output."""
    if not matches:
        return Text.from_markup(
            f"[yellow]No cheats found for[/yellow] [bold]{query!r}[/bold]\n"
            "Try a shorter query, or add a sheet under ~/.harpy/"
        )
    from itertools import groupby

    parts: list[RenderableType] = []
    entries = sorted((m.cheat for m in matches), key=lambda c: (c.tool, c.category))
    for (tool, category), group in groupby(entries, key=lambda c: (c.tool, c.category)):
        table = Table(
            title=f"[bold cyan]{tool}[/bold cyan] [dim]›[/dim] [bold]{category}[/bold]",
            title_justify="left",
            show_header=False,
            box=None,
            padding=(0, 2, 0, 0),
        )
        table.add_column("command", style="bold green")
        table.add_column("description", style="dim")
        for c in group:
            # Text (not raw str) so "[name]" placeholders are not parsed as markup.
            table.add_row(Text(c.command), Text(c.description))
        parts += [table, Text("")]
    return Group(*parts)


# packages
# pacman/AUR: repo+installed index, fuzzy name resolution, detached jobs.

IS_ROOT = getattr(os, "geteuid", lambda: 1)() == 0
SUDO: list[str] = [] if IS_ROOT else ["sudo", "-A"]
AUR_EXTRA = {
    "yay": ["--answerdiff", "None", "--answerclean", "None", "--answeredit", "None"],
    "paru": ["--skipreview"],
}
PKG_ACTIONS = (
    "install",
    "remove",
    "update",
    "orphans",
    "clean",
    "check",
    "search",
)


def aur_helper(cfg: Config) -> Optional[str]:
    """Which AUR helper to use: config, else yay/paru if installed."""
    want = str(cfg.get("packages.helper", "auto")).strip().lower()
    if want in ("none", "off", "false", "0"):
        return None
    if want != "auto":
        return want if shutil.which(want) else None
    if IS_ROOT:  # helpers refuse to run as root
        return None
    return next((h for h in ("yay", "paru") if shutil.which(h)), None)


def pac(*args: str) -> str:
    """`sudo pacman <args>` as a shell string."""
    return shlex.join([*SUDO, "pacman", *args])


def aurcmd(helper: str, *args: str) -> str:
    """`yay/paru <args>` with the flags that keep it non-interactive."""
    return shlex.join([helper, "--noconfirm", "--sudoflags", "-A", *AUR_EXTRA[helper], *args])


class PkgIndex:
    """Every known package name, mapped for fast fuzzy lookup."""

    def __init__(self) -> None:
        self.repo: set[str] = set()
        self.installed: set[str] = set()
        self.keys: dict[str, list[str]] = {}

    def refresh(self) -> "PkgIndex":
        """Rebuild from `pacman -Slq` + `pacman -Qq`."""
        repo, inst, keys = set(run_cmd(["pacman", "-Slq"])), set(run_cmd(["pacman", "-Qq"])), {}
        for p in repo | inst:
            keys.setdefault(norm(p), []).append(p)
        self.repo, self.installed, self.keys = repo, inst, keys
        return self

    def match(self, term: str, pool: Optional[set[str]] = None, deep: bool = False) -> list[str]:
        """Ranked candidates: same letters, then substring, then typos."""
        pool = self.repo | self.installed if pool is None else pool
        n = norm(term)
        if not n:
            return []
        hits = [p for p in self.keys.get(n, []) if p in pool]
        near = []
        if len(n) >= 2:
            near = [p for k, ps in self.keys.items() if n in k for p in ps if p in pool and p not in hits]
            near.sort(key=lambda p: (not p.startswith(term.lower()), len(p)))
        out = hits + near[:8]
        if deep and len(out) < 3:
            for k in difflib.get_close_matches(n, self.keys, n=5, cutoff=0.75):
                out += [p for p in self.keys[k] if p in pool and p not in out]
        return out[:8]


def aur_names(term: str) -> list[str]:
    """Ask the AUR RPC: exact probes with dash variants + suggestions."""
    t = term.lower()
    variants = ({t} | {t[:i] + "-" + t[i:] for i in range(1, len(t))}) if len(t) <= 40 else {t}

    def get(url: str) -> Any:
        with urlopen(url, timeout=5) as r:  # noqa: S310 - fixed https host
            return json.load(r)

    found: list[str] = []
    try:
        q = "&".join("arg[]=" + quote(v) for v in sorted(variants))
        found += [
            x["Name"]
            for x in get(f"https://aur.archlinux.org/rpc/v5/info?{q}").get("results", [])
        ]
        found += [
            s
            for s in get(f"https://aur.archlinux.org/rpc/v5/suggest/{quote(t)}")
            if s not in found
        ]
    except Exception:
        pass
    found.sort(key=lambda p: (p != t, norm(p) != norm(t)))
    return found


def resolve_install(idx: PkgIndex, tok: str, helper: Optional[str]) -> tuple:
    """-> ("ok", name, "repo"|"aur") | ("pick", [(name, src)]) | ("none",)"""
    if tok in idx.repo:
        return ("ok", tok, "repo")
    if tok in idx.installed and helper:
        return ("ok", tok, "aur")
    n = norm(tok)
    loc = idx.match(tok, idx.repo, deep=True)
    eq = [p for p in loc if norm(p) == n]
    if len(eq) == 1:
        return ("ok", eq[0], "repo")
    aur = aur_names(tok) if helper else []
    if tok in aur:
        return ("ok", tok, "aur")
    aeq = [p for p in aur if norm(p) == n]
    if not eq and len(aeq) == 1:
        return ("ok", aeq[0], "aur")
    opts = [(p, "repo") for p in loc] + [(p, "aur") for p in aur if p not in loc]
    return ("pick", opts[:10]) if opts else ("none",)


def resolve_remove(idx: PkgIndex, tok: str) -> tuple:
    """-> ("ok", name, "local") | ("pick", [(name, "local")]) | ("none",)"""
    if tok in idx.installed:
        return ("ok", tok, "local")
    cands = idx.match(tok, idx.installed, deep=True)
    eq = [p for p in cands if norm(p) == norm(tok)]
    if len(eq) == 1:
        return ("ok", eq[0], "local")
    return ("pick", [(p, "local") for p in cands]) if cands else ("none",)


def build_install_script(picks: list[tuple[str, str]], helper: Optional[str]) -> str:
    """`pacman -S` for repo packages, then the helper for AUR ones."""
    repo = [n for n, s in picks if s == "repo"]
    aur = [n for n, s in picks if s == "aur"]
    parts = []
    if repo:
        # no -y: avoids partial upgrades on Arch
        parts.append(pac("-S", "--needed", "--noconfirm", *repo))
    if aur and helper:
        parts.append(aurcmd(helper, "-S", "--needed", *aur))
    return " && ".join(parts)


def build_remove_script(names: list[str]) -> str:
    """`pacman -Rns` - remove with deps and now-unneeded configs."""
    return pac("-Rns", "--noconfirm", *names)


def pkg_plan(
    cfg: Config,
    action: str,
    names: Optional[list[str]] = None,
    helper: Optional[str] = None,
) -> Optional[tuple[str, str, bool]]:
    """Command for a package action -> (title, script, needs_root) or None."""
    names = names or []
    helper = aur_helper(cfg) if helper is None else helper
    if action == "update":
        parts = [pac("-Syu", "--noconfirm")] + ([aurcmd(helper, "-Sua")] if helper else [])
        return ("update system", " && ".join(parts), True)
    if action == "orphans":
        return ("remove orphans", pac("-Rns", "--noconfirm", *run_cmd(["pacman", "-Qdtq"])), True)
    if action == "clean":
        cmd = (
            shlex.join([*SUDO, "paccache", "-rk2"])
            if shutil.which("paccache")
            else pac("-Sc", "--noconfirm")
        )
        return ("clean cache", cmd, True)
    if action == "check":
        base = "checkupdates" if shutil.which("checkupdates") else "pacman -Qu"
        cmd = base + (f"; {shlex.join([helper, '-Qua'])}" if helper else "")
        return ("check updates", cmd + "; true", False)
    if action == "search" and names:
        cmd = shlex.join(["pacman", "-Ss", *names])
        if helper:
            cmd += f"; {shlex.join([helper, '-Ss', *names])}"
        return ("search " + " ".join(names), cmd + "; true", False)
    return None


def pkg_counts(idx: PkgIndex) -> dict[str, int]:
    """Numbers for the tab title / web header."""
    return {
        "installed": len(idx.installed),
        "repo": len(idx.repo),
        "orphans": len(run_cmd(["pacman", "-Qdtq"])),
        "updates": len(run_cmd(["pacman", "-Qu"])),
    }


# detached jobs: own session, log + rc files on disk


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def sudo_nopass() -> bool:
    """True when sudo works with no password."""
    return subprocess.run(["sudo", "-n", "-k", "true"], capture_output=True).returncode == 0


def sudo_ok(pw: str) -> bool:
    """Check a typed password without echoing it."""
    try:
        r = subprocess.run(
            ["sudo", "-S", "-k", "-v"], input=pw + "\n", capture_output=True,
            text=True, timeout=30,
        )
    except Exception:
        return False
    return r.returncode == 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def new_job(cfg: Config, title: str, script: str, password: Optional[str] = None) -> str:
    """Start `script` detached; returns its job id."""
    root = jobs_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while (root / jid).exists():
        n += 1
        jid = time.strftime("%Y%m%d-%H%M%S") + f"-{n}"
    d = root / jid
    d.mkdir(parents=True)

    env = os.environ.copy()
    askpass = None
    if password is not None:
        # sudo -A reads the password from this 0700 helper, deleted at job end
        askpass = runtime_dir() / f"{APP_NAME}-askpass-{jid}"
        fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(password) + "\n")
        env["SUDO_ASKPASS"] = str(askpass)
    rm = f"rm -f {shlex.quote(str(askpass))}; " if askpass else ""
    wrapper = (
        f"trap '' HUP; printf '$ %s\\n' {shlex.quote(script)}; {script}; rc=$?; "
        f'echo "[exit $rc]"; echo $rc > {shlex.quote(str(d / "rc"))}; {rm}exit $rc'
    )
    with open(d / "log", "ab") as log:
        p = subprocess.Popen(
            ["sh", "-c", wrapper], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd=Path.home(),
        )
    (d / "meta.json").write_text(
        json.dumps({"title": title, "cmd": script, "start": time.time(), "pid": p.pid})
    )
    return jid


def job_info(cfg: Config, jid: str) -> Optional[dict]:
    """Status of one job (name or path), or None when its files are unreadable."""
    d = jobs_dir(cfg) / Path(jid).name
    try:
        m = json.loads((d / "meta.json").read_text())
        rc = (d / "rc").read_text().strip() if (d / "rc").exists() else None
    except (OSError, ValueError):
        return None
    if rc is not None:
        st = "ok" if rc == "0" else f"failed ({rc})"
    elif pid_alive(m["pid"]):
        st = "running"
    else:
        st = "killed"
    return {
        "id": d.name,
        "st": st,
        "title": m["title"],
        "cmd": m.get("cmd", ""),
        "pid": m["pid"],
        "time": time.strftime("%H:%M:%S", time.localtime(m["start"])),
        "start": m["start"],
    }


def jobs_list(cfg: Config, limit: int = 40) -> list[dict]:
    """Newest jobs first."""
    root = jobs_dir(cfg)
    if not root.exists():
        return []
    out = [i for i in map(lambda j: job_info(cfg, j), sorted(root.glob("*"), reverse=True)[:limit]) if i]
    return out


def job_kill(cfg: Config, jid: str) -> bool:
    """SIGTERM a running job's whole process group."""
    info = job_info(cfg, jid)
    if not info or info["st"] != "running":
        return False
    try:
        os.killpg(info["pid"], signal.SIGTERM)
    except OSError:
        return False
    return True


def jobs_clear(cfg: Config) -> int:
    """Delete every finished job directory; returns how many went."""
    n = 0
    for d in jobs_dir(cfg).glob("*"):
        info = job_info(cfg, d.name)
        if info and info["st"] != "running":
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def job_read_log(cfg: Config, jid: str, offset: int, running: bool) -> tuple[str, int]:
    """New log text from `offset` -> (cleaned text, new offset).

    While a job runs only whole lines are returned, so the log never shows a
    half-written progress line.
    """
    try:
        with open(jobs_dir(cfg) / jid / "log", "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return "", offset
    if running:
        data = data[: data.rfind(b"\n") + 1]
    if not data:
        return "", offset
    text = strip_ansi(data.decode(errors="replace")).replace("\r\n", "\n")
    return (text[:-1] if text.endswith("\n") else text), offset + len(data)


# ollama

OPTION_FIELDS: list[tuple[str, type]] = [
    ("temperature", float),
    ("top_p", float),
    ("top_k", int),
    ("num_ctx", int),
    ("num_predict", int),
    ("repeat_penalty", float),
    ("seed", int),
]
OPTION_TYPES = dict(OPTION_FIELDS)

CHAT_COMMANDS = {
    "help", "new", "clear", "undo", "regen", "model", "models", "pull",
    "system", "rename", "delete", "export", "copy", "image", "set", "think",
    "settings", "stats", "cheat", "pkg",
}

HELP = """\
# sys

Ollama chat, pacman/AUR packages and markdown cheat sheets in one TUI.

## Tabs
| Key | Tab |
|---|---|
| **Ctrl+1** | Chat |
| **Ctrl+2** | Models |
| **Ctrl+3** | Cheats |
| **Ctrl+4** | Packages |
| **Ctrl+5** | Jobs |
| **F2 / F3** | Chat / Models |

## Chat
| Key | Action |
|---|---|
| **Enter** | send message |
| **Ctrl+J** / Shift+Enter | newline in message |
| **Esc** | stop generating |
| **Ctrl+N** | new chat |
| **Ctrl+O** | switch model for this chat |
| **Ctrl+R** | regenerate last answer |
| **Ctrl+Y** | copy last code block |
| **Ctrl+S** | export chat to Markdown |
| **Ctrl+T** | show / hide model thinking |
| **F4** | chat settings (system prompt, temperature, num_ctx, …) |
| **F6** | rename chat |

## Models tab
`p` pull · `n` create from folder/.gguf · `r` rename · `c` copy · `d` delete ·
`u` unload · `Enter` use in chat · `F5` refresh

## Cheats tab
Type to fuzzy-search `~/.harpy`. `Enter` run the command as a detached job ·
`y` copy · `a` add · `d` delete · `e` edit sheet · `n` new sheet

## Packages tab
Type a package name, `Enter` installs. With the jobs list focused:
`i` install · `r` remove · `u` update all · `o` orphans · `c` clean cache ·
`k` check updates · `s` search · `Esc` back to the input

## Jobs tab
`x` kill selected job · `d` delete finished jobs · `F5` refresh

## Slash commands
`/new` `/clear` `/undo` `/regen` `/model [name]` `/models` `/pull name`
`/system [text|-]` `/rename title` `/delete` `/export [path]` `/copy [n|all]`
`/image path` `/think auto|on|off` `/cheat query` `/pkg name` `/settings`
`/stats` `/help`

`/set temperature 0.7` (also top_p, top_k, num_ctx, num_predict,
repeat_penalty, seed, keep_alive)
"""

WELCOME = (
    "Start typing below and press Enter.\n\n"
    "  Ctrl+3 cheats   Ctrl+4 packages   Ctrl+5 jobs   Ctrl+2 models\n"
    "  /help  all keys & commands\n"
)


class OllamaError(Exception):
    pass


class Ollama:
    """Tiny async client for the Ollama REST API."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.http = httpx.AsyncClient(
            base_url=host, timeout=httpx.Timeout(None, connect=5.0)
        )

    async def _req(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        r = await self.http.request(method, path, json=payload)
        if r.status_code >= 400:
            raise OllamaError(self._err(r.text, r.status_code))
        return r.json() if r.content else {}

    @staticmethod
    def _err(raw: str, code: int) -> str:
        try:
            msg = json.loads(raw).get("error", raw)
        except Exception:
            msg = raw
        return f"{code}: {str(msg).strip()}"

    async def version(self) -> str:
        return (await self._req("GET", "/api/version")).get("version", "?")

    async def tags(self) -> list[dict]:
        return (await self._req("GET", "/api/tags")).get("models", [])

    async def ps(self) -> list[dict]:
        return (await self._req("GET", "/api/ps")).get("models", [])

    async def show(self, name: str) -> dict:
        return await self._req("POST", "/api/show", {"model": name})

    async def copy(self, src: str, dst: str) -> None:
        await self._req("POST", "/api/copy", {"source": src, "destination": dst})

    async def delete(self, name: str) -> None:
        await self._req("DELETE", "/api/delete", {"model": name})

    async def unload(self, name: str) -> None:
        await self._req("POST", "/api/generate", {"model": name, "keep_alive": 0})

    async def stream(self, path: str, payload: dict) -> AsyncIterator[dict]:
        """Yield every JSON object of a streaming endpoint."""
        async with self.http.stream("POST", path, json=payload) as r:
            if r.status_code >= 400:
                raw = (await r.aread()).decode("utf-8", "replace")
                raise OllamaError(self._err(raw, r.status_code))
            async for line in r.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                yield obj


class ChatStore:
    """chats.json: every chat plus global meta (last model, defaults)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dir = data_dir(cfg)
        self.path = self.dir / "chats.json"
        self.chats: dict[str, dict] = {}
        self.meta: dict[str, Any] = {
            "last_model": "",
            "active": "",
            "defaults": {"system": "", "options": {}, "think": "auto", "keep_alive": ""},
        }
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.migrate()
            return
        self.chats = {c["id"]: c for c in data.get("chats", []) if "id" in c}
        self.meta.update(data.get("meta", {}))

    def migrate(self) -> None:
        """One-time import of chats written by the old om-tui."""
        old = Path(
            os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
        ) / "ollama-tui" / "chats.json"
        try:
            data = json.loads(old.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.chats = {c["id"]: c for c in data.get("chats", []) if "id" in c}
        self.meta.update(data.get("meta", {}))
        self.save()

    def save(self) -> None:
        """Atomic write; never raise (a TUI should not die on a full disk)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"meta": self.meta, "chats": list(self.chats.values())},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            pass

    def new_chat(self, model: str = "") -> dict:
        """Create, register and return a fresh chat dict."""
        d = self.meta.get("defaults") or {}
        now = time.time()
        chat = {
            "id": uuid.uuid4().hex[:10],
            "title": "New chat",
            "model": model,
            "system": d.get("system", ""),
            "options": dict(d.get("options") or {}),
            "think": d.get("think", "auto"),
            "keep_alive": d.get("keep_alive") or self.cfg.get("ollama.keep_alive", ""),
            "messages": [],
            "created": now,
            "updated": now,
        }
        self.chats[chat["id"]] = chat
        return chat

    def sorted_chats(self) -> list[dict]:
        return sorted(self.chats.values(), key=lambda c: c.get("updated", 0), reverse=True)

    def token_totals(self, chat: dict) -> tuple[int, int]:
        """(prompt tokens, generated tokens) summed over a chat."""
        up = down = 0
        for m in chat.get("messages", []):
            s = m.get("stats") or {}
            up += s.get("prompt_tokens") or 0
            down += s.get("gen_tokens") or 0
        return up, down


def build_payload(chat: dict, images: Optional[list[str]] = None) -> dict:
    """Turn a stored chat into an /api/chat request body."""
    msgs: list[dict] = []
    if chat.get("system"):
        msgs.append({"role": "system", "content": chat["system"]})
    for m in chat.get("messages", []):
        if m.get("role") not in ("user", "assistant"):
            continue
        item: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
        if m.get("images"):
            encoded = encode_images(m["images"])
            if encoded:
                item["images"] = encoded
        msgs.append(item)
    payload: dict[str, Any] = {
        "model": chat["model"],
        "messages": msgs,
        "stream": True,
    }
    if chat.get("options"):
        payload["options"] = dict(chat["options"])
    if chat.get("think") == "on":
        payload["think"] = True
    elif chat.get("think") == "off":
        payload["think"] = False
    if str(chat.get("keep_alive", "")).strip():
        payload["keep_alive"] = parse_keep_alive(str(chat["keep_alive"]))
    return payload


def encode_images(paths: Iterable[str]) -> list[str]:
    """base64 images for the API, skipping unreadable ones."""
    out = []
    for p in paths:
        try:
            out.append(base64.b64encode(Path(p).read_bytes()).decode())
        except OSError:
            continue
    return out


def consume_chunk(obj: dict, acc: dict[str, str]) -> bool:
    """Fold one streamed object into the accumulating text."""
    msg = obj.get("message") or {}
    piece = False
    if msg.get("thinking"):
        acc["thinking"] += msg["thinking"]
        piece = True
    if msg.get("content"):
        acc["content"] += msg["content"]
        piece = True
    return piece


def build_stats(final: dict, chunks: int, elapsed: float, ttft: Optional[float]) -> Optional[dict]:
    """Per-answer stats, preferring the numbers Ollama reports."""
    if final:
        ec, ed = final.get("eval_count"), final.get("eval_duration") or 0
        pc, pd = final.get("prompt_eval_count"), final.get("prompt_eval_duration") or 0
        return {
            "prompt_tokens": pc,
            "gen_tokens": ec if ec is not None else chunks,
            "tps": (ec / (ed / 1e9)) if ec and ed else (chunks / elapsed if elapsed else 0.0),
            "prompt_tps": (pc / (pd / 1e9)) if pc and pd else None,
            "total_s": (final.get("total_duration") or 0) / 1e9 or elapsed,
            "load_s": (final.get("load_duration") or 0) / 1e9,
            "ttft_s": ttft,
            "done_reason": final.get("done_reason"),
        }
    if chunks:
        return {
            "prompt_tokens": None,
            "gen_tokens": chunks,
            "tps": chunks / elapsed if elapsed else 0.0,
            "total_s": elapsed,
            "load_s": 0,
            "ttft_s": ttft,
        }
    return None


def store_answer(
    chat: dict,
    content: str,
    thinking: str,
    stats: Optional[dict],
    stopped: bool = False,
) -> bool:
    """Append an assistant turn to a chat; returns True when something was stored."""
    if not (content or thinking):
        return False
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    if stats:
        msg["stats"] = stats
    if stopped:
        msg["stopped"] = True
    chat["messages"].append(msg)
    chat["updated"] = time.time()
    return True


def chat_markdown(chat: dict) -> str:
    """Render a chat as Markdown for export."""
    out = [
        f"# {chat.get('title', 'Chat')}",
        "",
        f"*Model: {chat.get('model')} · exported {datetime.now():%Y-%m-%d %H:%M}*",
        "",
    ]
    if chat.get("system"):
        out += ["> **System:** " + chat["system"].replace("\n", "\n> "), ""]
    for m in chat.get("messages", []):
        if m["role"] == "user":
            out += ["## You", "", m.get("content", ""), ""]
            continue
        out += [f"## {chat.get('model')}", ""]
        if m.get("thinking"):
            out += ["<details><summary>thinking</summary>", "", m["thinking"], "", "</details>", ""]
        out += [m.get("content", ""), ""]
        if sl := stats_line(m.get("stats"), m.get("stopped", False)):
            out += [f"*{sl}*", ""]
    return "\n".join(out)


# render


def render_content(text: str, md: bool = True, cursor: bool = False) -> RenderableType:
    """Markdown prose plus numbered, syntax-highlighted code panels."""
    if cursor:
        text += " ▍"
    parts: list[RenderableType] = []

    def prose(chunk: str) -> None:
        if chunk.strip():
            parts.append(
                RichMarkdown(chunk.strip("\n"), code_theme=CODE_THEME)
                if md
                else Text(chunk.strip("\n"))
            )

    pos = idx = 0
    for m in FENCE.finditer(text):
        prose(text[pos : m.start()])
        idx += 1
        lang, code = m.group(1) or "text", m.group(2).rstrip("\n")
        parts.append(
            Panel(
                Syntax(
                    code, lang, theme=CODE_THEME, word_wrap=True,
                    background_color=CODE_BG, line_numbers=code.count("\n") >= 6,
                ),
                box=box.ROUNDED,
                border_style=ACCENT,
                padding=(0, 1),
                title=lang,
                title_align="left",
                subtitle=Text(f" #{idx} · Ctrl+Y / /copy {idx} ", style=DIM),
                subtitle_align="right",
            )
        )
        pos = m.end()
    prose(text[pos:])
    return Group(*parts) if parts else Text("")


def stats_line(s: Optional[dict], stopped: bool = False) -> str:
    """'↑120 ↓340 tok · 42.1 tok/s · 8s' for one answer."""
    if not s:
        return "⏹ stopped" if stopped else ""
    bits: list[str] = []
    p, g = s.get("prompt_tokens"), s.get("gen_tokens")
    tok = [f"{sym}{n}" for sym, n in (("↑", p), ("↓", g)) if n is not None]
    if tok:
        bits.append(" ".join(tok) + " tok")
    if s.get("tps"):
        bits.append(f"{s['tps']:.1f} tok/s")
    if s.get("total_s"):
        bits.append(fmt_dur(s["total_s"]))
    if s.get("ttft_s"):
        bits.append(f"ttft {fmt_dur(s['ttft_s'])}")
    if (s.get("load_s") or 0) > 0.05:
        bits.append(f"load {fmt_dur(s['load_s'])}")
    if stopped:
        bits.append("⏹ stopped")
    return " · ".join(bits)


class Bubble(Static):
    """One chat message, streaming-aware."""

    def __init__(
        self,
        who: str,
        text: str = "",
        *,
        thinking: str = "",
        stats: Optional[dict] = None,
        images: Optional[list] = None,
        model: str = "",
        stopped: bool = False,
    ) -> None:
        super().__init__("", classes=f"bubble {who}")
        self.who = who
        self.body_text = text
        self.think_text = thinking
        self.stat = stats
        self.imgs = images or []
        self.model_name = model
        self.stopped = stopped
        self.streaming = False
        self.err = ""

    def on_mount(self) -> None:
        self.border_title = (
            " You " if self.who == "user" else f" {self.model_name or 'assistant'} "
        )
        self.refresh_body()

    def refresh_body(self) -> None:
        parts: list[RenderableType] = []
        if self.imgs:
            parts.append(
                Text("📎 " + ", ".join(Path(p).name for p in self.imgs), style=DIM)
            )
        if self.think_text and getattr(self.app, "show_thinking", True):
            parts.append(
                Panel(
                    Text(self.think_text.strip(), style=f"italic {DIM}"),
                    title="thinking",
                    title_align="left",
                    border_style=DIM,
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        if self.body_text or self.streaming:
            parts.append(
                render_content(
                    self.body_text, md=self.who == "assistant", cursor=self.streaming
                )
            )
        if self.err:
            parts.append(Text(f"⚠ {self.err}", style="bold red"))
        self.update(Group(*parts) if parts else Text(""))
        self.border_subtitle = (
            stats_line(self.stat, self.stopped) if self.who == "assistant" else ""
        )


class ChatInput(TextArea):
    """Multi-line message box: Enter sends, Ctrl+J inserts a newline."""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True, show=False),
        Binding("ctrl+j,shift+enter", "newline", "Newline", priority=True, show=False),
        Binding("escape", "stop_gen", "Stop", priority=True, show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_stop_gen(self) -> None:
        self.app.action_stop()


# dialogs


class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextPrompt(Dialog):
    """One-line input with OK/Cancel."""

    def __init__(self, title: str, value: str = "", hint: str = "") -> None:
        super().__init__()
        self.prompt_title, self.value, self.hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt_title, classes="title")
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(value=self.value, id="p-input")
            with Horizontal(classes="buttons"):
                yield Button("OK", id="p-ok", variant="primary")
                yield Button("Cancel", id="p-cancel")

    def on_mount(self) -> None:
        self.query_one("#p-input", Input).focus()

    @on(Input.Submitted, "#p-input")
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    @on(Button.Pressed, "#p-ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#p-input", Input).value.strip() or None)

    @on(Button.Pressed, "#p-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class Confirm(Dialog):
    """Yes/no question; Cancel is the default focus."""

    def __init__(self, message: str, yes: str = "Yes") -> None:
        super().__init__()
        self.message, self.yes = message, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button(self.yes, id="c-yes", variant="error")
                yield Button("Cancel", id="c-no", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#c-no", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#c-yes")
    def _y(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#c-no")
    def _n(self) -> None:
        self.dismiss(False)


class HelpScreen(Dialog):
    BINDINGS = [Binding("escape,q,f1", "cancel", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog wide"):
            yield Static(RichMarkdown(HELP, code_theme=CODE_THEME))
            with Horizontal(classes="buttons"):
                yield Button("Close", id="h-close", variant="primary")

    @on(Button.Pressed, "#h-close")
    def _c(self) -> None:
        self.dismiss(None)


class ModelPicker(Dialog):
    """Filterable list of installed models."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
    ]

    def __init__(self, models: list[dict], current: str) -> None:
        super().__init__()
        self.models, self.current = models, current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Select model", classes="title")
            yield Input(placeholder="type to filter…", id="mp-filter")
            yield OptionList(id="mp-list")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one("#mp-filter", Input).focus()

    def fill(self, q: str) -> None:
        ol = self.query_one("#mp-list", OptionList)
        ol.clear_options()
        opts = []
        for m in sorted(self.models, key=lambda m: m["name"]):
            if q.lower() not in m["name"].lower():
                continue
            d = m.get("details") or {}
            label = Text()
            label.append(m["name"], style="bold" if m["name"] == self.current else "")
            label.append(
                f"  {d.get('parameter_size', '')} {d.get('quantization_level', '')} · "
                f"{fmt_bytes(m.get('size', 0))}",
                style=DIM,
            )
            opts.append(Option(label, id=m["name"]))
        if opts:
            ol.add_options(opts)
            ol.highlighted = 0

    def action_move(self, delta: int) -> None:
        ol = self.query_one("#mp-list", OptionList)
        (ol.action_cursor_down if delta > 0 else ol.action_cursor_up)()

    @on(Input.Changed, "#mp-filter")
    def _changed(self, event: Input.Changed) -> None:
        self.fill(event.value)

    @on(Input.Submitted, "#mp-filter")
    def _submit(self) -> None:
        ol = self.query_one("#mp-list", OptionList)
        if ol.highlighted is not None and ol.option_count:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    @on(OptionList.OptionSelected, "#mp-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class SettingsScreen(Dialog):
    """Per-chat system prompt, sampling options and thinking mode."""

    def __init__(self, chat: dict) -> None:
        super().__init__()
        self.chat = chat

    def compose(self) -> ComposeResult:
        opts = self.chat.get("options") or {}
        with VerticalScroll(classes="dialog wide"):
            yield Label("Chat settings", classes="title")
            yield Label("System prompt")
            yield TextArea(self.chat.get("system", ""), id="s-system")
            for key, _ in OPTION_FIELDS:
                with Horizontal(classes="row"):
                    yield Label(key, classes="lbl")
                    yield Input(value=str(opts.get(key, "")), placeholder="model default", id=f"s-{key}")
            with Horizontal(classes="row"):
                yield Label("keep_alive", classes="lbl")
                yield Input(
                    value=str(self.chat.get("keep_alive", "")),
                    placeholder="e.g. 5m, 1h, -1",
                    id="s-keep_alive",
                )
            with Horizontal(classes="row"):
                yield Label("thinking", classes="lbl")
                yield Select(
                    [("auto (model default)", "auto"), ("on", "on"), ("off", "off")],
                    value=self.chat.get("think", "auto"),
                    allow_blank=False,
                    id="s-think",
                )
            yield Checkbox("Use these as defaults for new chats", id="s-default")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="s-save", variant="primary")
                yield Button("Cancel", id="s-cancel")

    def collect(self) -> Optional[dict]:
        """Read the form; None when a value is not a number."""
        res: dict[str, Any] = {
            "system": self.query_one("#s-system", TextArea).text.strip(),
            "options": {},
            "think": self.query_one("#s-think", Select).value,
            "keep_alive": self.query_one("#s-keep_alive", Input).value.strip(),
            "as_default": self.query_one("#s-default", Checkbox).value,
        }
        for key, typ in OPTION_FIELDS:
            raw = self.query_one(f"#s-{key}", Input).value.strip()
            if not raw:
                continue
            try:
                res["options"][key] = typ(raw)
            except ValueError:
                self.app.notify(f"{key}: '{raw}' is not a valid {typ.__name__}", severity="error")
                return None
        return res

    @on(Button.Pressed, "#s-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#s-save")
    def _save(self) -> None:
        self.dismiss(self.collect())


class CheatAddScreen(Dialog):
    """Add one cheat to a sheet."""

    def __init__(self, tool: str = "", directory: Path = Path(".")) -> None:
        super().__init__()
        self.tool, self.directory = tool, directory

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Add a cheat", classes="title")
            with Horizontal(classes="row"):
                yield Label("tool", classes="lbl")
                yield Input(value=self.tool, placeholder="git", id="ca-tool")
            with Horizontal(classes="row"):
                yield Label("command", classes="lbl")
                yield Input(placeholder="git rebase -i HEAD~5", id="ca-cmd")
            with Horizontal(classes="row"):
                yield Label("section", classes="lbl")
                yield Input(placeholder=DEFAULT_CATEGORY, id="ca-cat")
            yield Label("description (optional)")
            yield Input(placeholder="interactive rebase", id="ca-desc")
            with Horizontal(classes="buttons"):
                yield Button("Add", id="ca-ok", variant="primary")
                yield Button("Cancel", id="ca-cancel")

    def on_mount(self) -> None:
        self.query_one("#ca-tool", Input).focus()

    def value(self) -> Optional[tuple[str, str, str, str]]:
        tool = self.query_one("#ca-tool", Input).value.strip()
        command = self.query_one("#ca-cmd", Input).value.strip()
        if not tool or not command:
            return None
        cat = self.query_one("#ca-cat", Input).value.strip()
        desc = self.query_one("#ca-desc", Input).value.strip()
        return tool, command, cat or None, desc

    @on(Button.Pressed, "#ca-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ca-ok")
    def _ok(self) -> None:
        if (val := self.value()) is None:
            self.app.notify("tool and command are required", severity="error")
            return
        self.dismiss(val)

    @on(Input.Submitted)
    def _enter(self) -> None:
        self._ok()


class PickTree(DirectoryTree):
    """Directory tree filtered to folders and .gguf files."""

    def filter_paths(self, paths):  # directories + .gguf files, no dotfiles
        return [
            p
            for p in paths
            if not p.name.startswith(".")
            and (p.is_dir() or p.suffix.lower() == ".gguf")
        ]


class FolderPicker(Dialog):
    """Choose a model folder or .gguf file."""

    def __init__(self, start: str = "") -> None:
        super().__init__()
        p = Path(start).expanduser() if start else Path.home()
        while not p.is_dir() and p != p.parent:
            p = p.parent
        self.start = p if p.is_dir() else Path.home()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide tall"):
            yield Label("Pick a model folder (or a .gguf file)", classes="title")
            yield Input(value=str(self.start), id="fp-path")
            yield PickTree(self.start, id="fp-tree")
            with Horizontal(classes="buttons"):
                yield Button("Select", id="fp-ok", variant="primary")
                yield Button("Up a level", id="fp-up")
                yield Button("Cancel", id="fp-cancel")

    def _set(self, path: Any) -> None:
        if path:
            self.query_one("#fp-path", Input).value = str(path)

    @on(Tree.NodeHighlighted, "#fp-tree")
    def _hl(self, event: Tree.NodeHighlighted) -> None:
        self._set(getattr(event.node.data, "path", None))

    @on(Button.Pressed, "#fp-up")
    def _up(self) -> None:
        cur = Path(self.query_one("#fp-path", Input).value).expanduser()
        parent = cur.parent if cur.parent != cur else cur
        self.query_one("#fp-tree", PickTree).path = parent
        self._set(parent)

    @on(Button.Pressed, "#fp-ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#fp-path", Input).value.strip() or None)

    @on(Input.Submitted, "#fp-path")
    def _sub(self) -> None:
        self._ok()

    @on(Button.Pressed, "#fp-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class PullScreen(Dialog):
    """Streaming download of a model, with per-layer progress."""

    def __init__(self, api: Ollama, name: str) -> None:
        super().__init__()
        self.api, self.model_name = api, name
        self.ok = False
        self.layers: dict[str, list[int]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(Text.assemble(("Pulling ", ""), (self.model_name, f"bold {ACCENT}")), classes="title")
            yield Static("starting…", id="pull-status")
            yield ProgressBar(total=None, show_eta=False, id="pull-bar")
            yield Static("", id="pull-speed")
            yield RichLog(id="pull-log", wrap=True, markup=False, highlight=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="pull-cancel", variant="error")
                yield Button("Close", id="pull-close", disabled=True, variant="primary")

    def on_mount(self) -> None:
        self.run_pull()

    def action_cancel(self) -> None:
        self.workers.cancel_group(self, "pull")
        self.dismiss(self.ok)

    @on(Button.Pressed, "#pull-cancel")
    def _cancel_btn(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#pull-close")
    def _close_btn(self) -> None:
        self.dismiss(self.ok)

    def track_layers(self, obj: dict) -> bool:
        """Remember a layer's total/done; True when the object was a layer."""
        dig, total = obj.get("digest"), obj.get("total")
        if not (dig and total):
            return False
        self.layers[dig] = [total, obj.get("completed", 0)]
        return True

    def speed_sample(self, done: int, t_last: float, c_last: int, speed: float) -> float:
        """Smoothed download speed in bytes/s."""
        now = time.monotonic()
        if now - t_last < 0.5:
            return speed
        inst = (done - c_last) / (now - t_last)
        smoothed = inst if speed == 0 else speed * 0.7 + inst * 0.3
        return smoothed

    @work(exclusive=True, group="pull")
    async def run_pull(self) -> None:
        log = self.query_one("#pull-log", RichLog)
        status = self.query_one("#pull-status", Static)
        bar = self.query_one("#pull-bar", ProgressBar)
        speed_w = self.query_one("#pull-speed", Static)
        seen: set[str] = set()
        last_status = ""
        t_last, c_last, speed = time.monotonic(), 0, 0.0
        try:
            async for obj in self.api.stream(
                "/api/pull",
                {"model": self.model_name, "name": self.model_name, "stream": True},
            ):
                st = obj.get("status", "")
                dig = obj.get("digest")
                if dig and obj.get("total"):
                    self.track_layers(obj)
                    if dig not in seen:
                        seen.add(dig)
                        log.write(
                            Text(
                                f"layer {dig.split(':')[-1][:12]} · {fmt_bytes(obj['total'])}",
                                style=DIM,
                            )
                        )
                elif st and st != last_status:
                    log.write(Text(st))
                last_status = st
                total = sum(v[0] for v in self.layers.values())
                done = sum(v[1] for v in self.layers.values())
                new_speed = self.speed_sample(done, t_last, c_last, speed)
                if new_speed != speed:
                    speed = new_speed
                    t_last, c_last = time.monotonic(), done
                if total:
                    bar.update(total=total, progress=done)
                    eta = fmt_dur((total - done) / speed) if speed > 1 else "…"
                    speed_w.update(
                        f"{fmt_bytes(done)} / {fmt_bytes(total)} · {fmt_bytes(speed)}/s · ETA {eta}"
                    )
                status.update(st)
            self.ok = True
            bar.update(total=100, progress=100)
            status.update(Text("✓ done", style="bold green"))
            log.write(Text(f"✓ {self.model_name} is ready", style="bold green"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network / API
            status.update(Text(f"✗ {e}", style="bold red"))
            log.write(Text(str(e), style="red"))
        finally:
            try:
                self.query_one("#pull-close", Button).disabled = False
                self.query_one("#pull-cancel", Button).disabled = True
            except Exception:
                pass


QUANTS = [
    ("none (keep source precision)", ""),
    ("q4_K_M (balanced)", "q4_K_M"),
    ("q4_K_S (smaller)", "q4_K_S"),
    ("q8_0 (near-lossless)", "q8_0"),
]
OUTTYPES = [
    ("f16", "f16"),
    ("bf16", "bf16"),
    ("f32", "f32"),
    ("q8_0", "q8_0"),
    ("auto", "auto"),
]


class CreateScreen(Dialog):
    """Create an Ollama model from a folder or .gguf file."""

    def __init__(self) -> None:
        super().__init__()
        self.created = False
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._auto_name = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog wide"):
            yield Label("Create a model", classes="title")
            yield Label("Model name")
            yield Input(placeholder="my-model  or  my-model:q4", id="c-name")
            yield Label("Source folder (or .gguf file)")
            with Horizontal(classes="row"):
                yield Input(placeholder="/path/to/model-folder", id="c-src")
                yield Button("Browse…", id="c-browse", variant="primary")
            yield Static("", id="c-hint", classes="hint")
            yield Label("What should happen to the folder?")
            with RadioSet(id="c-mode"):
                yield RadioButton("Import the folder directly into Ollama (safetensors)", value=True)
                yield RadioButton("Convert folder → .gguf with llama.cpp, then create")
                yield RadioButton("Use an existing .gguf file")
            with Horizontal(classes="row"):
                yield Label("Quantize", classes="lbl")
                yield Select(QUANTS, value="", allow_blank=False, id="c-quant")
            with Horizontal(classes="row"):
                yield Label("GGUF type", classes="lbl")
                yield Select(OUTTYPES, value="f16", allow_blank=False, id="c-outtype")
            yield Label("llama.cpp directory (only for conversion)")
            yield Input(value=find_llama_cpp(), id="c-llama")
            yield Label("Python used for conversion (needs torch, transformers, gguf)")
            yield Input(value=os.environ.get("LLAMA_CPP_PYTHON", sys.executable), id="c-python")
            yield Label("SYSTEM prompt (optional)")
            yield Input(id="c-system")
            yield Label("Extra Modelfile lines (optional: TEMPLATE, PARAMETER …)")
            yield TextArea("", id="c-extra")
            yield Static("", id="c-status")
            yield RichLog(id="c-log", wrap=True, markup=False, highlight=False)
            with Horizontal(classes="buttons"):
                yield Button("Create", id="c-go", variant="success")
                yield Button("Stop", id="c-stop", variant="error", disabled=True)
                yield Button("Close", id="c-close")

    def action_cancel(self) -> None:
        self._kill()
        self.workers.cancel_group(self, "create")
        self.dismiss(self.created)

    def _kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    def _set_src(self, path: str) -> None:
        self.query_one("#c-src", Input).value = path

    @on(Button.Pressed, "#c-browse")
    def _browse(self) -> None:
        cur = self.query_one("#c-src", Input).value
        self.app.push_screen(FolderPicker(cur), lambda p: self._set_src(p) if p else None)

    @on(Input.Changed, "#c-src")
    def _src_changed(self, event: Input.Changed) -> None:
        kind, text = inspect_source(event.value) if event.value.strip() else ("none", "")
        self.query_one("#c-hint", Static).update(Text(text, style=DIM))
        mode = {"safetensors": 0, "dir": 0, "gguf": 2, "gguf-dir": 2}.get(kind)
        if mode is not None:
            self.query_one("#c-mode", RadioSet).query(RadioButton)[mode].value = True
        name = self.query_one("#c-name", Input)
        if kind != "none" and (not name.value or name.value == self._auto_name):
            self._auto_name = model_name_from_path(Path(event.value).expanduser())
            name.value = self._auto_name

    @on(Button.Pressed, "#c-close")
    def _close(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#c-stop")
    def _stop(self) -> None:
        self._kill()
        self.workers.cancel_group(self, "create")
        self._log("■ stopped", "yellow")
        self._busy(False)

    def form(self) -> dict[str, str]:
        """All the inputs as a plain dict."""
        q = lambda i, t=Input: self.query_one(i, t)  # noqa: E731
        return {
            "name": q("#c-name").value.strip(),
            "src": q("#c-src").value.strip(),
            "mode": str(q("#c-mode", RadioSet).pressed_index),
            "quant": str(q("#c-quant", Select).value),
            "outtype": str(q("#c-outtype", Select).value),
            "llama": q("#c-llama").value.strip(),
            "python": q("#c-python").value.strip(),
            "system": q("#c-system").value.strip(),
            "extra": q("#c-extra", TextArea).text,
        }

    def validate(self, p: dict[str, str]) -> Optional[str]:
        """A problem with the form, or None when it is fine."""
        if not p["name"] or re.search(r"\s", p["name"]):
            return "Enter a model name (no spaces)."
        if inspect_source(p["src"])[0] in ("none", "bad"):
            return "Source path not found."
        return None

    @on(Button.Pressed, "#c-go")
    def _go(self) -> None:
        p = self.form()
        if problem := self.validate(p):
            self.app.notify(problem, severity="error")
            return
        self.query_one("#c-log", RichLog).clear()
        self.run_create(p)

    def _log(self, text: str, style: str = "") -> None:
        self.query_one("#c-log", RichLog).write(Text(text, style=style))

    def _busy(self, on_: bool) -> None:
        self.query_one("#c-go", Button).disabled = on_
        self.query_one("#c-stop", Button).disabled = not on_
        if not on_:
            self.query_one("#c-status", Static).update("")

    async def _run_proc(self, cmd: list[str]) -> int:
        """Run a helper process, streaming its output into the dialog log."""
        self._log("$ " + " ".join(cmd), DIM)
        status = self.query_one("#c-status", Static)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf, last = "", ""
        assert self.proc.stdout is not None
        while True:
            chunk = await self.proc.stdout.read(2048)
            if not chunk:
                break
            buf += strip_ansi(dec.decode(chunk)).replace("\r\n", "\n")
            *done, buf = buf.split("\n")
            for line in done:
                seg = last_segment(line)
                if seg and seg != last:
                    self._log(seg)
                    last = seg
            status.update(Text(last_segment(buf), style=f"bold {ACCENT}"))
        return await self.proc.wait()

    def convert_script(self, llama: Path) -> Optional[Path]:
        """The llama.cpp converter script inside `llama`, if there is one."""
        return next(
            (
                c
                for c in (llama / "convert_hf_to_gguf.py", llama / "convert-hf-to-gguf.py")
                if c.exists()
            ),
            None,
        )

    async def _convert(self, src: Path, p: dict[str, str]) -> Path:
        """Run llama.cpp's converter on a model folder."""
        if not src.is_dir():
            raise RuntimeError("Conversion needs a model folder, not a file.")
        llama = Path(p["llama"]).expanduser()
        script = self.convert_script(llama)
        if script is None:
            raise RuntimeError(
                "convert_hf_to_gguf.py not found — clone llama.cpp and set its directory above."
            )
        outdir = src.parent if os.access(src.parent, os.W_OK) else src
        out = outdir / f"{src.name}-{p['outtype']}.gguf"
        self._log(f"Converting {src.name} → {out.name}", f"bold {ACCENT}")
        rc = await self._run_proc(
            [p["python"] or sys.executable, str(script), str(src), "--outfile", str(out), "--outtype", p["outtype"]]
        )
        if rc != 0 or not out.exists():
            raise RuntimeError(f"GGUF conversion failed (exit {rc}).")
        self._log(f"✓ wrote {out} ({fmt_bytes(out.stat().st_size)})", "green")
        return out

    def resolve_source(self, src: Path, p: dict[str, str]) -> Path:
        """Which path the Modelfile should point at."""
        if p["mode"] == "2":
            if src.is_dir():
                ggufs = sorted(src.glob("*.gguf"))
                if len(ggufs) != 1:
                    raise RuntimeError("Folder must contain exactly one .gguf — pick the file itself.")
                return ggufs[0]
        return src

    def modelfile(self, source: Path, p: dict[str, str]) -> str:
        """The FROM/SYSTEM/… lines handed to `ollama create`."""
        frm = f'"{source}"' if " " in str(source) else str(source)
        lines = [f"FROM {frm}"]
        if p["system"]:
            lines.append('SYSTEM """' + p["system"].replace('"""', "'''") + '"""')
        if p["extra"].strip():
            lines.append(p["extra"].strip())
        return "\n".join(lines) + "\n"

    @work(exclusive=True, group="create")
    async def run_create(self, p: dict[str, str]) -> None:
        """Import / convert / create, then report what happened."""
        self._busy(True)
        tmp: Optional[str] = None
        try:
            ollama = shutil.which("ollama")
            if not ollama:
                raise RuntimeError("`ollama` CLI not found in PATH (needed to import local files).")
            src = Path(p["src"]).expanduser().resolve()
            source = await self._convert(src, p) if p["mode"] == "1" else self.resolve_source(src, p)
            with tempfile.NamedTemporaryFile(
                "w", suffix=".Modelfile", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(self.modelfile(source, p))
                tmp = fh.name
            cmd = [ollama, "create", p["name"], "-f", tmp]
            if p["quant"]:
                cmd += ["-q", p["quant"]]
            self._log("Creating model in Ollama…", f"bold {ACCENT}")
            if await self._run_proc(cmd) != 0:
                raise RuntimeError("`ollama create` failed.")
            self.created = True
            self._log(
                f"✓ Created '{p['name']}'. Use it with Ctrl+O in a chat.", "bold green"
            )
        except asyncio.CancelledError:
            self._kill()
            raise
        except Exception as e:
            self._log(f"✗ {e}", "bold red")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            try:
                self._busy(False)
            except Exception:
                pass


class PkgPick(ModalScreen):
    """Choose between fuzzy package candidates: (name, repo|aur|local)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, term: str, opts: list[tuple[str, str]]) -> None:
        super().__init__()
        self.term, self.opts = term, opts

    def compose(self) -> ComposeResult:
        with Vertical(id="pick"):
            yield Static(Text(f"'{self.term}' not found - did you mean:", style="bold"))
            yield OptionList(
                *[Text.assemble(n, "   ", (s, "dim")) for n, s in self.opts]
            )

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(self.opts[e.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class Pass(ModalScreen):
    """Ask for the sudo password; it is never written to disk."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="pass"):
            yield Static("sudo password (kept in memory only, never saved)")
            yield Input(password=True, id="pw")
            yield Static("", id="err")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, e: Input.Submitted) -> None:
        e.stop()
        self.check(e.value)

    @work
    async def check(self, pw: str) -> None:
        """Verify the password off the event loop."""
        if await asyncio.to_thread(sudo_ok, pw):
            self.dismiss(pw)
        else:
            self.query_one("#err", Static).update("wrong password, try again")
            self.query_one(Input).value = ""

    def action_cancel(self) -> None:
        self.dismiss(None)


CSS = """
Screen { background: $background; }
Dialog { align: center middle; background: #000000 65%; }
.dialog { width: 80; max-width: 95%; height: auto; max-height: 92%;
          background: $surface; border: thick $primary; padding: 1 2; }
.dialog.wide { width: 100; }
.dialog.tall { height: 90%; }
.title { text-style: bold; color: $primary; margin-bottom: 1; }
.hint { color: $sys-dim; margin-bottom: 1; }
.side-title { text-style: bold; color: $primary; padding: 0 1; }
.row { height: auto; }
.row Input, .row Select { width: 1fr; }
.lbl { width: 18; height: 3; content-align: left middle; }
.buttons { height: auto; align-horizontal: right; margin-top: 1; }
.buttons Button { margin-left: 1; }
#fp-tree { height: 1fr; margin: 1 0; background: $sys-input-bg; }
#mp-list { height: 14; margin-top: 1; border: none; background: $sys-input-bg; }
#c-log, #pull-log { height: 10; background: $sys-log-bg; margin-top: 1; }
#c-extra, #s-system { height: 5; }
#pull-status, #pull-speed, #c-status { height: auto; }
#pick, #pass { width: 64; height: auto; max-height: 20; border: round $primary;
               background: $surface; padding: 1 2; }

#sidebar { width: 30; background: $surface; border-right: solid $sys-line; padding: 0 1; }
#sidebar-title { text-style: bold; color: $primary; padding: 1 0 0 0; }
#chat-list { height: 1fr; background: $surface; border: none; margin: 1 0; padding: 0; }
#sidebar-btns { height: auto; }
#sidebar-btns Button { width: 1fr; min-width: 6; }
#chat-main { width: 1fr; }
#messages { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
.bubble { height: auto; padding: 0 1; margin: 1 0 0 0; border: round $sys-line; }
.bubble.user { border: round $primary; margin-left: 10; }
.bubble.assistant { margin-right: 6; }
#welcome { margin: 2 4; color: $sys-dim; }
#statusline { height: 1; background: $surface; padding: 0 1; }
ChatInput { height: auto; min-height: 3; max-height: 12; border: round $primary;
            background: $surface; }

#model-actions { height: auto; padding: 1 1 0 1; }
#model-actions Button { margin-right: 1; }
#models-body { height: 1fr; padding: 1 1 0 1; }
#models-table { width: 3fr; }
#model-info { width: 2fr; border-left: solid $sys-line; padding: 0 2; }
#running-title { padding: 0 1; text-style: bold; color: $primary; }
#running-table { height: 7; margin: 0 1 1 1; }

#cheat-q { margin: 0 1; }
#cheat-body { height: 1fr; }
#cheat-left { width: 26; border-right: solid $sys-line; padding: 0 1; }
#cheat-sheets { height: 1fr; background: $sys-input-bg; border: none; }
#cheats { width: 1fr; }
#cheat-right { width: 2fr; border-left: solid $sys-line; padding: 0 2; }
#cheat-detail { height: auto; }

#pkg { margin: 0 1; }
#pkghint { height: 1; padding: 0 1; color: $sys-dim; }
#pkg-body { height: 1fr; padding: 0 1; }
#matches { width: 2fr; }
#pkg-side { width: 1fr; border-left: solid $sys-line; padding: 0 2; }

#jobs-body { height: 1fr; padding: 0 1; }
#jobs { width: 46; border-right: solid $sys-line; }
#jobslog { width: 1fr; }
"""

TAB_CHAT, TAB_MODELS, TAB_CHEATS, TAB_PKGS, TAB_JOBS = (
    "tab-chat",
    "tab-models",
    "tab-cheats",
    "tab-pkgs",
    "tab-jobs",
)
TAB_IDS = (TAB_CHAT, TAB_MODELS, TAB_CHEATS, TAB_PKGS, TAB_JOBS)

# bindings that only make sense on one tab
CHAT_ACTIONS = {
    "new_chat", "pick_model", "regenerate", "copy_code", "export_chat",
    "toggle_thinking", "rename_chat", "settings", "scroll_chat", "stop",
    "clear_chat", "undo_chat",
}
MODEL_ACTIONS = {
    "pull", "rename_model", "delete_model", "copy_model", "unload_model",
    "create_model", "reload",
}
CHEAT_ACTIONS = {
    "run_cheat", "copy_cheat", "add_cheat", "delete_cheat", "edit_sheet",
    "new_sheet", "reload_cheats", "focus_cheats",
}
PKG_ACTIONS = {
    "pkg_install", "pkg_remove", "pkg_update", "pkg_orphans", "pkg_clean",
    "pkg_check", "pkg_search", "pkg_kill", "pkg_clear", "focus_pkg_input",
    "focus_matches", "reload_index",
}
JOB_ACTIONS = {"job_kill", "job_clear", "job_refresh", "follow_log"}
TAB_ACTIONS = {
    TAB_CHAT: CHAT_ACTIONS,
    TAB_MODELS: MODEL_ACTIONS,
    TAB_CHEATS: CHEAT_ACTIONS,
    TAB_PKGS: PKG_ACTIONS,
    TAB_JOBS: JOB_ACTIONS,
}

# what a letter key means on each tab (dispatched by action_letter)
LETTER_ACTIONS: dict[str, dict[str, str]] = {
    TAB_MODELS: {
        "p": "pull", "n": "create_model", "r": "rename_model", "c": "copy_model",
        "d": "delete_model", "u": "unload_model", "f": "reload",
    },
    TAB_CHEATS: {
        "a": "add_cheat", "e": "edit_sheet", "t": "new_sheet", "d": "delete_cheat",
        "y": "copy_cheat", "g": "run_cheat", "r": "reload_cheats", "f": "focus_cheats",
    },
    TAB_PKGS: {
        "i": "pkg_install", "r": "pkg_remove", "u": "pkg_update", "o": "pkg_orphans",
        "c": "pkg_clean", "k": "pkg_check", "s": "pkg_search", "f": "focus_matches",
    },
    TAB_JOBS: {
        "x": "job_kill", "d": "job_clear", "f": "job_refresh", "l": "follow_log",
    },
}


# app


class PaneSwitcher(TabbedContent):
    """Tabs where only the pane on screen may claim the active tab.

    Hiding a pane can drop Textual's focus back into a widget of the pane we
    just left, which would otherwise yank the tab straight back.
    """

    def _on_tab_pane_focused(self, event: TabPane.Focused) -> None:
        event.stop().prevent_default()
        shown = self.get_child_by_type(ContentSwitcher).current
        if event.tab_pane.id is not None and event.tab_pane.id == shown:
            self.active = event.tab_pane.id


class SysApp(App):
    """Ollama chat + pacman/AUR packages + cheat sheets, one terminal app."""

    TITLE = APP_NAME
    SUB_TITLE = ""
    CSS = CSS
    show_thinking = True

    BINDINGS = [
        # tabs
        Binding("ctrl+1", "show_tab('tab-chat')", "Chat"),
        Binding("ctrl+2", "show_tab('tab-models')", "Models"),
        Binding("ctrl+3", "show_tab('tab-cheats')", "Cheats"),
        Binding("ctrl+4", "show_tab('tab-pkgs')", "Packages"),
        Binding("ctrl+5", "show_tab('tab-jobs')", "Jobs"),
        Binding("f2", "show_tab('tab-chat')", show=False),
        Binding("f3", "show_tab('tab-models')", show=False),
        Binding("f1", "help", "Help"),
        # chat
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("ctrl+o", "pick_model", "Model"),
        Binding("ctrl+r", "regenerate", "Regen"),
        Binding("ctrl+y", "copy_code", "Copy code", priority=True),
        Binding("ctrl+s", "export_chat", "Export"),
        Binding("ctrl+t", "toggle_thinking", "Thinking"),
        Binding("f4", "settings", "Settings"),
        Binding("f6", "rename_chat", "Rename chat", show=False),
        Binding("alt+up", "scroll_chat(-1)", show=False),
        Binding("alt+down", "scroll_chat(1)", show=False),
        Binding("escape", "tab_escape", show=False),
        Binding("slash", "focus_search", show=False),
        # single letters, dispatched per tab by action_letter
        Binding("p", "letter('p')", show=False),
        Binding("n", "letter('n')", show=False),
        Binding("r", "letter('r')", show=False),
        Binding("c", "letter('c')", show=False),
        Binding("d", "letter('d')", show=False),
        Binding("u", "letter('u')", show=False),
        Binding("a", "letter('a')", show=False),
        Binding("e", "letter('e')", show=False),
        Binding("t", "letter('t')", show=False),
        Binding("y", "letter('y')", show=False),
        Binding("g", "letter('g')", show=False),
        Binding("i", "letter('i')", show=False),
        Binding("o", "letter('o')", show=False),
        Binding("k", "letter('k')", show=False),
        Binding("s", "letter('s')", show=False),
        Binding("x", "letter('x')", show=False),
        Binding("f", "letter('f')", show=False),
        Binding("l", "letter('l')", show=False),
        Binding("f5", "refresh", "Refresh"),
        Binding("f7", "follow_log", show=False),
    ]

    # setup
    def __init__(self, cfg: Config, start_tab: str = TAB_CHAT) -> None:
        apply_theme(cfg)
        self.cfg = cfg
        super().__init__()
        self.start_tab = start_tab if start_tab in TAB_IDS else TAB_CHAT
        self.host = ollama_host(cfg)
        self.api = Ollama(self.host)
        self.store = ChatStore(cfg)
        self.helper = aur_helper(cfg)
        self.models: list[dict] = []
        self.pending_images: list[str] = []
        self.generating = False
        self.live: Optional[dict] = None
        self.chat: dict = {}
        # packages
        self.idx = PkgIndex()
        self.counts: dict[str, int] = {}
        self.pkg_row: str = ""
        self.jobs_cursor: Optional[str] = None
        self.log_off = 0
        self.jobs_sig: list[tuple[str, str]] = []
        self.jobs_last: dict[str, str] = {}
        self.want: Optional[str] = None
        self.pw: Optional[str] = None
        self.nopass: Optional[bool] = None
        # cheats
        self.cheats: list[Cheat] = []
        self.cheat_matches: list[CheatMatch] = []
        self.cheat_row: int = -1
        self.sheet_filter = ""

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """$sys-* CSS variables, needed before the stylesheet is parsed."""
        return theme_variables(self.cfg)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with PaneSwitcher(id="tabs", initial=TAB_CHAT):
            with TabPane("Chat", id=TAB_CHAT):
                with Horizontal():
                    with Vertical(id="sidebar"):
                        yield Label("Chats", id="sidebar-title")
                        yield OptionList(id="chat-list")
                        with Horizontal(id="sidebar-btns"):
                            yield Button("New", id="btn-new", variant="primary")
                            yield Button("Del", id="btn-del")
                    with Vertical(id="chat-main"):
                        yield VerticalScroll(id="messages")
                        yield Static(id="statusline")
                        yield ChatInput(id="input")
            with TabPane("Models", id=TAB_MODELS):
                with Horizontal(id="model-actions"):
                    yield Button("Pull (p)", name="pull", classes="mbtn", variant="primary")
                    yield Button("Create (n)", name="create_model", classes="mbtn", variant="success")
                    yield Button("Rename (r)", name="rename_model", classes="mbtn")
                    yield Button("Copy (c)", name="copy_model", classes="mbtn")
                    yield Button("Delete (d)", name="delete_model", classes="mbtn", variant="error")
                    yield Button("Unload (u)", name="unload_model", classes="mbtn")
                    yield Button("Refresh", name="reload", classes="mbtn")
                with Horizontal(id="models-body"):
                    yield DataTable(id="models-table")
                    with VerticalScroll(id="model-info"):
                        yield Static("Select a model to see its details.", id="model-info-body")
                yield Label("Loaded in memory", id="running-title")
                yield DataTable(id="running-table")
            with TabPane("Cheats", id=TAB_CHEATS):
                yield Input(
                    placeholder="fuzzy-search your sheets, e.g. 'git rebase'  ·  /help",
                    id="cheat-q",
                )
                with Horizontal(id="cheat-body"):
                    with Vertical(id="cheat-left"):
                        yield Label("Sheets", classes="side-title")
                        yield OptionList(id="cheat-sheets")
                    yield DataTable(id="cheats")
                    with VerticalScroll(id="cheat-right"):
                        yield Static("", id="cheat-detail")
            with TabPane("Packages", id=TAB_PKGS):
                yield Input(placeholder="package name(s), Enter = install", id="pkg")
                yield Static("", id="pkghint")
                with Horizontal(id="pkg-body"):
                    yield DataTable(id="matches")
                    with VerticalScroll(id="pkg-side"):
                        yield Static("", id="pkg-info")
            with TabPane("Jobs", id=TAB_JOBS):
                with Horizontal(id="jobs-body"):
                    yield DataTable(id="jobs")
                    yield RichLog(id="jobslog", wrap=True, markup=False, highlight=False, max_lines=5000)
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme_safe()
        self.setup_tables()
        self.show_thinking = bool(self.cfg.get("app.show_thinking", True))
        self.query_one("#input", ChatInput).border_title = (
            " Message — Enter send · Ctrl+J newline · /help "
        )
        await self.boot_chats()
        self.set_interval(4.0, self.poll_running)
        self.set_interval(1.5, self.tick_jobs)
        self.reload()
        self.reload_cheats()
        self.refresh_index()
        self.query_one("#input", ChatInput).focus()
        if self.start_tab != TAB_CHAT:
            self.action_show_tab(self.start_tab)

    def register_theme_safe(self) -> None:
        """Apply the configured colours; older textual may lack themes."""
        theme = build_theme(self.cfg)
        if theme is not None:
            try:
                self.register_theme(theme)
                self.theme = self.cfg.get("app.theme", "sys-dark")
            except Exception:
                pass
        self.sub_title = f"{self.host} · {self.helper or 'pacman only'}"

    def setup_tables(self) -> None:
        """Columns and cursor types for every DataTable."""
        mt = self.query_one("#models-table", DataTable)
        mt.cursor_type = "row"
        mt.zebra_stripes = True
        mt.add_columns("Name", "Params", "Quant", "Family", "Size", "Modified")
        rt = self.query_one("#running-table", DataTable)
        rt.cursor_type = "row"
        rt.add_columns("Name", "Size", "Placement", "Context", "Unloads in")
        ct = self.query_one("#cheats", DataTable)
        ct.cursor_type = "row"
        ct.zebra_stripes = True
        ct.add_columns("Tool", "Section", "Command", "Description")
        mt2 = self.query_one("#matches", DataTable)
        mt2.cursor_type = "row"
        mt2.zebra_stripes = True
        mt2.add_columns("Package", "Source", "Status")
        jt = self.query_one("#jobs", DataTable)
        jt.cursor_type = "row"
        jt.zebra_stripes = True
        jt.add_columns("ID", "Status", "Job", "Time")

    async def boot_chats(self) -> None:
        """Pick the chat to show: last active, else newest, else new."""
        active = self.store.chats.get(self.store.meta.get("active", ""))
        if not active and self.store.chats:
            active = max(self.store.chats.values(), key=lambda c: c.get("updated", 0))
        self.chat = active or self.store.new_chat(self.store.meta.get("last_model", ""))
        self.store.meta["active"] = self.chat["id"]
        await self.show_chat()

    # tabs and global keys
    def active_tab(self) -> str:
        try:
            return str(self.query_one(TabbedContent).active)
        except Exception:
            return TAB_CHAT

    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        """Grey out keys that do not belong to the visible tab."""
        try:
            if isinstance(self.screen, ModalScreen):
                return action in {"quit", "command_palette"}
            tab = self.active_tab()
        except Exception:
            return True
        if action in ("show_tab", "help"):
            return True
        for tid, actions in TAB_ACTIONS.items():
            if action in actions:
                return tab == tid
        return True

    @on(TabbedContent.TabActivated)
    async def _tab(self, event: TabbedContent.TabActivated) -> None:
        if not self.screen_stack:
            return
        self.refresh_bindings()
        pane = event.pane.id
        if pane == TAB_MODELS:
            self.query_one("#models-table", DataTable).focus()
            await self.refresh_running()
        elif pane == TAB_CHEATS:
            self.focus_cheats()
        elif pane == TAB_PKGS:
            self.query_one("#pkg", Input).focus()
        elif pane == TAB_JOBS:
            self.query_one("#jobs", DataTable).focus()
        else:
            self.focus_input()

    def action_show_tab(self, tab: str) -> None:
        if tab in TAB_IDS:
            self.query_one(TabbedContent).active = tab

    def action_refresh(self) -> None:
        """F5: refresh whatever the active tab shows."""
        handler = {
            TAB_MODELS: self.reload,
            TAB_CHEATS: self.reload_cheats,
            TAB_PKGS: self.refresh_index,
            TAB_JOBS: self.tick_jobs,
            TAB_CHAT: self.reload,
        }.get(self.active_tab())
        if handler is not None:
            result = handler()
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    def action_tab_escape(self) -> None:
        """Esc: leave a list, stop generation, or go back to the chat."""
        tab = self.active_tab()
        if tab == TAB_PKGS:
            self.focus_pkg_input()
        elif tab == TAB_CHEATS:
            self.focus_cheats()
        elif tab in (TAB_MODELS, TAB_JOBS):
            self.action_show_tab(TAB_CHAT)
        else:
            self.action_stop()

    def action_focus_search(self) -> None:
        """/ jumps to the search box of the active tab."""
        tab = self.active_tab()
        if tab == TAB_PKGS:
            self.focus_pkg_input()
        elif tab == TAB_MODELS:
            self.action_pick_model()
        else:
            self.focus_cheats()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def focus_input(self) -> None:
        try:
            self.query_one("#input", ChatInput).focus()
        except Exception:
            pass

    # chat list
    def refresh_chat_list(self) -> None:
        ol = self.query_one("#chat-list", OptionList)
        ol.clear_options()
        chats = self.store.sorted_chats()
        opts = []
        for c in chats:
            label = Text()
            label.append(
                shorten(c.get("title", "Chat"), 24),
                style="bold" if c["id"] == self.chat.get("id") else "",
            )
            label.append("\n" + (c.get("model") or "no model"), style=DIM)
            opts.append(Option(label, id=c["id"]))
        if opts:
            ol.add_options(opts)
            for i, c in enumerate(chats):
                if c["id"] == self.chat.get("id"):
                    ol.highlighted = i

    def make_bubble(self, m: dict) -> Bubble:
        return Bubble(
            m["role"],
            m.get("content", ""),
            thinking=m.get("thinking", ""),
            stats=m.get("stats"),
            images=m.get("images"),
            model=self.chat.get("model", ""),
            stopped=m.get("stopped", False),
        )

    async def show_chat(self) -> None:
        """Rebuild the message area from the active chat."""
        box_ = self.query_one("#messages", VerticalScroll)
        await box_.remove_children()
        msgs = [
            m for m in self.chat.get("messages", []) if m.get("role") in ("user", "assistant")
        ]
        if msgs:
            await box_.mount_all([self.make_bubble(m) for m in msgs])
        else:
            await box_.mount(Static(WELCOME, id="welcome"))
        self.call_after_refresh(box_.scroll_end, animate=False)
        self.refresh_chat_list()
        self.update_status()

    @on(OptionList.OptionSelected, "#chat-list")
    async def _chat_selected(self, event: OptionList.OptionSelected) -> None:
        cid = event.option.id
        if cid == self.chat.get("id"):
            self.focus_input()
            return
        if self.generating:
            self.notify("Stop generating first (Esc).", severity="warning")
            self.refresh_chat_list()
            return
        self.chat = self.store.chats[cid]
        self.store.meta["active"] = cid
        self.store.save()
        await self.show_chat()
        self.focus_input()

    @on(Button.Pressed, "#btn-new")
    async def _btn_new(self) -> None:
        await self.action_new_chat()

    @on(Button.Pressed, "#btn-del")
    def _btn_del(self) -> None:
        self.delete_chat()

    async def action_new_chat(self) -> None:
        if self.generating:
            self.notify("Stop generating first (Esc).", severity="warning")
            return
        self.chat = self.store.new_chat(
            self.chat.get("model") or self.store.meta.get("last_model", "")
        )
        self.store.meta["active"] = self.chat["id"]
        self.store.save()
        await self.show_chat()
        self.focus_input()

    def delete_chat(self) -> None:
        if self.generating:
            self.notify("Stop generating first (Esc).", severity="warning")
            return
        self.push_screen(
            Confirm(f"Delete chat '{self.chat.get('title', 'this chat')}'?", "Delete"),
            self._do_delete_chat,
        )

    async def _do_delete_chat(self, ok: Optional[bool]) -> None:
        if not ok:
            return
        self.store.chats.pop(self.chat["id"], None)
        rest = self.store.sorted_chats()
        self.chat = rest[0] if rest else self.store.new_chat(self.store.meta.get("last_model", ""))
        self.store.meta["active"] = self.chat["id"]
        self.store.save()
        await self.show_chat()

    def action_rename_chat(self) -> None:
        self.push_screen(
            TextPrompt("Rename chat", value=self.chat.get("title", "")), self._do_rename_chat
        )

    def _do_rename_chat(self, title: Optional[str]) -> None:
        if title:
            self.chat["title"] = title
            self.store.save()
            self.refresh_chat_list()

    def set_chat_model(self, name: str) -> None:
        self.chat["model"] = name
        self.store.meta["last_model"] = name
        self.store.save()
        self.update_status()
        self.refresh_chat_list()

    def action_pick_model(self) -> None:
        if self.generating:
            return
        if not self.models:
            self.notify("No models loaded. Press F5 to refresh or F3 → p to pull one.", severity="warning")
            return
        self.push_screen(
            ModelPicker(self.models, self.chat.get("model", "")),
            lambda n: (
                (self.set_chat_model(n), self.focus_input()) if n else self.focus_input()
            ),
        )

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.chat), self._apply_settings)

    def _apply_settings(self, res: Optional[dict]) -> None:
        if res:
            for k in ("system", "options", "think", "keep_alive"):
                self.chat[k] = res[k]
            if res["as_default"]:
                self.store.meta["defaults"] = {
                    k: res[k] for k in ("system", "options", "think", "keep_alive")
                }
            self.store.save()
            self.update_status()
            self.notify("Settings saved")
        self.focus_input()

    def action_toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        for b in self.query(Bubble):
            b.refresh_body()
        self.notify("Thinking " + ("shown" if self.show_thinking else "hidden"))

    def action_scroll_chat(self, direction: int) -> None:
        box_ = self.query_one("#messages", VerticalScroll)
        (box_.scroll_page_down if direction > 0 else box_.scroll_page_up)(animate=False)

    # status line
    def status_prefix(self) -> Text:
        return Text(
            " ● " if not self.generating else " ◉ ",
            style=f"bold {ACCENT}" if not self.generating else "bold #ffb454",
        )

    def status_ctx(self, t: Text) -> None:
        """Add `ctx used/limit` to the status line."""
        last = 0
        for m in self.chat.get("messages", []):
            s = m.get("stats") or {}
            last = (s.get("prompt_tokens") or 0) + (s.get("gen_tokens") or 0)
        num_ctx = (self.chat.get("options") or {}).get("num_ctx")
        if not last:
            return
        t.append("  │  ctx ", style=DIM)
        t.append(f"{last:,}" + (f"/{num_ctx:,}" if num_ctx else ""))
        if num_ctx and last / num_ctx > 0.85:
            t.append(" ⚠", style="bold #ffb454")

    def update_status(self) -> None:
        """The one-line footer of the chat tab."""
        c = self.chat
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append_text(self.status_prefix())
        t.append(c.get("model") or "no model (Ctrl+O)", style="bold")
        self.status_ctx(t)
        up, down = self.store.token_totals(c)
        if up or down:
            t.append("  │  session ", style=DIM)
            t.append(f"↑{up:,} ↓{down:,} tok")
        if c.get("think", "auto") != "auto":
            t.append(f"  │  think:{c['think']}", style=DIM)
        if self.pending_images:
            t.append(f"  │  📎{len(self.pending_images)}", style=DIM)
        if self.live:
            live = self.live
            t.append("  │  ", style=DIM)
            t.append(
                f"{live['tok']} tok · {live['tps']:.1f} tok/s · {fmt_dur(live['elapsed'])}",
                style="bold #ffb454",
            )
            if live.get("ttft"):
                t.append(f" · ttft {fmt_dur(live['ttft'])}", style=DIM)
        self.query_one("#statusline", Static).update(t)

    # sending & streaming
    @on(ChatInput.Submitted)
    async def _submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        inp = self.query_one("#input", ChatInput)
        first = text.split()[0][1:].lower()
        if text.startswith("/") and first in CHAT_COMMANDS:
            inp.load_text("")
            await self.run_command(text)
            return
        if self.generating:
            self.notify("Still generating — press Esc to stop.", severity="warning")
            return
        inp.load_text("")
        await self.send(text)

    async def send(self, text: str) -> None:
        """Append the user message and start generating."""
        if not self.chat.get("model"):
            self.notify("Pick a model first (Ctrl+O).", severity="warning")
            return
        msg: dict[str, Any] = {"role": "user", "content": text}
        if self.pending_images:
            msg["images"] = self.pending_images
            self.pending_images = []
        chat = self.chat
        chat["messages"].append(msg)
        if chat.get("title", "New chat") == "New chat":
            chat["title"] = shorten(text)
        chat["updated"] = time.time()
        self.store.save()
        box_ = self.query_one("#messages", VerticalScroll)
        for w in box_.query("#welcome"):
            w.remove()
        await box_.mount(self.make_bubble(msg))
        self.refresh_chat_list()
        await self.start_generation()

    async def start_generation(self) -> None:
        """Mount an empty assistant bubble and stream into it."""
        box_ = self.query_one("#messages", VerticalScroll)
        bubble = Bubble("assistant", model=self.chat["model"])
        bubble.streaming = True
        await box_.mount(bubble)
        box_.scroll_end(animate=False)
        self.generating = True
        self.live = {"tok": 0, "tps": 0.0, "elapsed": 0.0, "ttft": None}
        self.update_status()
        self.run_generation(bubble)

    def consume_chunk(self, obj: dict, acc: dict[str, str]) -> bool:
        """Fold one streamed object into the accumulating text."""
        return consume_chunk(obj, acc)

    def live_stats(self, chunks: int, t0: float, ttft: Optional[float]) -> dict:
        """Rolling token/s numbers for the status line."""
        el = time.monotonic() - t0
        gen_time = max(el - (ttft or 0), 1e-6)
        return {
            "tok": chunks,
            "tps": (chunks - 1) / gen_time if chunks > 1 else 0.0,
            "elapsed": el,
            "ttft": ttft,
        }

    @work(exclusive=True, group="gen")
    async def run_generation(self, bubble: Bubble) -> None:
        box_ = self.query_one("#messages", VerticalScroll)
        acc = {"content": "", "thinking": ""}
        final: dict = {}
        chunks = 0
        ttft: Optional[float] = None
        t0 = time.monotonic()
        last_draw = 0.0
        stopped, error = False, ""
        try:
            async for obj in self.api.stream("/api/chat", build_payload(self.chat)):
                if self.consume_chunk(obj, acc):
                    chunks += 1
                    if ttft is None:
                        ttft = time.monotonic() - t0
                if obj.get("done"):
                    final = obj
                now = time.monotonic()
                if now - last_draw > 0.09 or obj.get("done"):
                    last_draw = now
                    stick = box_.scroll_y >= box_.max_scroll_y - 2
                    bubble.body_text, bubble.think_text = acc["content"], acc["thinking"]
                    bubble.refresh_body()
                    self.live = self.live_stats(chunks, t0, ttft)
                    self.update_status()
                    if stick:
                        self.call_after_refresh(box_.scroll_end, animate=False)
        except asyncio.CancelledError:
            stopped = True
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}" if not isinstance(e, OllamaError) else str(e)
        finally:
            self.finish_generation(
                bubble, acc["content"], acc["thinking"], final, stopped, error, t0, ttft, chunks
            )

    def finish_generation(
        self,
        bubble: Bubble,
        content: str,
        thinking: str,
        final: dict,
        stopped: bool,
        error: str,
        t0: float,
        ttft: Optional[float],
        chunks: int,
    ) -> None:
        """Store the answer and put the UI back into its idle state."""
        elapsed = time.monotonic() - t0
        stats = build_stats(final, chunks, elapsed, ttft)
        bubble.streaming = False
        bubble.body_text, bubble.think_text, bubble.stat = content, thinking, stats
        bubble.stopped, bubble.err = stopped, error
        if store_answer(self.chat, content, thinking, stats, stopped):
            self.store.save()
        elif not error:
            bubble.remove()
        try:
            bubble.refresh_body()
        except Exception:
            pass
        self.generating = False
        self.live = None
        try:
            self.refresh_chat_list()
            self.update_status()
            self.focus_input()
        except Exception:
            pass
        if error and "support" in error.lower() and "think" in error.lower():
            self.notify(
                "This model doesn't support thinking — set it to 'auto' or 'off' in F4.",
                severity="warning",
            )

    def action_stop(self) -> None:
        if self.generating:
            self.workers.cancel_group(self, "gen")

    async def action_regenerate(self) -> None:
        """Drop trailing assistant turns and answer the last user turn again."""
        if self.generating:
            return
        box_ = self.query_one("#messages", VerticalScroll)
        for b in reversed(list(box_.query(Bubble))):
            if b.who != "assistant":
                break
            await b.remove()
        msgs = self.chat["messages"]
        if msgs and msgs[-1]["role"] == "assistant":
            msgs.pop()
        if msgs and msgs[-1]["role"] == "user":
            await self.start_generation()

    async def action_clear_chat(self) -> None:
        """Empty the active chat (asks first unless there is nothing to lose)."""
        if self.generating:
            return
        if self.chat.get("messages"):
            if not await self.push_screen_wait(Confirm("Erase every message?", "Erase")):
                return
        self.chat["messages"] = []
        self.store.save()
        await self.show_chat()

    async def action_undo_chat(self) -> None:
        """Remove the last exchange (assistant turn plus its user turn)."""
        if self.generating or not self.chat.get("messages"):
            return
        msgs = self.chat["messages"]
        while msgs and msgs[-1]["role"] == "assistant":
            msgs.pop()
        if msgs:
            msgs.pop()
        self.store.save()
        await self.show_chat()

    # code copy / export
    def last_assistant(self) -> str:
        for m in reversed(self.chat.get("messages", [])):
            if m["role"] == "assistant":
                return m.get("content", "")
        return ""

    def action_copy_code(self) -> None:
        self.copy_block("")

    def copy_block(self, which: str) -> None:
        """Copy a code block of the last answer ('all' for everything)."""
        text = self.last_assistant()
        if not text:
            self.notify("Nothing to copy yet.", severity="warning")
            return
        if which == "all":
            payload, label = text, "answer"
        else:
            blocks = extract_code_blocks(text)
            if not blocks:
                self.notify("No code blocks in the last answer.", severity="warning")
                return
            try:
                i = int(which) - 1 if which else len(blocks) - 1
                payload = blocks[i][1]
            except (ValueError, IndexError):
                self.notify(f"Pick a block between 1 and {len(blocks)}.", severity="warning")
                return
            label = f"code block #{i + 1} ({blocks[i][0]}, {payload.count(chr(10)) + 1} lines)"
        self.put_on_clipboard(payload, f"Copied {label}")

    def put_on_clipboard(self, text: str, ok_message: str = "Copied") -> bool:
        """System clipboard, falling back to OSC-52."""
        if not system_copy(text):
            self.copy_to_clipboard(text)
        self.notify(ok_message)
        return True

    def export_markdown(self) -> str:
        return chat_markdown(self.chat)

    def action_export_chat(self, path: str = "") -> None:
        dest = (
            Path(path).expanduser()
            if path
            else Path.home()
            / f"{APP_NAME}-chat-{slugify(self.chat.get('title', ''))}-{datetime.now():%Y%m%d-%H%M}.md"
        )
        try:
            dest.write_text(self.export_markdown(), encoding="utf-8")
            self.notify(f"Exported → {dest}")
        except OSError as e:
            self.notify(f"Export failed: {e}", severity="error")

    # slash commands
    async def run_command(self, line: str) -> None:
        """Dispatch `/help`, `/model`, `/cheat`, … to a cmd_ method."""
        cmd, _, arg = line[1:].partition(" ")
        handler = getattr(self, f"cmd_{cmd.lower()}", None)
        if handler is None:
            self.notify(f"Unknown command /{cmd} — try /help", severity="warning")
            return
        if inspect.isawaitable(result := handler(arg.strip())):
            await result
        self.update_status()

    async def cmd_help(self, arg: str) -> None:
        self.action_help()

    async def cmd_new(self, arg: str) -> None:
        await self.action_new_chat()

    async def cmd_clear(self, arg: str) -> None:
        await self.action_clear_chat()

    async def cmd_undo(self, arg: str) -> None:
        await self.action_undo_chat()

    async def cmd_regen(self, arg: str) -> None:
        await self.action_regenerate()

    async def cmd_delete(self, arg: str) -> None:
        self.delete_chat()

    def cmd_rename(self, arg: str) -> None:
        if arg:
            self._do_rename_chat(arg)
        else:
            self.action_rename_chat()

    def cmd_model(self, arg: str) -> None:
        if arg:
            self.set_chat_model(arg)
            self.notify(f"Model → {arg}")
        else:
            self.action_pick_model()

    def cmd_models(self, arg: str) -> None:
        self.action_show_tab(TAB_MODELS)

    def cmd_pull(self, arg: str) -> None:
        self.action_pull(arg)

    def cmd_system(self, arg: str) -> None:
        c = self.chat
        if not arg:
            self.notify(c.get("system") or "(no system prompt)", title="System prompt", timeout=8)
            return
        c["system"] = "" if arg == "-" else arg
        self.store.save()
        self.notify("System prompt " + ("cleared" if arg == "-" else "set"))

    def cmd_think(self, arg: str) -> None:
        if arg not in ("auto", "on", "off"):
            self.notify("Usage: /think auto|on|off", severity="warning")
            return
        self.chat["think"] = arg
        self.store.save()

    def cmd_set(self, arg: str) -> None:
        """`/set key value` for one sampling option (or keep_alive)."""
        key, _, val = arg.partition(" ")
        val = val.strip()
        c = self.chat
        if key == "keep_alive" and val:
            c["keep_alive"] = val
        elif key in OPTION_TYPES and val:
            try:
                c.setdefault("options", {})[key] = OPTION_TYPES[key](val)
            except ValueError:
                self.notify(f"{key} must be {OPTION_TYPES[key].__name__}", severity="error")
        elif key in OPTION_TYPES:
            c.setdefault("options", {}).pop(key, None)
        else:
            self.notify(
                "Usage: /set <" + "|".join(list(OPTION_TYPES) + ["keep_alive"]) + "> <value>",
                severity="warning",
            )
        self.store.save()

    def cmd_settings(self, arg: str) -> None:
        self.action_settings()

    def cmd_stats(self, arg: str) -> None:
        c = self.chat
        up, down = self.store.token_totals(c)
        tps = [
            (m.get("stats") or {}).get("tps")
            for m in c["messages"]
            if (m.get("stats") or {}).get("tps")
        ]
        avg = f"{sum(tps) / len(tps):.1f}" if tps else "–"
        self.notify(
            f"{len(c['messages'])} messages · ↑{up:,} ↓{down:,} tokens · avg {avg} tok/s\n"
            f"options: {c.get('options') or 'defaults'}",
            title="Chat stats",
            timeout=8,
        )

    def cmd_export(self, arg: str) -> None:
        self.action_export_chat(arg)

    def cmd_copy(self, arg: str) -> None:
        self.copy_block(arg)

    def cmd_image(self, arg: str) -> None:
        p = Path(arg.strip("'\"")).expanduser()
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            self.pending_images.append(str(p.resolve()))
            self.notify(f"Attached {p.name} — sent with your next message (needs a vision model).")
        else:
            self.notify("Usage: /image /path/to/picture.png", severity="warning")

    def cmd_cheat(self, arg: str) -> None:
        """Jump to the cheats tab with `arg` in the search box."""
        self.action_show_tab(TAB_CHEATS)
        self.query_one("#cheat-q", Input).value = arg
        self.run_cheat_search(arg)

    def cmd_pkg(self, arg: str) -> None:
        """Jump to the packages tab with `arg` typed in."""
        self.action_show_tab(TAB_PKGS)
        self.query_one("#pkg", Input).value = arg
        self.pkg_hint()

    def action_reload(self) -> None:
        self.reload()

    def selected_model(self) -> Optional[str]:
        t = self.query_one("#models-table", DataTable)
        if t.row_count == 0:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None

    @work(exclusive=True, group="refresh")
    async def reload(self) -> None:
        """Re-read model list + version from Ollama."""
        try:
            ver = await self.api.version()
            tags = await self.api.tags()
        except Exception as e:
            self.sub_title = f"{self.host} · offline"
            self.notify(
                f"Cannot reach Ollama at {self.host}: {e}\nStart it with `ollama serve`.",
                severity="error",
                timeout=8,
            )
            return
        self.sub_title = f"{self.host} · v{ver}"
        self.models = tags
        self.fill_models_table()
        await self.refresh_running()
        self.ensure_chat_model()

    def fill_models_table(self) -> None:
        t = self.query_one("#models-table", DataTable)
        prev = self.selected_model()
        t.clear()
        for m in sorted(self.models, key=lambda m: m["name"]):
            d = m.get("details") or {}
            t.add_row(
                m["name"],
                d.get("parameter_size") or "?",
                d.get("quantization_level") or "?",
                d.get("family") or "?",
                fmt_bytes(m.get("size", 0)),
                ago(m.get("modified_at")),
                key=m["name"],
            )
        if prev:
            with contextlib.suppress(Exception):
                t.move_cursor(row=t.get_row_index(prev))

    async def refresh_running(self) -> None:
        """Fill the 'loaded in memory' table."""
        try:
            running = await self.api.ps()
        except Exception:
            return
        t = self.query_one("#running-table", DataTable)
        t.clear()
        for m in running:
            t.add_row(
                m.get("name", "?"),
                fmt_bytes(m.get("size", 0)),
                placement_of(m),
                str(m.get("context_length", "—")),
                unload_countdown(m.get("expires_at")),
                key=m.get("name", str(uuid.uuid4())),
            )

    async def poll_running(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        with contextlib.suppress(Exception):
            if self.active_tab() == TAB_MODELS:
                await self.refresh_running()

    def ensure_chat_model(self) -> None:
        """Point an empty chat at a model that actually exists."""
        names = {m["name"] for m in self.models}
        if self.chat.get("model") in names:
            self.update_status()
            return
        if not self.chat.get("messages"):
            last = self.store.meta.get("last_model")
            pick = last if last in names else (sorted(names)[0] if names else "")
            if pick:
                self.set_chat_model(pick)
            elif not names:
                self.notify("No models installed yet — press Ctrl+2, then `p` to pull one.", timeout=8)
        self.update_status()

    @on(DataTable.RowHighlighted, "#models-table")
    def _model_hl(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.load_model_info(str(event.row_key.value))

    @on(DataTable.RowSelected, "#models-table")
    def _model_sel(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value:
            self.set_chat_model(str(event.row_key.value))
            self.notify(f"Chat model → {event.row_key.value}")
            self.action_show_tab(TAB_CHAT)

    @work(exclusive=True, group="info")
    async def load_model_info(self, name: str) -> None:
        """Detail pane for the highlighted model."""
        body = self.query_one("#model-info-body", Static)
        try:
            info = await self.api.show(name)
        except Exception as e:
            body.update(Text(f"Could not load details: {e}", style="red"))
            return
        d = info.get("details") or {}
        mi = info.get("model_info") or {}
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=DIM)
        grid.add_column()
        for k, v in [
            ("Family", d.get("family")),
            ("Parameters", d.get("parameter_size")),
            ("Quantization", d.get("quantization_level")),
            ("Format", d.get("format")),
            ("Architecture", mi.get("general.architecture")),
            ("Max context", model_info_int(mi, "context_length")),
            ("Embedding dim", model_info_int(mi, "embedding_length")),
            ("Capabilities", ", ".join(info.get("capabilities") or []) or None),
        ]:
            if v:
                grid.add_row(k, str(v))
        parts: list[RenderableType] = [Text(name, style=f"bold {ACCENT}"), Text(""), grid]
        for title, key, n in [
            ("Parameters", "parameters", 12),
            ("System", "system", 8),
            ("Template", "template", 12),
            ("License", "license", 4),
        ]:
            if info.get(key):
                parts += [Text(f"\n{title}", style="bold"), Text(trim_lines(info[key], n), style="#9a9a9a")]
        body.update(Group(*parts))

    @on(Button.Pressed, ".mbtn")
    def _model_button(self, event: Button.Pressed) -> None:
        if event.button.name:
            getattr(self, f"action_{event.button.name}")()

    def action_pull(self, name: str = "") -> None:
        """Pull a model, asking for the name when not given."""

        def go(n: Optional[str]) -> None:
            if n:
                self.push_screen(PullScreen(self.api, n), lambda _: self.reload())

        if name:
            go(name)
        else:
            self.push_screen(
                TextPrompt(
                    "Pull a model",
                    hint="e.g. llama3.2:3b · qwen2.5-coder:7b · hf.co/user/repo:Q4_K_M",
                ),
                go,
            )

    def action_create_model(self) -> None:
        self.push_screen(CreateScreen(), lambda _: self.reload())

    def action_rename_model(self) -> None:
        old = self.selected_model()
        if old:
            self.push_screen(
                TextPrompt(
                    f"Rename '{old}' to",
                    value=old,
                    hint="Ollama has no rename API: this copies the model, then deletes the old name.",
                ),
                lambda new: self._do_rename(old, new),
            )

    @work(group="modelop")
    async def _do_rename(self, old: str, new: Optional[str]) -> None:
        if not new or new == old:
            return
        names = {m["name"] for m in self.models}
        if new in names or f"{new}:latest" in names:
            if not await self.push_screen_wait(Confirm(f"'{new}' already exists. Overwrite it?", "Overwrite")):
                return
        if not await self._model_op(lambda: self.api.copy(old, new), f"Copy {old}"):
            return
        if not await self._model_op(lambda: self.api.delete(old), f"Delete {old}"):
            return
        for c in self.store.chats.values():
            if c.get("model") == old:
                c["model"] = new
        self.store.save()
        self.notify(f"Renamed {old} → {new}")
        self.reload()

    async def _model_op(self, call, what: str) -> bool:
        """Run an ollama call; notify on failure. False = failed."""
        try:
            await call()
        except Exception as e:
            self.notify(f"{what} failed: {e}", severity="error")
            return False
        return True

    def action_copy_model(self) -> None:
        src = self.selected_model()
        if src:
            self.push_screen(
                TextPrompt(f"Copy '{src}' as", value=src + "-copy"), lambda dst: self._do_copy(src, dst)
            )

    @work(group="modelop")
    async def _do_copy(self, src: str, dst: Optional[str]) -> None:
        if dst and await self._model_op(lambda: self.api.copy(src, dst), "Copy"):
            self.notify(f"Copied {src} → {dst}")
            self.reload()

    def action_delete_model(self) -> None:
        name = self.selected_model()
        if name:
            self.push_screen(
                Confirm(
                    f"Delete model '{name}'?\nThis frees disk space and cannot be undone.", "Delete"
                ),
                lambda ok: self._do_delete(name) if ok else None,
            )

    @work(group="modelop")
    async def _do_delete(self, name: str) -> None:
        if await self._model_op(lambda: self.api.delete(name), "Delete"):
            self.notify(f"Deleted {name}")
            self.reload()

    def action_unload_model(self) -> None:
        name = self.selected_model()
        if name:
            self._do_unload(name)

    @work(group="modelop")
    async def _do_unload(self, name: str) -> None:
        if await self._model_op(lambda: self.api.unload(name), "Unload"):
            self.notify(f"Unloaded {name}")
            await asyncio.sleep(0.4)
            await self.refresh_running()


    # packages tab

    def pkg_terms(self) -> list[str]:
        """Names from the input box, falling back to the highlighted match."""
        typed = self.query_one("#pkg", Input).value.split()
        return typed or ([self.pkg_row] if self.pkg_row else [])

    @on(Input.Changed, "#pkg")
    def _pkg_typed(self, event: Input.Changed) -> None:
        self.pkg_hint()
        self.fill_matches((event.value.split() or [""])[-1])

    @on(Input.Submitted, "#pkg")
    async def _pkg_submitted(self, event: Input.Submitted) -> None:
        await self.action_pkg_install()

    def pkg_hint(self) -> None:
        """Live feedback below the package input."""
        word = (self.query_one("#pkg", Input).value.split() or [""])[-1]
        msg = ""
        if len(norm(word)) >= 2:
            cands = self.idx.match(word)
            if word in cands:
                msg = "exists locally" + (" (installed)" if word in self.idx.installed else " (repo)")
            elif cands:
                msg = "did you mean: " + ", ".join(cands[:4])
            else:
                msg = "no local match (AUR is checked on Enter)"
        if not msg:
            msg = "Enter install · Esc, then i r u o c k s"
        self.query_one("#pkghint", Static).update(Text(msg, style=DIM))

    def fill_matches(self, term: str) -> None:
        """Candidates for the word being typed."""
        t = self.query_one("#matches", DataTable)
        t.clear()
        self.pkg_row = ""
        if len(norm(term)) < 2:
            return
        names = self.idx.match(term, deep=True)
        for name in names:
            t.add_row(
                name,
                pkg_source(self.idx, name),
                "installed" if name in self.idx.installed else "",
                key=name,
            )
        if names:
            with contextlib.suppress(Exception):
                t.move_cursor(row=0)
            self.pkg_row = names[0]

    @on(DataTable.RowHighlighted, "#matches")
    def _match_hl(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.pkg_row = str(event.row_key.value)

    @on(DataTable.RowSelected, "#matches")
    async def _match_sel(self, event: DataTable.RowSelected) -> None:
        """Enter on a candidate installs it."""
        if event.row_key is None or event.row_key.value is None:
            return
        self.query_one("#pkg", Input).value = str(event.row_key.value)
        await self.action_pkg_install()

    @work(thread=True, exclusive=True, group="idx")
    def refresh_index(self) -> None:
        """Rebuild the package index off the event loop."""
        self.idx.refresh()
        counts = pkg_counts(self.idx)
        self.call_from_thread(self.show_pkg_info, counts)

    def show_pkg_info(self, counts: dict) -> None:
        self.counts = counts
        self.query_one("#pkg-info", Static).update(
            pkg_status_text(self.cfg, counts, self.helper)
        )

    def action_reload_index(self) -> None:
        self.refresh_index()

    def action_focus_matches(self) -> None:
        self.query_one("#matches", DataTable).focus()

    def focus_pkg_input(self) -> None:
        self.query_one("#pkg", Input).focus()

    def action_focus_pkg_input(self) -> None:
        self.focus_pkg_input()

    def action_letter(self, key: str) -> None:
        """Dispatch a single-letter key to whatever the active tab needs."""
        target = LETTER_ACTIONS.get(self.active_tab(), {}).get(key)
        if not target:
            return
        handler = getattr(self, f"action_{target}", None)
        if handler is None:
            return
        result = handler()
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    async def run_plan(self, plan: tuple[str, str, bool]) -> None:
        title, script, root = plan
        await self.launch_job(title, script, root)

    async def action_pkg_update(self) -> None:
        if plan := pkg_plan(self.cfg, "update", helper=self.helper):
            await self.run_plan(plan)

    async def action_pkg_orphans(self) -> None:
        if plan := pkg_plan(self.cfg, "orphans", helper=self.helper):
            await self.run_plan(plan)

    async def action_pkg_clean(self) -> None:
        if plan := pkg_plan(self.cfg, "clean", helper=self.helper):
            await self.run_plan(plan)

    async def action_pkg_check(self) -> None:
        if plan := pkg_plan(self.cfg, "check", helper=self.helper):
            await self.run_plan(plan)

    async def action_pkg_search(self) -> None:
        if not (names := self.pkg_terms()):
            self.focus_pkg_input()
            self.notify("type a package name first")
            return
        if plan := pkg_plan(self.cfg, "search", names, helper=self.helper):
            await self.run_plan(plan)

    async def action_pkg_install(self) -> None:
        """Resolve typed names against repo+AUR, then install."""
        names = self.pkg_terms()
        if not names:
            self.focus_pkg_input()
            self.notify("type a package name first")
            return
        picks = await self.resolve_all(names, resolve_install, self.helper)
        if picks is None:
            return
        script = build_install_script(picks, self.helper)
        if not script:
            self.notify("nothing to install", severity="warning")
            return
        await self.launch_job("install " + " ".join(n for n, _ in picks), script)
        self.query_one("#pkg", Input).value = ""
        self.focus_pkg_input()

    async def action_pkg_remove(self) -> None:
        """Resolve against installed packages, confirm, then remove."""
        names = self.pkg_terms()
        if not names:
            self.focus_pkg_input()
            self.notify("type a package name first")
            return
        picks = await self.resolve_all(names, resolve_remove, None)
        if picks is None:
            return
        got = ", ".join(n for n, _ in picks)
        if not await self.push_screen_wait(Confirm(f"Remove {got}?", "Remove")):
            return
        await self.launch_job("remove " + got, build_remove_script([n for n, _ in picks]))
        self.query_one("#pkg", Input).value = ""
        self.focus_pkg_input()

    async def resolve_all(
        self, tokens: list[str], resolver, helper: Optional[str]
    ) -> Optional[list[tuple[str, str]]]:
        """Run `resolver` per token, asking the picker when ambiguous.

        Returns the chosen (name, source) pairs, or None when cancelled.
        """
        picks: list[tuple[str, str]] = []
        for tok in tokens:
            if helper is None:
                res = await asyncio.to_thread(resolver, self.idx, tok)
            else:
                res = await asyncio.to_thread(resolver, self.idx, tok, helper)
            if res[0] == "none":
                self.notify(f"'{tok}' not found", severity="error")
                return None
            if res[0] == "pick":
                choice = await self.push_screen_wait(PkgPick(tok, res[1]))
                if choice is None:
                    return None
                picks.append(choice)
                continue
            if res[1] != tok:
                self.notify(f"{tok} → {res[1]}")
            picks.append((res[1], res[2]))
        return picks

    async def launch_job(self, title: str, script: str, root: bool = True) -> Optional[str]:
        """Start a detached job, asking for sudo when it needs it."""
        password = None
        if root and not IS_ROOT:
            if self.nopass is None:
                self.nopass = await asyncio.to_thread(sudo_nopass)
            if not self.nopass:
                if self.pw is None:
                    self.pw = await self.push_screen_wait(Pass())
                    if self.pw is None:
                        return None
                password = self.pw
        jid = await asyncio.to_thread(new_job, self.cfg, title, script, password)
        self.want = jid
        self.tick_jobs()
        return jid

    # jobs tab

    def tick_jobs(self) -> None:
        """Redraw the job table and append any new log output."""
        infos = jobs_list(self.cfg)
        self.notice_finished(infos)
        if [(i["id"], i["st"]) for i in infos] != self.jobs_sig:
            self.jobs_sig = [(i["id"], i["st"]) for i in infos]
            self.fill_jobs_table(infos)
        self.pump_log(infos)

    def notice_finished(self, infos: list[dict]) -> None:
        """Announce jobs that just left the 'running' state."""
        for i in infos:
            old = self.jobs_last.get(i["id"])
            if old == "running" and i["st"] != "running":
                severity = "information" if i["st"] == "ok" else "error"
                self.notify(f"{i['title']}: {i['st']}", severity=severity)
                self.refresh_index()
            self.jobs_last[i["id"]] = i["st"]

    def fill_jobs_table(self, infos: list[dict]) -> None:
        """Rebuild rows, keeping the cursor on the followed job."""
        keep, self.want = self.want or self.jobs_cursor, None
        t = self.query_one("#jobs", DataTable)
        t.clear()
        for i in infos:
            color = "yellow" if i["st"] == "running" else "green" if i["st"] == "ok" else "red"
            t.add_row(i["id"][-6:], Text(i["st"], style=color), i["title"], i["time"], key=i["id"])
        ids = [i["id"] for i in infos]
        if keep in ids:
            with contextlib.suppress(Exception):
                t.move_cursor(row=ids.index(keep))

    def pump_log(self, infos: list[dict]) -> None:
        """Write the followed job's new log lines into the log pane."""
        if not self.jobs_cursor:
            return
        info = next((i for i in infos if i["id"] == self.jobs_cursor), None)
        if info is None:
            return
        text, off = job_read_log(
            self.cfg, self.jobs_cursor, self.log_off, info["st"] == "running"
        )
        self.log_off = off
        log = self.query_one("#jobslog", RichLog)
        for line in text.split("\n"):
            log.write(Text(line.split("\r")[-1]))

    @on(DataTable.RowHighlighted, "#jobs")
    def _job_hl(self, event: DataTable.RowHighlighted) -> None:
        jid = event.row_key.value if event.row_key else None
        if jid and jid != self.jobs_cursor:
            self.jobs_cursor, self.log_off = str(jid), 0
            self.query_one("#jobslog", RichLog).clear()
            self.pump_log(jobs_list(self.cfg))

    def action_job_kill(self) -> None:
        if self.jobs_cursor and job_kill(self.cfg, self.jobs_cursor):
            self.notify("SIGTERM sent")
        else:
            self.notify("that job is not running", severity="warning")
        self.tick_jobs()

    def action_job_clear(self) -> None:
        """Delete finished job dirs, keeping running ones."""
        n = jobs_clear(self.cfg)
        self.jobs_cursor, self.log_off = None, 0
        self.query_one("#jobslog", RichLog).clear()
        self.notify(f"removed {n} finished job(s)")
        self.tick_jobs()

    def action_job_refresh(self) -> None:
        self.tick_jobs()

    def action_follow_log(self) -> None:
        """Follow the newest job instead of waiting for the cursor."""
        self.jobs_sig = []
        self.tick_jobs()

    # cheats tab

    def action_reload_cheats(self) -> None:
        self.reload_cheats()

    def reload_cheats(self) -> None:
        """Re-read every sheet, then rerun the current query."""
        self.cheats = load_cheats(cheat_dir(self.cfg))
        self.fill_sheets()
        self.run_cheat_search(self.cheat_query())

    def cheat_query(self) -> str:
        return self.query_one("#cheat-q", Input).value

    def fill_sheets(self) -> None:
        """Left pane: every sheet with its cheat count, plus an 'all' row."""
        ol = self.query_one("#cheat-sheets", OptionList)
        ol.clear_options()
        counts = Counter(c.tool for c in self.cheats)
        opts = [Option(f"all   {len(self.cheats)}", id="*")]
        for tool in sorted(counts):
            opts.append(Option(f"{tool}   {counts[tool]}", id=tool))
        ol.add_options(opts)
        wanted = self.sheet_filter if self.sheet_filter in counts else "*"
        ol.highlighted = 0 if wanted == "*" else sorted(counts).index(wanted) + 1

    @on(OptionList.OptionSelected, "#cheat-sheets")
    def _sheet_selected(self, event: OptionList.OptionSelected) -> None:
        """Filter the results down to one sheet."""
        self.sheet_filter = "" if event.option.id == "*" else str(event.option.id)
        self.run_cheat_search(self.cheat_query())
        self.query_one("#cheats", DataTable).focus()

    def run_cheat_search(self, query: str) -> None:
        """Rank cheats for `query` and refill the table."""
        pool = self.cheats
        if self.sheet_filter:
            pool = [c for c in pool if c.tool == self.sheet_filter]
        self.cheat_matches = cheat_search(pool, query, limit=200)
        self.fill_cheats()

    def fill_cheats(self) -> None:
        """Rebuild the results table and its detail pane."""
        t = self.query_one("#cheats", DataTable)
        t.clear()
        for i, m in enumerate(self.cheat_matches):
            t.add_row(
                m.cheat.tool,
                m.cheat.category,
                Text(m.cheat.command),
                Text(m.cheat.description),
                key=str(i),
            )
        if not self.cheat_matches:
            self.cheat_row = -1
            self.show_cheat_detail()
            return
        self.cheat_row = min(max(self.cheat_row, 0), len(self.cheat_matches) - 1)
        with contextlib.suppress(Exception):
            t.move_cursor(row=self.cheat_row)
        self.show_cheat_detail()

    def current_cheat(self) -> Optional[Cheat]:
        if 0 <= self.cheat_row < len(self.cheat_matches):
            return self.cheat_matches[self.cheat_row].cheat
        return None

    def show_cheat_detail(self) -> None:
        cheat = self.current_cheat()
        detail = self.query_one("#cheat-detail", Static)
        detail.update(
            cheat_row_text(cheat)
            if cheat
            else Text("No cheats match. Press `a` to add one.", style=DIM)
        )

    @on(Input.Changed, "#cheat-q")
    def _cheat_typed(self, event: Input.Changed) -> None:
        self.cheat_row = 0
        self.run_cheat_search(event.value)

    @on(DataTable.RowHighlighted, "#cheats")
    def _cheat_hl(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.cheat_row = int(event.row_key.value)
        self.show_cheat_detail()

    @on(DataTable.RowSelected, "#cheats")
    async def _cheat_selected(self, event: DataTable.RowSelected) -> None:
        await self.action_run_cheat()

    def focus_cheats(self) -> None:
        self.query_one("#cheat-q", Input).focus()

    def action_focus_cheats(self) -> None:
        self.focus_cheats()

    async def action_run_cheat(self) -> None:
        """Run the highlighted cheat as a detached job."""
        cheat = self.current_cheat()
        if not cheat:
            return
        script = cheat_script(cheat)
        question = f"Run as a detached job?\n  {script}"
        if not await self.push_screen_wait(Confirm(question, "Run")):
            return
        jid = await self.launch_job(f"cheat {cheat.tool}", script, root=False)
        if jid:
            self.notify(f"job {jid[-6:]} started — Ctrl+5 to watch")

    def action_copy_cheat(self) -> None:
        if cheat := self.current_cheat():
            self.put_on_clipboard(cheat_script(cheat), "Copied command")
        else:
            self.notify("nothing selected", severity="warning")

    def action_add_cheat(self) -> None:
        cheat = self.current_cheat()
        tool = self.sheet_filter or (cheat.tool if cheat else "")
        self.push_screen(CheatAddScreen(tool, cheat_dir(self.cfg)), self._do_add_cheat)

    def _do_add_cheat(self, values: Optional[tuple[str, str, str, str]]) -> None:
        if not values:
            return
        tool, command, category, description = values
        path, created, used = cheat_add(
            cheat_dir(self.cfg), tool, command, description, category
        )
        note = " (created sheet)" if created else ""
        self.notify(f"added to {used}{note} in {path.name}")
        self.reload_cheats()

    def action_delete_cheat(self) -> None:
        if not (cheat := self.current_cheat()):
            return
        question = f"Delete from {cheat.tool} › {cheat.category}?\n  {cheat.command}"
        self.push_screen(Confirm(question, "Delete"), self._do_delete_cheat)

    def _do_delete_cheat(self, ok: Optional[bool]) -> None:
        cheat = self.current_cheat()
        if not ok or not cheat:
            return
        path, n = cheat_delete_cmd(cheat_dir(self.cfg), cheat.tool, cheat.command)
        self.notify(f"removed {n} line(s) from {path.name}", severity="info" if n else "warning")
        self.reload_cheats()

    def action_edit_sheet(self) -> None:
        cheat = self.current_cheat()
        if not cheat:
            return
        path, _ = sheet_edit(cheat_dir(self.cfg), cheat.tool)
        self.notify(f"opened {path.name}")
        self.reload_cheats()

    def action_new_sheet(self) -> None:
        self.push_screen(TextPrompt("New sheet (tool name)"), self._do_new_sheet)

    def _do_new_sheet(self, tool: Optional[str]) -> None:
        if not tool:
            return
        path = sheet_new(cheat_dir(self.cfg), tool)
        self.notify(f"created {path.name}")
        self.reload_cheats()

def placement_of(m: dict) -> str:
    """'CPU', 'GPU' or '80% GPU' for a loaded model."""
    size, vram = m.get("size", 0), m.get("size_vram", 0)
    if not vram:
        return "CPU"
    return "GPU" if vram >= size else f"{vram / size:.0%} GPU"


def unload_countdown(expires_at: Optional[str]) -> str:
    """'12m30s', 'now', 'never' or '—' for when a model is dropped."""
    ts = parse_ts(expires_at)
    if ts is None:
        return "—"
    if ts < 0:
        return "never"
    return "now" if ts < time.time() else fmt_dur(ts - time.time())


def model_info_int(model_info: dict, suffix: str) -> Optional[str]:
    """'131072' for e.g. "...context_length", or None when absent."""
    val = next((v for k, v in model_info.items() if k.endswith(suffix)), None)
    return f"{val:,}" if isinstance(val, int) else None


def sys_app(cfg: Config, tab: str = TAB_CHAT) -> "SysApp":
    """The app, optionally opening on a given tab."""
    return SysApp(cfg, start_tab=tab)


def cheat_row_text(cheat: Cheat) -> RenderableType:
    """Right-hand pane content for one cheat."""
    t = Text()
    t.append(cheat.command, style=f"bold {ACCENT}")
    t.append(f"   [{cheat.tool} › {cheat.category}]", style=DIM)
    parts: list[RenderableType] = [t, Text("")]
    if cheat.description:
        parts.append(Text(cheat.description))
    parts += [
        Text(""),
        Text(cheat.source.name, style=DIM),
        Text("Enter run  ·  y copy  ·  d delete", style=DIM),
    ]
    return Group(*parts)


def pkg_source(idx: PkgIndex, name: str) -> str:
    """'repo', 'repo+local' or 'aur/local' for a package row."""
    if name in idx.repo:
        return "repo+local" if name in idx.installed else "repo"
    return "aur/local"


def pkg_status_text(
    cfg: Config, counts: dict, helper: Optional[str]
) -> RenderableType:
    """Right-hand pane content for the packages tab."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM)
    grid.add_column()
    for k, v in [
        ("AUR helper", helper or "none (pacman only)"),
        ("installed", str(counts.get("installed", 0))),
        ("in repos", str(counts.get("repo", 0))),
        ("orphans", str(counts.get("orphans", 0))),
        ("updates", str(counts.get("updates", 0))),
        ("jobs dir", str(jobs_dir(cfg))),
    ]:
        grid.add_row(k, v)
    return Group(
        Text("Index", style=f"bold {ACCENT}"),
        Text(""),
        grid,
        Text(""),
        Text(
            "Enter install  ·  i/r/u/o/c/k/s with the list focused  ·  Esc returns to the input",
            style=DIM,
        ),
    )


# web

# a PyInstaller one-file build unpacks datas under _MEIPASS
BUNDLED_STATIC = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "static"


def sse(obj: Any) -> str:
    """One server-sent event line."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def asset_dirs(cfg: Config) -> list[Path]:
    """Asset search path: the user's override dir first, then the bundled copy."""
    return [Path(cfg.get("web.assets") or (CONFIG_HOME / "web")), BUNDLED_STATIC]


def read_asset(cfg: Config, name: str) -> Optional[str]:
    """First file called `name` found along the asset search path."""
    for directory in asset_dirs(cfg):
        try:
            return (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return None


def web_init(cfg: Config) -> str:
    """`window.INIT = {...}`: theme colours and flags the page reads once."""
    values = {
        "title": cfg.get("web.title", APP_NAME),
        "accent": cfg.get("app.accent", ACCENT),
        "background": cfg.get("app.background", BG),
        "surface": cfg.get("app.surface", SURFACE),
        "panel": cfg.get("app.panel", PANEL),
        "foreground": cfg.get("app.foreground", FG),
        "dim": cfg.get("app.dim", DIM),
        "line": cfg.get("app.line", "#333333"),
        "log_bg": cfg.get("app.log_bg", "#0b0b0b"),
        "code_theme": cfg.get("app.code_theme", CODE_THEME),
        "allow_root_actions": bool(cfg.get("web.allow_root_actions", True)),
        "cheat_dir": str(cheat_dir(cfg)),
    }
    return "window.INIT = " + json.dumps(values, ensure_ascii=False) + ";\n"


def web_page(cfg: Config) -> str:
    """index.html from the override dir when present, else the bundled one."""
    return read_asset(cfg, "index.html") or "<p>index.html not found</p>"


def web_guard(cfg: Config) -> None:
    """403 unless package/cheat execution over HTTP is allowed."""
    from fastapi import HTTPException

    if not cfg.get("web.allow_root_actions", True):
        raise HTTPException(403, "set web.allow_root_actions = true in config.toml")


class ChatSend(BaseModel):
    text: str
    chat_id: str = ""


class ChatPick(BaseModel):
    id: str


class ChatRename(BaseModel):
    id: str
    title: str


class ModelRef(BaseModel):
    name: str


class CheatNew(BaseModel):
    tool: str
    command: str
    description: str = ""
    category: str = ""


class CheatDel(BaseModel):
    tool: str
    command: str = ""


class CheatRun(BaseModel):
    command: str


class PkgRun(BaseModel):
    action: str
    names: list[str] = []
    picks: list[list[str]] = []
    password: str = ""


def build_web(cfg: Config) -> Any:
    """The FastAPI app: one shared Ollama client, chat store and job runner."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

    api = Ollama(ollama_host(cfg))
    store = ChatStore(cfg)
    idx = PkgIndex()
    app = FastAPI(title=str(cfg.get("web.title", APP_NAME)), docs_url="/api/docs")

    def active_chat() -> dict:
        chat = store.chats.get(store.meta.get("active", ""))
        if not chat:
            chat = store.new_chat(store.meta.get("last_model", ""))
            store.meta["active"] = chat["id"]
        return chat

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(web_page(cfg))

    @app.get("/assets/init.js")
    def init_js() -> Any:
        return PlainTextResponse(web_init(cfg), media_type="application/javascript")

    @app.get("/assets/{name}")
    def asset(name: str) -> Any:
        """Only the two bundled files, so the dir can't be listed."""
        media = {"app.css": "text/css", "app.js": "application/javascript"}
        if name not in media or (text := read_asset(cfg, name)) is None:
            raise HTTPException(404, "no such asset")
        return PlainTextResponse(text, media_type=media[name])

    @app.get("/api/state")
    async def state() -> dict:
        host = ollama_host(cfg)
        version, reachable = "", False
        try:
            version, reachable = await Ollama(host).version(), True
        except Exception:
            pass
        counts = pkg_counts(idx) if idx.installed or idx.repo else {}
        return {
            "title": cfg.get("web.title", APP_NAME),
            "accent": cfg.get("app.accent", ACCENT),
            "host": host,
            "ollama_ok": reachable,
            "version": version,
            "helper": aur_helper(cfg),
            "cheats": len(load_cheats(cheat_dir(cfg))),
            "jobs": len(jobs_list(cfg, limit=100)),
            "packages": counts,
            "allow_root_actions": bool(cfg.get("web.allow_root_actions", True)),
            "cheat_dir": str(cheat_dir(cfg)),
            "jobs_dir": str(jobs_dir(cfg)),
        }

    # chat
    @app.get("/api/chats")
    def chats() -> list[dict]:
        return [
            {"id": c["id"], "title": c.get("title", "Chat"), "model": c.get("model", ""),
             "updated": c.get("updated", 0), "messages": len(c.get("messages", []))}
            for c in store.sorted_chats()
        ]

    @app.get("/api/chat")
    def current_chat() -> dict:
        chat = active_chat()
        return dict(chat, active=store.meta.get("active"))

    @app.post("/api/chats")
    def new_chat() -> dict:
        chat = store.new_chat(store.meta.get("last_model", ""))
        store.meta["active"] = chat["id"]
        store.save()
        return chat

    @app.post("/api/chats/select")
    def select_chat(body: ChatPick) -> dict:
        if body.id not in store.chats:
            raise HTTPException(404, "no such chat")
        store.meta["active"] = body.id
        store.save()
        return store.chats[body.id]

    @app.post("/api/chats/rename")
    def rename_chat(body: ChatRename) -> dict:
        chat = store.chats.get(body.id)
        if not chat:
            raise HTTPException(404, "no such chat")
        chat["title"] = body.title
        store.save()
        return chat

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: str) -> dict:
        store.chats.pop(chat_id, None)
        if store.meta.get("active") == chat_id:
            rest = store.sorted_chats()
            store.meta["active"] = rest[0]["id"] if rest else store.new_chat()["id"]
        store.save()
        return {"ok": True}

    @app.post("/api/chat/send")
    async def send(body: ChatSend) -> Any:
        """Stream the assistant answer as server-sent events."""
        web_guard(cfg)
        chat = active_chat()
        if not chat.get("model"):
            raise HTTPException(400, "no model selected")
        if not (text := body.text.strip()):
            raise HTTPException(400, "empty message")
        if body.chat_id and body.chat_id in store.chats:
            chat = store.chats[body.chat_id]
            store.meta["active"] = chat["id"]
        msg = {"role": "user", "content": text}
        chat["messages"].append(msg)
        if chat.get("title", "New chat") == "New chat":
            chat["title"] = shorten(text)
        chat["updated"] = time.time()
        store.save()

        async def events() -> AsyncIterator[str]:
            acc = {"content": "", "thinking": ""}
            chunks, ttft, final, stored = 0, None, {}, False
            t0 = time.monotonic()
            yield sse({"type": "user", "message": msg, "chat_id": chat["id"]})
            try:
                async for obj in api.stream("/api/chat", build_payload(chat)):
                    if consume_chunk(obj, acc):
                        chunks += 1
                        if ttft is None:
                            ttft = time.monotonic() - t0
                        yield sse(
                            {
                                "type": "delta",
                                "content": obj.get("message", {}).get("content", ""),
                                "thinking": obj.get("message", {}).get("thinking", ""),
                            }
                        )
                    if obj.get("done"):
                        final = obj
                stats = build_stats(final, chunks, time.monotonic() - t0, ttft)
                store_answer(chat, acc["content"], acc["thinking"], stats)
                store.save()
                stored = True
                yield sse({"type": "done", "stats": stats, "chat_id": chat["id"]})
            except Exception as e:
                yield sse({"type": "error", "error": str(e)})
            finally:
                # Stop button or closed tab: keep what the model managed to say
                if not stored and (acc["content"] or acc["thinking"]):
                    stats = build_stats(final, chunks, time.monotonic() - t0, ttft)
                    store_answer(chat, acc["content"], acc["thinking"], stats, True)
                    store.save()

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/chat/export")
    def export_chat() -> str:
        """The active chat as Markdown."""
        return chat_markdown(active_chat())

    @app.post("/api/chat/model")
    def pick_model(body: ModelRef) -> dict:
        """Point the active chat at a model and remember it as the default."""
        chat = active_chat()
        chat["model"] = body.name
        chat["updated"] = time.time()
        store.meta["last_model"] = body.name
        store.save()
        return {"model": body.name}

    # models

    @app.get("/api/models")
    async def models() -> list[dict]:
        try:
            return await api.tags()
        except Exception as e:
            raise HTTPException(502, f"ollama unreachable: {e}")

    @app.post("/api/models/pull")
    async def pull(body: ModelRef) -> Any:
        """Stream ollama's pull progress as server-sent events."""
        web_guard(cfg)

        async def events() -> AsyncIterator[str]:
            try:
                async for obj in api.stream(
                    "/api/pull", {"model": body.name, "name": body.name, "stream": True}
                ):
                    yield sse(obj)
            except Exception as e:
                yield sse({"error": str(e), "done": True})

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/models/unload")
    async def unload(body: ModelRef) -> dict:
        await api.unload(body.name)
        return {"ok": True}

    @app.delete("/api/models/{name}")
    async def delete_model(name: str) -> dict:
        web_guard(cfg)
        await api.delete(name)
        return {"ok": True}

    # cheats
    @app.get("/api/cheats")
    def cheats(q: str = "", tool: str = "") -> list[dict]:
        entries = load_cheats(cheat_dir(cfg))
        if tool:
            entries = [c for c in entries if c.tool == tool]
        matches = cheat_search(entries, q, limit=200) if q else [CheatMatch(c, 0) for c in entries[:200]]
        return [
            {
                "tool": m.cheat.tool,
                "category": m.cheat.category,
                "command": m.cheat.command,
                "description": m.cheat.description,
                "sheet": m.cheat.source.name,
                "score": round(m.score, 1),
            }
            for m in matches
        ]

    @app.get("/api/cheats/sheets")
    def sheets() -> list[str]:
        return cheat_tools(load_cheats(cheat_dir(cfg)))

    @app.post("/api/cheats")
    def add_cheat(body: CheatNew) -> dict:
        web_guard(cfg)
        path, created, category = cheat_add(
            cheat_dir(cfg), body.tool, body.command, body.description,
            body.category or None,
        )
        return {"ok": True, "sheet": path.name, "category": category, "created": created}

    @app.delete("/api/cheats")
    def delete_cheat(body: CheatDel) -> dict:
        path, removed = cheat_delete_cmd(cheat_dir(cfg), body.tool, body.command)
        return {"ok": True, "removed": removed, "sheet": path.name}

    @app.post("/api/cheats/run")
    def run_cheat(body: CheatRun) -> dict:
        """Run a cheat line as a detached job (needs web.allow_root_actions)."""
        web_guard(cfg)
        if not (script := body.command.strip()):
            raise HTTPException(400, "empty command")
        return {"id": new_job(cfg, "cheat " + script.split(" ")[0], script)}

    # packages

    @app.get("/api/packages")
    def packages(q: str = "", refresh: int = 0) -> dict:
        if refresh or (not idx.installed and not idx.repo):
            idx.refresh()
        names = idx.match(q, deep=True) if q.strip() else []
        return {
            "matches": [
                {"name": n, "source": pkg_source(idx, n), "installed": n in idx.installed}
                for n in names
            ],
            "counts": pkg_counts(idx),
        }

    @app.post("/api/packages/resolve")
    def resolve(body: PkgRun) -> dict:
        """Work out what to run, or ask the client to pick an ambiguity."""
        helper = aur_helper(cfg)
        picks: list[list[str]] = [[n, s] for n, s in body.picks]
        if not picks:
            for tok in body.names:
                res = resolve_install(idx, tok, helper) if body.action == "install" else resolve_remove(idx, tok)
                if res[0] == "none":
                    raise HTTPException(404, f"'{tok}' not found")
                if res[0] == "pick":
                    return {"need_pick": {"term": tok, "options": res[1]}}
                picks.append([res[1], res[2]])
        if body.action == "install":
            script = build_install_script([(n, s) for n, s in picks], helper)
        elif body.action == "remove":
            script = build_remove_script([n for n, _ in picks])
        else:
            plan = pkg_plan(cfg, body.action, body.names, helper)
            if not plan:
                raise HTTPException(400, "unknown action")
            script = plan[1]
        return {"ok": True, "script": script, "picks": picks}

    @app.post("/api/packages/run")
    async def run_package(body: PkgRun) -> dict:
        """Start a package job (install/remove/update/...)."""
        web_guard(cfg)
        from asyncio import to_thread

        resolved = await to_thread(resolve, body)
        if "need_pick" in resolved:
            return resolved
        root = body.action in ("install", "remove", "update", "orphans", "clean")
        password = (body.password or None) if root else None
        if password and not IS_ROOT and not await to_thread(sudo_ok, password):
            raise HTTPException(403, "wrong sudo password")
        title = body.action + (" " + " ".join(body.names) if body.names else "")
        jid = await to_thread(new_job, cfg, title, resolved["script"], password)
        return {"id": jid, "script": resolved["script"], "picks": resolved["picks"]}

    @app.post("/api/packages/index")
    def refresh_index() -> dict:
        idx.refresh()
        return {"counts": pkg_counts(idx)}

    # jobs
    @app.get("/api/jobs")
    def job_list() -> list[dict]:
        return jobs_list(cfg, limit=100)

    @app.get("/api/jobs/{jid}/log")
    def job_log(jid: str, offset: int = 0) -> dict:
        info = job_info(cfg, jid)
        if not info:
            raise HTTPException(404, "no such job")
        text, new_offset = job_read_log(cfg, jid, offset, info["st"] == "running")
        return {"text": text, "offset": new_offset, "status": info["st"]}

    @app.post("/api/jobs/{jid}/kill")
    def kill_job(jid: str) -> dict:
        web_guard(cfg)
        return {"ok": job_kill(cfg, jid)}

    @app.delete("/api/jobs")
    def clear_jobs() -> dict:
        return {"removed": jobs_clear(cfg)}

    return app


def run_web(cfg: Config, host: str = "", port: int = 0, open_browser: bool = False) -> None:
    """Serve the web UI (uvicorn)."""
    import uvicorn

    host = host or str(cfg.get("web.host", "127.0.0.1"))
    port = port or int(cfg.get("web.port", 8787))
    url = f"http://{host}:{port}"
    print(f"{APP_NAME} web on {url}")
    if not cfg.get("web.allow_root_actions", True):
        print("  web.allow_root_actions = false  -> install/remove/run are disabled")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  warning: {host} is not localhost and package actions are enabled")
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(build_web(cfg), host=host, port=port, log_level="warning")


# cli

TAB_ALIASES = {
    "c": TAB_CHAT, "chat": TAB_CHAT,
    "m": TAB_MODELS, "model": TAB_MODELS, "models": TAB_MODELS,
    "ch": TAB_CHEATS, "sheet": TAB_CHEATS, "sheets": TAB_CHEATS, "cheats": TAB_CHEATS,
    "p": TAB_PKGS, "pkg": TAB_PKGS, "pkgs": TAB_PKGS, "packages": TAB_PKGS,
    "j": TAB_JOBS, "job": TAB_JOBS, "jobs": TAB_JOBS,
}


def resolve_tab(name: str) -> str:
    """Map a --tab argument onto a tab id (unknown values open the chat)."""
    return TAB_ALIASES.get(str(name).strip().lower(), TAB_CHAT)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Everything `sys` accepts on the command line."""
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="One TUI (plus a tiny web UI) for Ollama, packages and cheat sheets.",
        epilog=(
            "examples:\n"
            "  sys                      open the TUI\n"
            "  sys --tab pkgs           start on the packages tab\n"
            "  sys --web --open         serve the web UI and open a browser\n"
            "  sys --cheat 'git rebase' one-shot cheat-sheet search\n"
            "  sys --set web.port=9000  override any option for this run\n"
            "  sys --init-config        write ~/.config/sys/config.toml\n"
        ),
    )
    p.add_argument("-w", "--web", action="store_true", help="serve the web UI instead of the TUI")
    p.add_argument("--host", default="", help="web bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=0, help="web port (default 8787)")
    p.add_argument("--open", dest="open_browser", action="store_true", help="open the web UI in a browser")
    p.add_argument("--tab", default="", metavar="TAB", help="chat, models, cheats, pkgs or jobs")
    p.add_argument("--cheat", default="", metavar="QUERY", help="search cheat sheets, print, then exit")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                   help="override a config value for this run (repeatable)")
    p.add_argument("--config", default="", metavar="PATH", help="path to config.toml")
    p.add_argument("--init-config", action="store_true", help="write the default config.toml and exit")
    p.add_argument("--force", action="store_true", help="let --init-config overwrite an existing file")
    p.add_argument("--show-config", action="store_true", help="print the effective configuration and exit")
    p.add_argument("-v", "--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return p.parse_args(argv)


def overrides_from(pairs: list[str]) -> tuple[dict, list[str]]:
    """Turn `--set web.port=9000` pairs into one nested override dict."""
    out: dict = {}
    errors: list[str] = []
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            errors.append(f"bad --set {pair!r} (want KEY=VALUE)")
            continue
        setdig(out, key.strip(), parse_value(raw))
    return out, errors


def show_config(cfg: Config) -> None:
    """Print the effective configuration, noting where it was read from."""
    print(f"# source: {cfg.path}")
    print(cfg.dumps())


def write_config(cfg: Config, force: bool = False) -> int:
    """Write the default config.toml, refusing to clobber an existing one."""
    if cfg.path.exists() and not force:
        print(f"{cfg.path} already exists — pass --force to overwrite", file=sys.stderr)
        return 1
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.path.write_text(toml_dump(DEFAULTS), encoding="utf-8")
    print(f"wrote {cfg.path}")
    return 0


def cheat_lookup(cfg: Config, query: str) -> int:
    """`--cheat`: rank sheets in the terminal, print them, then exit."""
    matches = cheat_search(load_cheats(cheat_dir(cfg)), query, limit=20)
    if not matches:
        print(f"no cheats match {query!r}", file=sys.stderr)
        return 1
    from rich.console import Console

    Console().print(render_cheats(matches, query))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the `sys` command."""
    args = parse_args(argv)
    overrides, errors = overrides_from(args.sets)
    if errors:
        for err in errors:
            print(f"{APP_NAME}: {err}", file=sys.stderr)
        return 2
    cfg = Config.load(args.config, overrides)
    apply_theme(cfg)
    if args.init_config:
        return write_config(cfg, args.force)
    if args.show_config:
        show_config(cfg)
        return 0
    if args.cheat:
        return cheat_lookup(cfg, args.cheat)
    if args.web:
        run_web(cfg, args.host, args.port, args.open_browser or bool(cfg.get("web.open_browser")))
        return 0
    run_tui(cfg, resolve_tab(args.tab))
    return 0


def run_tui(cfg: Config, tab: str) -> None:
    """Boot the Textual app on the requested tab."""
    sys_app(cfg, tab).run()


if __name__ == "__main__":
    raise SystemExit(main())