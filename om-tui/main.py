#!/usr/bin/env python3
"""
ollama-tui — a full-featured terminal UI for Ollama (Textual + Rich).

Features
  * Multi-chat sidebar (persisted), streaming replies, Esc to stop, regenerate, undo
  * Markdown rendering, numbered + syntax-highlighted code blocks, one-key copy
  * Live + final stats: tokens in/out, tok/s, time-to-first-token, load time, ctx use
  * Thinking-model support (collapsible-style dim panel), image attachments (/image)
  * Per-chat settings: system prompt, temperature, top_p, top_k, num_ctx, keep_alive...
  * Models tab: list, details (/api/show), pull with progress, rename, copy, delete,
    unload, loaded-models table
  * Create a model from a folder (folder picker): import directly, or convert the
    folder to .gguf with llama.cpp first, or use an existing .gguf; optional quantize

Run:   pip install textual httpx   &&   python ollama_tui.py [--host http://host:11434]
"""
from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, DataTable, DirectoryTree, Footer, Header, Input, Label,
    OptionList, ProgressBar, RadioButton, RadioSet, RichLog, Select, Static,
    TabbedContent, TabPane, TextArea, Tree,
)
from textual.widgets.option_list import Option

# ───────────────────────────── look & feel ──────────────────────────────
BG = "#0e0e0e"
SURFACE = "#141414"
PANEL = "#1a1a1a"
ACCENT = "#4da3ff"          # flame-blue accent — change to taste
DIM = "#7d7d7d"
CODE_THEME = "monokai"
CODE_BG = "#121212"

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ollama-tui"

# --- helpers (pure, no UI) -------------------------------------------------


def normalize_host(raw: Optional[str]) -> str:
    raw = (raw or "127.0.0.1:11434").strip()
    if "://" not in raw:
        raw = "http://" + raw
    rest = raw.split("://", 1)[1].split("/")[0]
    if ":" not in rest:
        raw = raw.rstrip("/") + ":11434"
    return raw.rstrip("/")


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
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return raw


FENCE = re.compile(
    r"^[ \t]*```[ \t]*([\w+#.\-]*)[^\n]*\n(.*?)(?:^[ \t]*```[ \t]*$|\Z)",
    re.M | re.S,
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    return [((m.group(1) or "text"), m.group(2).rstrip("\n")) for m in FENCE.finditer(text)]


def last_segment(line: str) -> str:
    """Terminal-style: text after the last carriage return."""
    parts = [p for p in line.split("\r") if p.strip()]
    return parts[-1] if parts else ""


def trim_lines(s: str, n: int) -> str:
    lines = s.strip().splitlines()
    return "\n".join(lines[:n]) + (f"\n… (+{len(lines) - n} lines)" if len(lines) > n else "")


def system_copy(text: str) -> bool:
    cands: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        cands.append(["wl-copy"])
    cands += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]]
    for cmd in cands:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), timeout=3, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
    return False


def find_llama_cpp() -> str:
    env = os.environ.get("LLAMA_CPP_DIR")
    cands = [Path(env)] if env else []
    cands += [Path.home() / "llama.cpp", Path.home() / "src" / "llama.cpp", Path("/opt/llama.cpp")]
    for c in cands:
        if (c / "convert_hf_to_gguf.py").exists():
            return str(c)
    which = shutil.which("convert_hf_to_gguf.py")
    return str(Path(which).parent) if which else str(cands[0] if cands else Path.home() / "llama.cpp")


def inspect_source(p: str) -> tuple[str, str]:
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

# --- end helpers -----------------------------------------------------------

OPTION_FIELDS: list[tuple[str, type]] = [
    ("temperature", float), ("top_p", float), ("top_k", int), ("num_ctx", int),
    ("num_predict", int), ("repeat_penalty", float), ("seed", int),
]
OPTION_TYPES = dict(OPTION_FIELDS)

COMMANDS = {
    "help", "new", "clear", "undo", "regen", "model", "models", "pull", "system", "rename",
    "delete", "export", "copy", "image", "set", "think", "settings", "stats",
}

HELP = """\
# Ollama TUI

## Keys
| Key | Action |
|---|---|
| **Enter** | send message |
| **Ctrl+J** / Shift+Enter | newline in message |
| **Esc** | stop generating |
| **Ctrl+N** | new chat |
| **Ctrl+O** | switch model for this chat |
| **Ctrl+R** | regenerate last answer |
| **Ctrl+Y** | copy last code block (`/copy 2` = block #2, `/copy all` = whole answer) |
| **Ctrl+S** | export chat to Markdown |
| **Ctrl+T** | show / hide model thinking |
| **F2 / F3** | Chat tab / Models tab |
| **F4** | chat settings (system prompt, temperature, num_ctx, …) |
| **F6** | rename chat |
| **Alt+↑ / Alt+↓** | scroll chat while typing |
| **Ctrl+Q** | quit |

## Models tab
`p` pull · `n` create from folder/.gguf · `r` rename · `c` copy · `d` delete · `u` unload · `Enter` use in chat · `F5` refresh

## Slash commands
`/new` `/clear` `/undo` `/regen` `/model [name]` `/models` `/pull name` `/system [text|-]`
`/rename title` `/delete` `/export [path]` `/copy [n|all]` `/image path` `/think auto|on|off`
`/set temperature 0.7` (temperature, top_p, top_k, num_ctx, num_predict, repeat_penalty, seed, keep_alive)
`/settings` `/stats` `/help`

Chats are stored in `~/.local/share/ollama-tui/chats.json`.
"""

WELCOME = (
    "Start typing below and press Enter.\n\n"
    "  Ctrl+O  pick a model      F3  models / pull / create\n"
    "  /help   all keys & commands\n"
)

# ───────────────────────────── Ollama client ──────────────────────────────


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, host: str) -> None:
        self.host = host
        self.http = httpx.AsyncClient(base_url=host, timeout=httpx.Timeout(None, connect=5.0))

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

# ───────────────────────────── persistence ────────────────────────────────


class Store:
    def __init__(self) -> None:
        self.path = DATA_DIR / "chats.json"
        self.chats: dict[str, dict] = {}
        self.meta: dict[str, Any] = {
            "last_model": "", "active": "",
            "defaults": {"system": "", "options": {}, "think": "auto", "keep_alive": ""},
        }
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.chats = {c["id"]: c for c in data.get("chats", []) if "id" in c}
        self.meta.update(data.get("meta", {}))

    def save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"meta": self.meta, "chats": list(self.chats.values())},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def new_chat(self, model: str = "") -> dict:
        d = self.meta.get("defaults", {})
        now = time.time()
        c = {
            "id": uuid.uuid4().hex[:10], "title": "New chat", "model": model,
            "system": d.get("system", ""), "options": dict(d.get("options") or {}),
            "think": d.get("think", "auto"), "keep_alive": d.get("keep_alive", ""),
            "messages": [], "created": now, "updated": now,
        }
        self.chats[c["id"]] = c
        return c

# ───────────────────────────── rendering ──────────────────────────────────


def render_content(text: str, md: bool = True, cursor: bool = False) -> RenderableType:
    """Markdown + numbered, highlighted code panels."""
    if cursor:
        text += " ▍"
    parts: list[RenderableType] = []

    def prose(chunk: str) -> None:
        if chunk.strip():
            parts.append(RichMarkdown(chunk.strip("\n"), code_theme=CODE_THEME) if md
                         else Text(chunk.strip("\n")))

    pos, idx = 0, 0
    for m in FENCE.finditer(text):
        prose(text[pos:m.start()])
        idx += 1
        lang = m.group(1) or "text"
        code = m.group(2).rstrip("\n")
        parts.append(Panel(
            Syntax(code, lang, theme=CODE_THEME, word_wrap=True, background_color=CODE_BG,
                   line_numbers=code.count("\n") >= 6),
            box=box.ROUNDED, border_style=ACCENT, padding=(0, 1),
            title=Text.assemble((" ", ""), (lang, "bold"), (" ", "")), title_align="left",
            subtitle=Text(f" #{idx} · Ctrl+Y / /copy {idx} ", style=DIM), subtitle_align="right",
        ))
        pos = m.end()
    prose(text[pos:])
    return Group(*parts) if parts else Text("")


def stats_line(s: Optional[dict], stopped: bool = False) -> str:
    if not s:
        return "⏹ stopped" if stopped else ""
    bits = []
    p, g = s.get("prompt_tokens"), s.get("gen_tokens")
    tok = []
    if p is not None:
        tok.append(f"↑{p}")
    if g is not None:
        tok.append(f"↓{g}")
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
    """One chat message."""

    def __init__(self, who: str, text: str = "", *, thinking: str = "", stats: Optional[dict] = None,
                 images: Optional[list] = None, model: str = "", stopped: bool = False) -> None:
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
        self.border_title = " You " if self.who == "user" else f" {self.model_name or 'assistant'} "
        self.refresh_body()

    def refresh_body(self) -> None:
        parts: list[RenderableType] = []
        if self.imgs:
            parts.append(Text("📎 " + ", ".join(Path(p).name for p in self.imgs), style=DIM))
        if self.think_text and getattr(self.app, "show_thinking", True):
            parts.append(Panel(Text(self.think_text.strip(), style=f"italic {DIM}"), title="thinking",
                               title_align="left", border_style="#3a3a3a", box=box.ROUNDED, padding=(0, 1)))
        if self.body_text or self.streaming:
            parts.append(render_content(self.body_text, md=self.who == "assistant", cursor=self.streaming))
        if self.err:
            parts.append(Text(f"⚠ {self.err}", style="bold red"))
        self.update(Group(*parts) if parts else Text(""))
        self.border_subtitle = stats_line(self.stat, self.stopped) if self.who == "assistant" else ""


class ChatInput(TextArea):
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

# ───────────────────────────── dialogs ────────────────────────────────────


class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextPrompt(Dialog):
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
            if q.lower() in m["name"].lower():
                d = m.get("details") or {}
                label = Text()
                label.append(m["name"], style="bold" if m["name"] == self.current else "")
                label.append(f"  {d.get('parameter_size', '')} {d.get('quantization_level', '')} · "
                             f"{fmt_bytes(m.get('size', 0))}", style=DIM)
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
    def __init__(self, chat: dict) -> None:
        super().__init__()
        self.chat = chat

    def compose(self) -> ComposeResult:
        c = self.chat
        opts = c.get("options") or {}
        with VerticalScroll(classes="dialog wide"):
            yield Label("Chat settings", classes="title")
            yield Label("System prompt")
            yield TextArea(c.get("system", ""), id="s-system")
            for key, _ in OPTION_FIELDS:
                with Horizontal(classes="row"):
                    yield Label(key, classes="lbl")
                    yield Input(value=str(opts.get(key, "")), placeholder="model default", id=f"s-{key}")
            with Horizontal(classes="row"):
                yield Label("keep_alive", classes="lbl")
                yield Input(value=str(c.get("keep_alive", "")), placeholder="e.g. 5m, 1h, -1", id="s-keep_alive")
            with Horizontal(classes="row"):
                yield Label("thinking", classes="lbl")
                yield Select([("auto (model default)", "auto"), ("on", "on"), ("off", "off")],
                             value=c.get("think", "auto"), allow_blank=False, id="s-think")
            yield Checkbox("Use these as defaults for new chats", id="s-default")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="s-save", variant="primary")
                yield Button("Cancel", id="s-cancel")

    @on(Button.Pressed, "#s-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#s-save")
    def _save(self) -> None:
        res: dict[str, Any] = {
            "system": self.query_one("#s-system", TextArea).text.strip(),
            "options": {},
            "think": self.query_one("#s-think", Select).value,
            "keep_alive": self.query_one("#s-keep_alive", Input).value.strip(),
            "as_default": self.query_one("#s-default", Checkbox).value,
        }
        for key, typ in OPTION_FIELDS:
            raw = self.query_one(f"#s-{key}", Input).value.strip()
            if raw:
                try:
                    res["options"][key] = typ(raw)
                except ValueError:
                    self.notify(f"{key}: '{raw}' is not a valid {typ.__name__}", severity="error")
                    return
        self.dismiss(res)


class PickTree(DirectoryTree):
    def filter_paths(self, paths):  # directories + .gguf files, no dotfiles
        return [p for p in paths if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() == ".gguf")]


class FolderPicker(Dialog):
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
        tree = self.query_one("#fp-tree", PickTree)
        tree.path = parent
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
            async for obj in self.api.stream("/api/pull", {"model": self.model_name, "name": self.model_name, "stream": True}):
                st = obj.get("status", "")
                dig = obj.get("digest")
                if dig and obj.get("total"):
                    self.layers[dig] = [obj["total"], obj.get("completed", 0)]
                    if dig not in seen:
                        seen.add(dig)
                        log.write(Text(f"layer {dig.split(':')[-1][:12]} · {fmt_bytes(obj['total'])}", style=DIM))
                elif st and st != last_status:
                    log.write(Text(st))
                last_status = st
                total = sum(v[0] for v in self.layers.values())
                done = sum(v[1] for v in self.layers.values())
                now = time.monotonic()
                if now - t_last >= 0.5:
                    inst = (done - c_last) / (now - t_last)
                    speed = inst if speed == 0 else speed * 0.7 + inst * 0.3
                    t_last, c_last = now, done
                if total:
                    bar.update(total=total, progress=done)
                    eta = fmt_dur((total - done) / speed) if speed > 1 else "…"
                    speed_w.update(f"{fmt_bytes(done)} / {fmt_bytes(total)} · {fmt_bytes(speed)}/s · ETA {eta}")
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


QUANTS = [("none (keep source precision)", ""), ("q4_K_M (balanced)", "q4_K_M"),
          ("q4_K_S (smaller)", "q4_K_S"), ("q8_0 (near-lossless)", "q8_0")]
OUTTYPES = [("f16", "f16"), ("bf16", "bf16"), ("f32", "f32"), ("q8_0", "q8_0"), ("auto", "auto")]


class CreateScreen(Dialog):
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

    # -- form events
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

    @on(Button.Pressed, "#c-go")
    def _go(self) -> None:
        q = lambda i, t=Input: self.query_one(i, t)  # noqa: E731
        p = {
            "name": q("#c-name").value.strip(), "src": q("#c-src").value.strip(),
            "mode": q("#c-mode", RadioSet).pressed_index,
            "quant": q("#c-quant", Select).value, "outtype": q("#c-outtype", Select).value,
            "llama": q("#c-llama").value.strip(), "python": q("#c-python").value.strip(),
            "system": q("#c-system").value.strip(), "extra": q("#c-extra", TextArea).text,
        }
        if not p["name"] or re.search(r"\s", p["name"]):
            self.notify("Enter a model name (no spaces).", severity="error")
            return
        if inspect_source(p["src"])[0] in ("none", "bad"):
            self.notify("Source path not found.", severity="error")
            return
        self.query_one("#c-log", RichLog).clear()
        self.run_create(p)

    # -- helpers
    def _log(self, text: str, style: str = "") -> None:
        self.query_one("#c-log", RichLog).write(Text(text, style=style))

    def _busy(self, on_: bool) -> None:
        self.query_one("#c-go", Button).disabled = on_
        self.query_one("#c-stop", Button).disabled = not on_
        if not on_:
            self.query_one("#c-status", Static).update("")

    async def _run_proc(self, cmd: list[str]) -> int:
        self._log("$ " + " ".join(cmd), DIM)
        status = self.query_one("#c-status", Static)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf, last = "", ""
        assert self.proc.stdout is not None
        while True:
            chunk = await self.proc.stdout.read(2048)
            if not chunk:
                break
            buf += ANSI.sub("", dec.decode(chunk)).replace("\r\n", "\n")
            *done, buf = buf.split("\n")
            for line in done:
                seg = last_segment(line)
                if seg and seg != last:
                    self._log(seg)
                    last = seg
            status.update(Text(last_segment(buf), style=f"bold {ACCENT}"))
        return await self.proc.wait()

    async def _convert(self, src: Path, p: dict) -> Path:
        if not src.is_dir():
            raise RuntimeError("Conversion needs a model folder, not a file.")
        llama = Path(p["llama"]).expanduser()
        script = next((c for c in (llama / "convert_hf_to_gguf.py", llama / "convert-hf-to-gguf.py") if c.exists()), None)
        if script is None:
            raise RuntimeError("convert_hf_to_gguf.py not found — clone llama.cpp and set its directory above.")
        outdir = src.parent if os.access(src.parent, os.W_OK) else src
        out = outdir / f"{src.name}-{p['outtype']}.gguf"
        self._log(f"Converting {src.name} → {out.name}", f"bold {ACCENT}")
        rc = await self._run_proc([p["python"] or sys.executable, str(script), str(src),
                                   "--outfile", str(out), "--outtype", p["outtype"]])
        if rc != 0 or not out.exists():
            raise RuntimeError(f"GGUF conversion failed (exit {rc}).")
        self._log(f"✓ wrote {out} ({fmt_bytes(out.stat().st_size)})", "green")
        return out

    @work(exclusive=True, group="create")
    async def run_create(self, p: dict) -> None:
        self._busy(True)
        tmp: Optional[str] = None
        try:
            ollama = shutil.which("ollama")
            if not ollama:
                raise RuntimeError("`ollama` CLI not found in PATH (needed to import local files).")
            src = Path(p["src"]).expanduser().resolve()
            if p["mode"] == 1:
                source = await self._convert(src, p)
            elif p["mode"] == 2:
                if src.is_dir():
                    ggufs = sorted(src.glob("*.gguf"))
                    if len(ggufs) != 1:
                        raise RuntimeError("Folder must contain exactly one .gguf — pick the file itself.")
                    src = ggufs[0]
                source = src
            else:
                if not src.is_dir():
                    raise RuntimeError("Direct import needs a folder.")
                source = src
            frm = f'"{source}"' if " " in str(source) else str(source)
            lines = [f"FROM {frm}"]
            if p["system"]:
                lines.append('SYSTEM """' + p["system"].replace('"""', "'''") + '"""')
            if p["extra"].strip():
                lines.append(p["extra"].strip())
            with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False, encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                tmp = fh.name
            cmd = [ollama, "create", p["name"], "-f", tmp]
            if p["quant"]:
                cmd += ["-q", p["quant"]]
            self._log("Creating model in Ollama…", f"bold {ACCENT}")
            rc = await self._run_proc(cmd)
            if rc != 0:
                raise RuntimeError(f"`ollama create` exited with code {rc}.")
            self.created = True
            self._log(f"✓ Created '{p['name']}'. Use it with Ctrl+O in a chat.", "bold green")
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

# ───────────────────────────── the app ────────────────────────────────────

MODEL_ACTIONS = {"pull", "rename_model", "delete_model", "copy_model", "unload_model", "create_model", "reload"}
CHAT_ACTIONS = {"new_chat", "pick_model", "regenerate", "copy_code", "export_chat", "toggle_thinking",
                "rename_chat", "settings", "scroll_chat"}


class OllamaTUI(App):
    TITLE = "Ollama TUI"
    show_thinking = True

    CSS = """
    Screen { background: $background; }
    Dialog { align: center middle; background: #000000 65%; }
    .dialog { width: 80; max-width: 95%; height: auto; max-height: 92%;
              background: #141414; border: thick $primary; padding: 1 2; }
    .dialog.wide { width: 100; }
    .dialog.tall { height: 90%; }
    .title { text-style: bold; color: $primary; margin-bottom: 1; }
    .hint { color: #7d7d7d; margin-bottom: 1; }
    .row { height: auto; }
    .row Input, .row Select { width: 1fr; }
    .lbl { width: 16; height: 3; content-align: left middle; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    #fp-tree { height: 1fr; margin: 1 0; background: #101010; }
    #mp-list { height: 14; margin-top: 1; border: none; background: #101010; }
    #c-log, #pull-log { height: 10; background: #0b0b0b; margin-top: 1; }
    #c-extra, #s-system { height: 5; }
    #pull-status, #pull-speed, #c-status { height: auto; }

    #sidebar { width: 30; background: $surface; border-right: solid #222222; padding: 0 1; }
    #sidebar-title { text-style: bold; color: $primary; padding: 1 0 0 0; }
    #chat-list { height: 1fr; background: $surface; border: none; margin: 1 0; padding: 0; }
    #sidebar-btns { height: auto; }
    #sidebar-btns Button { width: 1fr; min-width: 6; }
    #chat-main { width: 1fr; }
    #messages { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    .bubble { height: auto; padding: 0 1; margin: 1 0 0 0; border: round #333333; }
    .bubble.user { border: round $primary; margin-left: 10; }
    .bubble.assistant { margin-right: 6; }
    #welcome { margin: 2 4; color: #8a8a8a; }
    #statusline { height: 1; background: $surface; padding: 0 1; }
    ChatInput { height: auto; min-height: 3; max-height: 12; border: round $primary; background: $surface; }

    #model-actions { height: auto; padding: 1 1 0 1; }
    #model-actions Button { margin-right: 1; }
    #models-body { height: 1fr; padding: 1 1 0 1; }
    #models-table { width: 3fr; }
    #model-info { width: 2fr; border-left: solid #222222; padding: 0 2; }
    #running-title { padding: 0 1; text-style: bold; color: $primary; }
    #running-table { height: 7; margin: 0 1 1 1; }
    """

    BINDINGS = [
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
        Binding("f1", "help", "Help"),
        Binding("f2", "show_tab('tab-chat')", "Chat"),
        Binding("f3", "show_tab('tab-models')", "Models"),
        Binding("escape", "stop", "Stop", show=False),
        # models tab
        Binding("p", "pull", "Pull"),
        Binding("n", "create_model", "Create"),
        Binding("r", "rename_model", "Rename"),
        Binding("c", "copy_model", "Copy"),
        Binding("d", "delete_model", "Delete"),
        Binding("u", "unload_model", "Unload"),
        Binding("f5", "reload", "Refresh"),
    ]

    # ---- setup
    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host
        self.api = Ollama(host)
        self.store = Store()
        self.models: list[dict] = []
        self.pending_images: list[str] = []
        self.generating = False
        self.live: Optional[dict] = None
        self.chat: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs", initial="tab-chat"):
            with TabPane("Chat", id="tab-chat"):
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
            with TabPane("Models", id="tab-models"):
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
        yield Footer()

    async def on_mount(self) -> None:
        try:
            from textual.theme import Theme
            self.register_theme(Theme(name="ollama-dark", primary=ACCENT, secondary="#2a6fb8", accent=ACCENT,
                                      background=BG, surface=SURFACE, panel=PANEL, foreground="#dcdcdc", dark=True))
            self.theme = "ollama-dark"
        except Exception:
            pass
        mt = self.query_one("#models-table", DataTable)
        mt.cursor_type = "row"
        mt.zebra_stripes = True
        mt.add_columns("Name", "Params", "Quant", "Family", "Size", "Modified")
        rt = self.query_one("#running-table", DataTable)
        rt.cursor_type = "row"
        rt.add_columns("Name", "Size", "Placement", "Context", "Unloads in")
        inp = self.query_one("#input", ChatInput)
        inp.border_title = " Message — Enter send · Ctrl+J newline · /help "
        # chats
        active = self.store.chats.get(self.store.meta.get("active", ""))
        if not active and self.store.chats:
            active = max(self.store.chats.values(), key=lambda c: c.get("updated", 0))
        self.chat = active or self.store.new_chat(self.store.meta.get("last_model", ""))
        self.store.meta["active"] = self.chat["id"]
        await self.show_chat()
        self.set_interval(4.0, self.poll_running)
        self.reload()
        inp.focus()

    # ---- binding availability per tab / modal
    def check_action(self, action: str, parameters: tuple[object, ...]) -> Optional[bool]:
        try:
            if isinstance(self.screen, ModalScreen):
                return action in {"quit", "command_palette"}
            tab = self.query_one(TabbedContent).active
        except Exception:
            return True
        if action in MODEL_ACTIONS:
            return tab == "tab-models"
        if action in CHAT_ACTIONS:
            return tab == "tab-chat"
        return True

    @on(TabbedContent.TabActivated)
    def _tab(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()
        if event.pane.id == "tab-models":
            self.query_one("#models-table", DataTable).focus()
            self.refresh_running()
        else:
            self.focus_input()

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def focus_input(self) -> None:
        try:
            self.query_one("#input", ChatInput).focus()
        except Exception:
            pass

    # ---- data refresh
    @work(exclusive=True, group="refresh")
    async def reload(self) -> None:
        try:
            ver = await self.api.version()
            tags = await self.api.tags()
        except Exception as e:
            self.sub_title = f"{self.host} · offline"
            self.notify(f"Cannot reach Ollama at {self.host}: {e}\nStart it with `ollama serve`.",
                        severity="error", timeout=8)
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
            t.add_row(m["name"], d.get("parameter_size") or "?", d.get("quantization_level") or "?",
                      d.get("family") or "?", fmt_bytes(m.get("size", 0)), ago(m.get("modified_at")), key=m["name"])
        if prev:
            try:
                t.move_cursor(row=t.get_row_index(prev))
            except Exception:
                pass

    async def refresh_running(self) -> None:
        try:
            running = await self.api.ps()
        except Exception:
            return
        t = self.query_one("#running-table", DataTable)
        t.clear()
        for m in running:
            size, vram = m.get("size", 0), m.get("size_vram", 0)
            where = "CPU" if not vram else ("GPU" if vram >= size else f"{vram / size:.0%} GPU")
            ts = parse_ts(m.get("expires_at"))
            until = "—" if ts is None else ("never" if ts < 0 else ("now" if ts < time.time() else fmt_dur(ts - time.time())))
            t.add_row(m.get("name", "?"), fmt_bytes(size), where, str(m.get("context_length", "—")), until,
                      key=m.get("name", str(uuid.uuid4())))

    async def poll_running(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        try:
            if self.query_one(TabbedContent).active == "tab-models":
                await self.refresh_running()
        except Exception:
            pass

    def ensure_chat_model(self) -> None:
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
                self.notify("No models installed yet — press F3, then `p` to pull one.", timeout=8)
        self.update_status()

    # ---- models tab
    def selected_model(self) -> Optional[str]:
        t = self.query_one("#models-table", DataTable)
        if t.row_count == 0:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None

    @on(DataTable.RowHighlighted, "#models-table")
    def _model_hl(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.load_model_info(str(event.row_key.value))

    @on(DataTable.RowSelected, "#models-table")
    def _model_sel(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value:
            self.set_chat_model(str(event.row_key.value))
            self.notify(f"Chat model → {event.row_key.value}")
            self.action_show_tab("tab-chat")

    @work(exclusive=True, group="info")
    async def load_model_info(self, name: str) -> None:
        body = self.query_one("#model-info-body", Static)
        try:
            info = await self.api.show(name)
        except Exception as e:
            body.update(Text(f"Could not load details: {e}", style="red"))
            return
        d = info.get("details") or {}
        mi = info.get("model_info") or {}
        ctx = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
        emb = next((v for k, v in mi.items() if k.endswith(".embedding_length")), None)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=DIM)
        grid.add_column()
        for k, v in [("Family", d.get("family")), ("Parameters", d.get("parameter_size")),
                     ("Quantization", d.get("quantization_level")), ("Format", d.get("format")),
                     ("Architecture", mi.get("general.architecture")),
                     ("Max context", f"{ctx:,}" if isinstance(ctx, int) else None),
                     ("Embedding dim", f"{emb:,}" if isinstance(emb, int) else None),
                     ("Capabilities", ", ".join(info.get("capabilities") or []) or None)]:
            if v:
                grid.add_row(k, str(v))
        parts: list[RenderableType] = [Text(name, style=f"bold {ACCENT}"), Text(""), grid]
        for title, key, n in [("Parameters", "parameters", 12), ("System", "system", 8),
                              ("Template", "template", 12), ("License", "license", 4)]:
            if info.get(key):
                parts += [Text(f"\n{title}", style="bold"), Text(trim_lines(info[key], n), style="#9a9a9a")]
        body.update(Group(*parts))

    @on(Button.Pressed, ".mbtn")
    def _model_button(self, event: Button.Pressed) -> None:
        if event.button.name:
            getattr(self, f"action_{event.button.name}")()

    def action_reload(self) -> None:
        self.reload()

    def action_pull(self, name: str = "") -> None:
        def go(n: Optional[str]) -> None:
            if n:
                self.push_screen(PullScreen(self.api, n), lambda _: self.reload())
        if name:
            go(name)
        else:
            self.push_screen(TextPrompt("Pull a model", hint="e.g. llama3.2:3b · qwen2.5-coder:7b · hf.co/user/repo:Q4_K_M"), go)

    def action_create_model(self) -> None:
        self.push_screen(CreateScreen(), lambda _: self.reload())

    def action_rename_model(self) -> None:
        old = self.selected_model()
        if old:
            self.push_screen(TextPrompt(f"Rename '{old}' to", value=old,
                                        hint="Ollama has no rename API: this copies the model, then deletes the old name."),
                             lambda new: self._do_rename(old, new))

    @work(group="modelop")
    async def _do_rename(self, old: str, new: Optional[str]) -> None:
        if not new or new == old:
            return
        names = {m["name"] for m in self.models}
        if new in names or f"{new}:latest" in names:
            if not await self.push_screen_wait(Confirm(f"'{new}' already exists. Overwrite it?", "Overwrite")):
                return
        try:
            await self.api.copy(old, new)
            await self.api.delete(old)
        except Exception as e:
            self.notify(f"Rename failed: {e}", severity="error")
            return
        for c in self.store.chats.values():
            if c.get("model") == old:
                c["model"] = new
        self.store.save()
        self.notify(f"Renamed {old} → {new}")
        self.reload()

    def action_copy_model(self) -> None:
        src = self.selected_model()
        if src:
            self.push_screen(TextPrompt(f"Copy '{src}' as", value=src + "-copy"), lambda dst: self._do_copy(src, dst))

    @work(group="modelop")
    async def _do_copy(self, src: str, dst: Optional[str]) -> None:
        if not dst:
            return
        try:
            await self.api.copy(src, dst)
        except Exception as e:
            self.notify(f"Copy failed: {e}", severity="error")
            return
        self.notify(f"Copied {src} → {dst}")
        self.reload()

    def action_delete_model(self) -> None:
        name = self.selected_model()
        if name:
            self.push_screen(Confirm(f"Delete model '{name}'?\nThis frees disk space and cannot be undone.", "Delete"),
                             lambda ok: self._do_delete(name) if ok else None)

    @work(group="modelop")
    async def _do_delete(self, name: str) -> None:
        try:
            await self.api.delete(name)
        except Exception as e:
            self.notify(f"Delete failed: {e}", severity="error")
            return
        self.notify(f"Deleted {name}")
        self.reload()

    def action_unload_model(self) -> None:
        name = self.selected_model()
        if name:
            self._do_unload(name)

    @work(group="modelop")
    async def _do_unload(self, name: str) -> None:
        try:
            await self.api.unload(name)
        except Exception as e:
            self.notify(f"Unload failed: {e}", severity="error")
            return
        self.notify(f"Unloaded {name}")
        await asyncio.sleep(0.4)
        await self.refresh_running()

    # ---- chat list / switching
    def refresh_chat_list(self) -> None:
        ol = self.query_one("#chat-list", OptionList)
        ol.clear_options()
        chats = sorted(self.store.chats.values(), key=lambda c: c.get("updated", 0), reverse=True)
        opts = []
        for c in chats:
            label = Text()
            label.append(shorten(c.get("title", "Chat"), 24), style="bold" if c["id"] == self.chat.get("id") else "")
            label.append("\n" + (c.get("model") or "no model"), style=DIM)
            opts.append(Option(label, id=c["id"]))
        if opts:
            ol.add_options(opts)
            for i, c in enumerate(chats):
                if c["id"] == self.chat.get("id"):
                    ol.highlighted = i

    def make_bubble(self, m: dict) -> Bubble:
        return Bubble(m["role"], m.get("content", ""), thinking=m.get("thinking", ""), stats=m.get("stats"),
                      images=m.get("images"), model=self.chat.get("model", ""), stopped=m.get("stopped", False))

    async def show_chat(self) -> None:
        box_ = self.query_one("#messages", VerticalScroll)
        await box_.remove_children()
        msgs = [m for m in self.chat.get("messages", []) if m.get("role") in ("user", "assistant")]
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
        self.chat = self.store.new_chat(self.chat.get("model") or self.store.meta.get("last_model", ""))
        self.store.meta["active"] = self.chat["id"]
        self.store.save()
        await self.show_chat()
        self.focus_input()

    def delete_chat(self) -> None:
        if self.generating:
            self.notify("Stop generating first (Esc).", severity="warning")
            return
        title = self.chat.get("title", "this chat")
        self.push_screen(Confirm(f"Delete chat '{title}'?", "Delete"), self._do_delete_chat)

    async def _do_delete_chat(self, ok: Optional[bool]) -> None:
        if not ok:
            return
        self.store.chats.pop(self.chat["id"], None)
        rest = sorted(self.store.chats.values(), key=lambda c: c.get("updated", 0), reverse=True)
        self.chat = rest[0] if rest else self.store.new_chat(self.store.meta.get("last_model", ""))
        self.store.meta["active"] = self.chat["id"]
        self.store.save()
        await self.show_chat()

    def action_rename_chat(self) -> None:
        self.push_screen(TextPrompt("Rename chat", value=self.chat.get("title", "")), self._do_rename_chat)

    def _do_rename_chat(self, title: Optional[str]) -> None:
        if title:
            self.chat["title"] = title
            self.store.save()
            self.refresh_chat_list()

    # ---- model / settings for the active chat
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
        self.push_screen(ModelPicker(self.models, self.chat.get("model", "")),
                         lambda n: (self.set_chat_model(n), self.focus_input()) if n else self.focus_input())

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.chat), self._apply_settings)

    def _apply_settings(self, res: Optional[dict]) -> None:
        if res:
            for k in ("system", "options", "think", "keep_alive"):
                self.chat[k] = res[k]
            if res["as_default"]:
                self.store.meta["defaults"] = {k: res[k] for k in ("system", "options", "think", "keep_alive")}
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

    # ---- status line
    def update_status(self) -> None:
        c = self.chat
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" ● " if not self.generating else " ◉ ", style=f"bold {ACCENT}" if not self.generating else "bold #ffb454")
        t.append(c.get("model") or "no model (Ctrl+O)", style="bold")
        up = down = 0
        last_ctx = None
        for m in c.get("messages", []):
            s = m.get("stats")
            if s:
                up += s.get("prompt_tokens") or 0
                down += s.get("gen_tokens") or 0
                last_ctx = (s.get("prompt_tokens") or 0) + (s.get("gen_tokens") or 0)
        num_ctx = (c.get("options") or {}).get("num_ctx")
        if last_ctx:
            t.append("  │  ctx ", style=DIM)
            t.append(f"{last_ctx:,}" + (f"/{num_ctx:,}" if num_ctx else ""))
            if num_ctx and last_ctx / num_ctx > 0.85:
                t.append(" ⚠", style="bold #ffb454")
        if up or down:
            t.append("  │  session ", style=DIM)
            t.append(f"↑{up:,} ↓{down:,} tok")
        if c.get("think", "auto") != "auto":
            t.append(f"  │  think:{c['think']}", style=DIM)
        if self.pending_images:
            t.append(f"  │  📎{len(self.pending_images)}", style=DIM)
        if self.live:
            l = self.live
            t.append("  │  ", style=DIM)
            t.append(f"{l['tok']} tok · {l['tps']:.1f} tok/s · {fmt_dur(l['elapsed'])}", style="bold #ffb454")
            if l.get("ttft"):
                t.append(f" · ttft {fmt_dur(l['ttft'])}", style=DIM)
        self.query_one("#statusline", Static).update(t)

    # ---- sending & streaming
    @on(ChatInput.Submitted)
    async def _submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        inp = self.query_one("#input", ChatInput)
        if text.startswith("/") and text.split()[0][1:].lower() in COMMANDS:
            inp.load_text("")
            await self.run_command(text)
            return
        if self.generating:
            self.notify("Still generating — press Esc to stop.", severity="warning")
            return
        inp.load_text("")
        await self.send(text)

    async def send(self, text: str) -> None:
        chat = self.chat
        if not chat.get("model"):
            self.notify("Pick a model first (Ctrl+O).", severity="warning")
            return
        msg: dict[str, Any] = {"role": "user", "content": text}
        if self.pending_images:
            msg["images"] = self.pending_images
            self.pending_images = []
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
        box_ = self.query_one("#messages", VerticalScroll)
        bubble = Bubble("assistant", model=self.chat["model"])
        bubble.streaming = True
        await box_.mount(bubble)
        box_.scroll_end(animate=False)
        self.generating = True
        self.live = {"tok": 0, "tps": 0.0, "elapsed": 0.0, "ttft": None}
        self.update_status()
        self.run_generation(bubble)

    def build_payload(self) -> dict:
        c = self.chat
        msgs: list[dict] = []
        if c.get("system"):
            msgs.append({"role": "system", "content": c["system"]})
        for m in c["messages"]:
            if m.get("role") not in ("user", "assistant"):
                continue
            d: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            if m.get("images"):
                import base64
                imgs = []
                for p in m["images"]:
                    try:
                        imgs.append(base64.b64encode(Path(p).read_bytes()).decode())
                    except OSError:
                        self.notify(f"Image not readable: {p}", severity="warning")
                if imgs:
                    d["images"] = imgs
            msgs.append(d)
        payload: dict[str, Any] = {"model": c["model"], "messages": msgs, "stream": True}
        if c.get("options"):
            payload["options"] = dict(c["options"])
        if c.get("think") == "on":
            payload["think"] = True
        elif c.get("think") == "off":
            payload["think"] = False
        if str(c.get("keep_alive", "")).strip():
            payload["keep_alive"] = parse_keep_alive(str(c["keep_alive"]))
        return payload

    @work(exclusive=True, group="gen")
    async def run_generation(self, bubble: Bubble) -> None:
        box_ = self.query_one("#messages", VerticalScroll)
        content = thinking = ""
        final: dict = {}
        chunks = 0
        ttft: Optional[float] = None
        t0 = time.monotonic()
        last_draw = 0.0
        stopped, error = False, ""
        try:
            async for obj in self.api.stream("/api/chat", self.build_payload()):
                msg = obj.get("message") or {}
                piece = False
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    piece = True
                if msg.get("content"):
                    content += msg["content"]
                    piece = True
                if piece:
                    chunks += 1
                    if ttft is None:
                        ttft = time.monotonic() - t0
                if obj.get("done"):
                    final = obj
                now = time.monotonic()
                if now - last_draw > 0.09 or obj.get("done"):
                    last_draw = now
                    stick = box_.scroll_y >= box_.max_scroll_y - 2
                    bubble.body_text, bubble.think_text = content, thinking
                    bubble.refresh_body()
                    el = now - t0
                    gen_time = max(el - (ttft or 0), 1e-6)
                    self.live = {"tok": chunks, "tps": (chunks - 1) / gen_time if chunks > 1 else 0.0,
                                 "elapsed": el, "ttft": ttft}
                    self.update_status()
                    if stick:
                        self.call_after_refresh(box_.scroll_end, animate=False)
        except asyncio.CancelledError:
            stopped = True
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}" if not isinstance(e, OllamaError) else str(e)
        finally:
            self.finish_generation(bubble, content, thinking, final, stopped, error, t0, ttft, chunks)

    def finish_generation(self, bubble: Bubble, content: str, thinking: str, final: dict, stopped: bool,
                          error: str, t0: float, ttft: Optional[float], chunks: int) -> None:
        elapsed = time.monotonic() - t0
        stats: Optional[dict] = None
        if final:
            ec, ed = final.get("eval_count"), final.get("eval_duration") or 0
            pc, pd = final.get("prompt_eval_count"), final.get("prompt_eval_duration") or 0
            stats = {
                "prompt_tokens": pc, "gen_tokens": ec if ec is not None else chunks,
                "tps": (ec / (ed / 1e9)) if ec and ed else (chunks / elapsed if elapsed else 0.0),
                "prompt_tps": (pc / (pd / 1e9)) if pc and pd else None,
                "total_s": (final.get("total_duration") or 0) / 1e9 or elapsed,
                "load_s": (final.get("load_duration") or 0) / 1e9,
                "ttft_s": ttft, "done_reason": final.get("done_reason"),
            }
        elif content or thinking:
            stats = {"prompt_tokens": None, "gen_tokens": chunks, "tps": chunks / elapsed if elapsed else 0.0,
                     "total_s": elapsed, "load_s": 0, "ttft_s": ttft}
        bubble.streaming = False
        bubble.body_text, bubble.think_text, bubble.stat = content, thinking, stats
        bubble.stopped, bubble.err = stopped, error
        if content or thinking:
            m: dict[str, Any] = {"role": "assistant", "content": content}
            if thinking:
                m["thinking"] = thinking
            if stats:
                m["stats"] = stats
            if stopped:
                m["stopped"] = True
            self.chat["messages"].append(m)
            self.chat["updated"] = time.time()
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
            self.notify("This model doesn't support thinking — set it to 'auto' or 'off' in F4.", severity="warning")

    def action_stop(self) -> None:
        if self.generating:
            self.workers.cancel_group(self, "gen")

    async def action_regenerate(self) -> None:
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

    # ---- code copy / export
    def last_assistant(self) -> str:
        for m in reversed(self.chat.get("messages", [])):
            if m["role"] == "assistant":
                return m.get("content", "")
        return ""

    def action_copy_code(self) -> None:
        self.copy_block("")

    def copy_block(self, which: str) -> None:
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
        if not system_copy(payload):
            self.copy_to_clipboard(payload)  # OSC-52 fallback
        self.notify(f"Copied {label}")

    def export_markdown(self) -> str:
        c = self.chat
        out = [f"# {c.get('title', 'Chat')}", "",
               f"*Model: {c.get('model')} · exported {datetime.now():%Y-%m-%d %H:%M}*", ""]
        if c.get("system"):
            out += ["> **System:** " + c["system"].replace("\n", "\n> "), ""]
        for m in c.get("messages", []):
            if m["role"] == "user":
                out += ["## You", "", m.get("content", ""), ""]
            else:
                out += [f"## {c.get('model')}", ""]
                if m.get("thinking"):
                    out += ["<details><summary>thinking</summary>", "", m["thinking"], "", "</details>", ""]
                out += [m.get("content", ""), ""]
                sl = stats_line(m.get("stats"), m.get("stopped", False))
                if sl:
                    out += [f"*{sl}*", ""]
        return "\n".join(out)

    def action_export_chat(self, path: str = "") -> None:
        dest = Path(path).expanduser() if path else Path.home() / f"ollama-chat-{slugify(self.chat.get('title', ''))}-{datetime.now():%Y%m%d-%H%M}.md"
        try:
            dest.write_text(self.export_markdown(), encoding="utf-8")
            self.notify(f"Exported → {dest}")
        except OSError as e:
            self.notify(f"Export failed: {e}", severity="error")

    # ---- slash commands
    async def run_command(self, line: str) -> None:
        cmd, _, arg = line[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        c = self.chat
        if cmd == "help":
            self.action_help()
        elif cmd == "new":
            await self.action_new_chat()
        elif cmd == "clear":
            if not self.generating:
                c["messages"] = []
                self.store.save()
                await self.show_chat()
        elif cmd == "undo":
            if not self.generating and c["messages"]:
                while c["messages"] and c["messages"][-1]["role"] == "assistant":
                    c["messages"].pop()
                if c["messages"]:
                    c["messages"].pop()
                self.store.save()
                await self.show_chat()
        elif cmd == "regen":
            await self.action_regenerate()
        elif cmd == "model":
            if arg:
                self.set_chat_model(arg)
                self.notify(f"Model → {arg}")
            else:
                self.action_pick_model()
        elif cmd == "models":
            self.action_show_tab("tab-models")
        elif cmd == "pull":
            self.action_pull(arg) if arg else self.action_pull()
        elif cmd == "system":
            if not arg:
                self.notify(c.get("system") or "(no system prompt)", title="System prompt", timeout=8)
            else:
                c["system"] = "" if arg == "-" else arg
                self.store.save()
                self.notify("System prompt " + ("cleared" if arg == "-" else "set"))
        elif cmd == "rename":
            if arg:
                self._do_rename_chat(arg)
            else:
                self.action_rename_chat()
        elif cmd == "delete":
            self.delete_chat()
        elif cmd == "export":
            self.action_export_chat(arg)
        elif cmd == "copy":
            self.copy_block(arg)
        elif cmd == "image":
            p = Path(arg.strip("'\"")).expanduser()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                self.pending_images.append(str(p.resolve()))
                self.notify(f"Attached {p.name} — sent with your next message (needs a vision model).")
            else:
                self.notify("Usage: /image /path/to/picture.png", severity="warning")
        elif cmd == "think":
            if arg in ("auto", "on", "off"):
                c["think"] = arg
                self.store.save()
            else:
                self.notify("Usage: /think auto|on|off", severity="warning")
        elif cmd == "set":
            key, _, val = arg.partition(" ")
            val = val.strip()
            if key == "keep_alive" and val:
                c["keep_alive"] = val
            elif key in OPTION_TYPES and val:
                try:
                    c.setdefault("options", {})[key] = OPTION_TYPES[key](val)
                except ValueError:
                    self.notify(f"{key} must be {OPTION_TYPES[key].__name__}", severity="error")
            elif key in OPTION_TYPES and not val:
                c.setdefault("options", {}).pop(key, None)
            else:
                self.notify("Usage: /set <temperature|top_p|top_k|num_ctx|num_predict|repeat_penalty|seed|keep_alive> <value>",
                            severity="warning")
            self.store.save()
        elif cmd == "settings":
            self.action_settings()
        elif cmd == "stats":
            up = sum((m.get("stats") or {}).get("prompt_tokens") or 0 for m in c["messages"])
            down = sum((m.get("stats") or {}).get("gen_tokens") or 0 for m in c["messages"])
            tps = [(m.get("stats") or {}).get("tps") for m in c["messages"] if (m.get("stats") or {}).get("tps")]
            avg = f"{sum(tps) / len(tps):.1f}" if tps else "–"
            self.notify(f"{len(c['messages'])} messages · ↑{up:,} ↓{down:,} tokens · avg {avg} tok/s\n"
                        f"options: {c.get('options') or 'defaults'}", title="Chat stats", timeout=8)
        self.update_status()


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal UI for Ollama")
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST"), help="Ollama URL (default: $OLLAMA_HOST or 127.0.0.1:11434)")
    args = ap.parse_args()
    OllamaTUI(normalize_host(args.host)).run()


if __name__ == "__main__":
    main()
