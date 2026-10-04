#!/usr/bin/env python3
"""om - a Rich-powered terminal chat for Ollama, with built-in server management.

    om --serve      start `ollama serve` in the background (its PID is remembered)
    om --end        stop the server that `om --serve` started
    om --status     show server state and loaded models
    om              chat (offers to start the server if it isn't running)
    om -m llama3.2 "hello"      pick a model / send a first message

Deps: rich, prompt_toolkit, requests, psutil   (Python 3.9+)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import psutil
import requests
from prompt_toolkit import Application, PromptSession
from prompt_toolkit.completion import NestedCompleter, PathCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm
from rich.segment import Segment
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #


def _state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "om"
    d.mkdir(parents=True, exist_ok=True)
    return d


STATE_DIR = _state_dir()
PID_FILE = STATE_DIR / "ollama.pid.json"
LOG_FILE = STATE_DIR / "ollama.log"
HISTORY_FILE = STATE_DIR / "history"
CONFIG_FILE = STATE_DIR / "config.json"
SAVE_DIR = STATE_DIR / "chats"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def resolve_host(cli_host: str | None) -> str:
    """Turn --host / $OLLAMA_HOST into a base URL like http://127.0.0.1:11434."""
    raw = cli_host or os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    host = u.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:  # IPv6
        host = f"[{host}]"
    port = u.port or (443 if u.scheme == "https" else 11434)
    return f"{u.scheme}://{host}:{port}"


# --------------------------------------------------------------------------- #
# Background server management  (om --serve / om --end / om --status)
# --------------------------------------------------------------------------- #


def server_up(base: str, timeout: float = 0.8) -> bool:
    try:
        return requests.get(base, timeout=timeout).ok
    except requests.RequestException:
        return False


def tracked_process() -> psutil.Process | None:
    """The `ollama serve` process that om started, if it's still the same process."""
    try:
        info = json.loads(PID_FILE.read_text())
        proc = psutil.Process(info["pid"])
        if abs(proc.create_time() - info["created"]) > 2:  # PID was recycled
            return None
        return proc
    except (OSError, ValueError, KeyError, psutil.Error):
        return None


def start_server(console: Console, base: str) -> bool:
    if server_up(base):
        proc = tracked_process()
        if proc:
            console.print(f"[green]●[/] Ollama is already running (pid {proc.pid}, started by om).")
        else:
            console.print(
                f"[yellow]●[/] Something is already answering at {base}, but om didn't start it, "
                "so [bold]om --end[/] won't touch it."
            )
        return True

    exe = shutil.which("ollama")
    if not exe:
        console.print("[red]✗[/] Couldn't find the [bold]ollama[/] binary on your PATH.")
        return False

    env = os.environ.copy()
    env["OLLAMA_HOST"] = urlparse(base).netloc
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True  # survives closing this terminal

    with open(LOG_FILE, "ab") as log:
        proc = subprocess.Popen(
            [exe, "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **kwargs
        )
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        console.print(f"[red]✗[/] ollama serve exited immediately. See {LOG_FILE}")
        return False
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "created": created, "host": base}))

    ready = False
    with console.status("Starting Ollama…"):
        deadline = time.time() + 20
        while time.time() < deadline:
            if server_up(base):
                ready = True
                break
            if proc.poll() is not None:
                PID_FILE.unlink(missing_ok=True)
                console.print(f"[red]✗[/] ollama serve exited (code {proc.returncode}). See {LOG_FILE}")
                return False
            time.sleep(0.3)
    if not ready:
        console.print(f"[yellow]●[/] Started (pid {proc.pid}) but it isn't answering yet. Log: {LOG_FILE}")
        return True
    console.print(f"[green]●[/] Ollama is up at {base} (pid {proc.pid}). Stop it with [bold]om --end[/].")
    return True


def stop_server(console: Console, base: str) -> None:
    proc = tracked_process()
    if proc is None:
        PID_FILE.unlink(missing_ok=True)
        if server_up(base):
            console.print(f"[yellow]●[/] A server is running at {base}, but om didn't start it, so I'm leaving it alone.")
        else:
            console.print("[dim]Nothing to stop: no om-managed Ollama server.[/]")
        return

    pid = proc.pid
    try:
        procs = [proc] + proc.children(recursive=True)  # runners are children of serve
    except psutil.Error:
        procs = [proc]
    with console.status("Stopping Ollama…"):
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=8)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=3)
    PID_FILE.unlink(missing_ok=True)
    console.print(f"[green]●[/] Stopped Ollama (pid {pid}).")


def show_status(console: Console, base: str) -> None:
    proc = tracked_process()
    up = server_up(base)
    t = Table.grid(padding=(0, 2))
    t.add_row("[dim]server[/]", f"[green]running[/] at {base}" if up else f"[red]stopped[/] ({base})")
    if proc:
        up_for = int(time.time() - proc.create_time())
        t.add_row("[dim]managed[/]", f"by om, pid {proc.pid}, up {up_for // 3600}h {up_for % 3600 // 60}m")
    elif up:
        t.add_row("[dim]managed[/]", "not by om (om --end won't stop it)")
    t.add_row("[dim]log[/]", str(LOG_FILE))
    console.print(t)
    if up:
        try:
            loaded = Client(base).running()
        except (OllamaError, requests.RequestException):
            loaded = []
        if loaded:
            console.print("[dim]loaded:[/] " + ", ".join(m["name"] for m in loaded))


# --------------------------------------------------------------------------- #
# Ollama HTTP client
# --------------------------------------------------------------------------- #


class OllamaError(Exception):
    pass


def _err(r: requests.Response) -> str:
    try:
        return str(r.json().get("error") or r.text)
    except ValueError:
        return r.text or f"HTTP {r.status_code}"


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = requests.Session()

    def _json(self, method: str, path: str, **kw):
        r = self.http.request(method, self.base + path, timeout=15, **kw)
        if not r.ok:
            raise OllamaError(_err(r))
        return r.json()

    def _stream(self, path: str, payload: dict):
        with self.http.post(self.base + path, json=payload, stream=True, timeout=(5, None)) as r:
            if not r.ok:
                raise OllamaError(_err(r))
            for raw in r.iter_lines():
                if not raw:
                    continue
                ev = json.loads(raw)
                if ev.get("error"):
                    raise OllamaError(str(ev["error"]))
                yield ev

    def models(self) -> list[dict]:
        return sorted(self._json("GET", "/api/tags").get("models", []), key=lambda m: m["name"])

    def show(self, name: str) -> dict:
        return self._json("POST", "/api/show", json={"model": name, "name": name})

    def running(self) -> list[dict]:
        return self._json("GET", "/api/ps").get("models", [])

    def unload(self, name: str) -> None:
        self._json("POST", "/api/generate", json={"model": name, "keep_alive": 0})

    def chat_stream(self, payload: dict):
        return self._stream("/api/chat", payload)

    def pull_stream(self, name: str):
        return self._stream("/api/pull", {"model": name, "name": name, "stream": True})


# --------------------------------------------------------------------------- #
# Small helpers: sizes, interactive picker, <think> splitting
# --------------------------------------------------------------------------- #


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pick(title: str, options: list[tuple[str, str]], default: str | None = None, page: int = 12) -> str | None:
    """Inline arrow-key picker. Returns the chosen value, or None if cancelled."""
    if not options:
        return None
    idx = [next((i for i, (v, _) in enumerate(options) if v == default), 0)]
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        idx[0] = (idx[0] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        idx[0] = (idx[0] + 1) % len(options)

    @kb.add("enter")
    def _ok(event):
        event.app.exit(result=options[idx[0]][0])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("q")
    def _cancel(event):
        event.app.exit(result=None)

    def render():
        start = max(0, min(idx[0] - page // 2, len(options) - page))
        out = [("bold", f"{title}\n"), ("fg:ansibrightblack", "  ↑/↓ move · enter select · esc cancel\n")]
        for i in range(start, min(start + page, len(options))):
            sel = i == idx[0]
            out.append(("reverse bold" if sel else "", f" {'❯' if sel else ' '} {options[i][1]} \n"))
        return out

    app = Application(
        layout=Layout(Window(FormattedTextControl(render, show_cursor=False), dont_extend_height=True)),
        key_bindings=kb,
        erase_when_done=True,
    )
    return app.run()


class ThinkSplitter:
    """Routes <think>…</think> found inside streamed content into a separate 'thinking' channel.

    Only needed for older Ollama servers / models that inline their reasoning; newer servers
    return it in a dedicated `thinking` field.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> tuple[str, str]:
        self.buf += text
        think: list[str] = []
        answer: list[str] = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i != -1:
                (think if self.inside else answer).append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0  # hold back a possible partial tag at the end of the buffer
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            cut = len(self.buf) - keep
            (think if self.inside else answer).append(self.buf[:cut])
            self.buf = self.buf[cut:]
            break
        return "".join(think), "".join(answer)

    def flush(self) -> tuple[str, str]:
        rest, self.buf = self.buf, ""
        return (rest, "") if self.inside else ("", rest)


# --------------------------------------------------------------------------- #
# Live rendering
# --------------------------------------------------------------------------- #


def tail_lines(console: Console, renderable, options, limit: int) -> list[list[Segment]]:
    lines = console.render_lines(renderable, options.update(height=None), pad=False)
    return lines[-limit:] if limit > 0 else []


@dataclass
class State:
    base: str
    model: str | None = None
    caps: list[str] = field(default_factory=list)
    think: str = "on"  # on | off | low | medium | high
    show_thoughts: bool = True
    lang: str | None = None
    system: str | None = None
    temperature: float | None = None
    num_ctx: int | None = None
    theme: str = "monokai"
    messages: list[dict] = field(default_factory=list)

    def system_prompt(self) -> str | None:
        parts = []
        if self.system:
            parts.append(self.system)
        if self.lang:
            parts.append(
                f"Always reply in {self.lang}, even if the user writes in another language, "
                "unless they explicitly ask otherwise. Keep code, identifiers and shell commands unchanged."
            )
        return "\n\n".join(parts) or None

    def think_param(self):
        """Value for the API's `think` field, or None to leave it out."""
        if not self.model or "thinking" not in self.caps:
            return None
        if self.think == "off":
            return False
        if self.think in ("low", "medium", "high"):
            return self.think if self.model.startswith("gpt-oss") else True
        return True


class StreamView:
    """Live renderable: dim thinking on top, Markdown answer below.

    Only the part that fits on screen is drawn while streaming (so Live never scrolls);
    the full text is printed once when the reply is finished.
    """

    def __init__(self, st: State):
        self.st = st
        self.thinking = ""
        self.answer = ""

    def __rich_console__(self, console, options):
        budget = max(console.height - 2, 4)
        out: list[list[Segment]] = []
        think, answer = self.thinking.strip(), self.answer.strip()
        if think and self.st.show_thoughts:
            body = Text.assemble(("✻ thinking\n", "dim bold"), (think, "dim italic"))
            cap = budget if not answer else max(3, min(7, budget // 3))
            lines = tail_lines(console, body, options, cap)
            out += lines
            budget -= len(lines)
        if answer:
            out += tail_lines(console, Markdown(answer, code_theme=self.st.theme), options, max(budget, 3))
        if not out:
            label = " thinking…" if think else " waiting for the model…"
            out = tail_lines(console, Spinner("dots", text=Text(label, style="dim")), options, 1)
        for line in out:
            yield from line
            yield Segment.line()


# --------------------------------------------------------------------------- #
# The chat application
# --------------------------------------------------------------------------- #

COMMANDS = [
    ("/help", "show this help"),
    ("/model [name]", "pick a model interactively, or switch by name"),
    ("/models", "list installed models"),
    ("/think [on|off|low|medium|high]", "control model thinking (thinking-capable models)"),
    ("/thoughts", "show / hide the thinking text"),
    ("/lang [language|off]", "make the model answer in a language, e.g. /lang 日本語"),
    ("/system [text|clear]", "set a system prompt"),
    ("/temp [0-2|off]", "sampling temperature"),
    ("/ctx [tokens|off]", "context window size (num_ctx)"),
    ("/file <path>", "attach a text file or image to your next message"),
    ("/retry", "regenerate the last answer"),
    ("/clear", "start a fresh conversation"),
    ("/save [name]", "save the chat as Markdown"),
    ("/pull <model>", "download a model"),
    ("/ps", "models currently loaded in memory"),
    ("/unload", "unload the current model from memory"),
    ("/info", "details about the current model"),
    ("/theme [name]", "code highlighting theme"),
    ("/exit", "quit (Ctrl+D also works)"),
]

LANG_HINTS = ["off", "English", "Spanish", "French", "German", "Dutch", "Portuguese", "Italian",
              "Japanese", "Chinese", "Korean", "Hindi", "Arabic", "Russian"]


class Chat:
    def __init__(self, console: Console, client: Client, cfg: dict):
        self.console = console
        self.client = client
        self.cfg = cfg
        self.st = State(
            base=client.base,
            think=cfg.get("think", "on"),
            show_thoughts=cfg.get("show_thoughts", True),
            lang=cfg.get("lang"),
            theme=cfg.get("theme", "monokai"),
        )
        self.attachments: list[str] = []
        self.images: list[str] = []
        self.model_names: list[str] = []

        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event):
            buf = event.current_buffer
            cs = buf.complete_state
            if cs and cs.current_completion:
                buf.apply_completion(cs.current_completion)
            else:
                buf.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        self.session = PromptSession(
            history=FileHistory(str(HISTORY_FILE)),
            multiline=True,
            key_bindings=kb,
            completer=self.build_completer(),
            complete_while_typing=True,
            prompt_continuation="  ┆ ",
            bottom_toolbar=self.toolbar,
        )
        self.handlers = {
            "/help": self.cmd_help, "/model": self.cmd_model, "/models": self.cmd_models,
            "/think": self.cmd_think, "/thoughts": self.cmd_thoughts, "/lang": self.cmd_lang,
            "/system": self.cmd_system, "/temp": self.cmd_temp, "/ctx": self.cmd_ctx,
            "/file": self.cmd_file, "/retry": self.cmd_retry, "/clear": self.cmd_clear,
            "/save": self.cmd_save, "/pull": self.cmd_pull, "/ps": self.cmd_ps,
            "/unload": self.cmd_unload, "/info": self.cmd_info, "/theme": self.cmd_theme,
        }

    # ----- prompt niceties --------------------------------------------------

    def build_completer(self) -> NestedCompleter:
        return NestedCompleter.from_nested_dict({
            "/help": None,
            "/model": {n: None for n in self.model_names},
            "/models": None,
            "/think": {k: None for k in ("on", "off", "low", "medium", "high")},
            "/thoughts": None,
            "/lang": {k: None for k in LANG_HINTS},
            "/system": {"clear": None},
            "/temp": None,
            "/ctx": None,
            "/file": PathCompleter(expanduser=True),
            "/retry": None,
            "/clear": None,
            "/save": None,
            "/pull": None,
            "/ps": None,
            "/unload": None,
            "/info": None,
            "/theme": {k: None for k in ("monokai", "dracula", "github-dark", "nord", "one-dark",
                                         "solarized-dark", "gruvbox-dark", "native", "default")},
            "/exit": None,
        })

    def toolbar(self) -> str:
        st = self.st
        bits = [st.model or "no model"]
        if "thinking" in st.caps:
            bits.append(f"think: {st.think}")
        if st.lang:
            bits.append(f"lang: {st.lang}")
        if st.system:
            bits.append("system prompt")
        if st.temperature is not None:
            bits.append(f"temp {st.temperature}")
        if self.attachments or self.images:
            bits.append(f"📎 {len(self.attachments) + len(self.images)}")
        return " " + "  ·  ".join(bits) + "  ·  /help "

    def refresh_models(self) -> list[dict]:
        models = self.client.models()
        self.model_names = [m["name"] for m in models]
        self.session.completer = self.build_completer()
        return models

    def match_model(self, wanted: str) -> str | None:
        for cand in (wanted, f"{wanted}:latest"):
            if cand in self.model_names:
                return cand
        return None

    def set_model(self, name: str) -> None:
        st = self.st
        st.model = name
        try:
            st.caps = self.client.show(name).get("capabilities") or []
        except (OllamaError, requests.RequestException):
            st.caps = []
        self.cfg["model"] = name
        save_config(self.cfg)
        tags = []
        if "thinking" in st.caps:
            tags.append("💭 thinking")
        if "vision" in st.caps:
            tags.append("🖼  vision")
        self.console.print(f"[green]●[/] model: [bold]{escape(name)}[/]  [dim]{'  '.join(tags)}[/]")

    # ----- main loop --------------------------------------------------------

    def run(self, model_arg: str | None, first_prompt: str = "") -> None:
        st, console = self.st, self.console
        models = self.refresh_models()
        wanted = model_arg or self.cfg.get("model")
        if wanted:
            match = self.match_model(wanted)
            if match:
                self.set_model(match)
            elif model_arg:
                console.print(f"[yellow]●[/] {escape(model_arg)} isn't installed (try [bold]/pull {escape(model_arg)}[/]).")
        if not st.model:
            if models:
                self.cmd_model("")
            else:
                console.print("[yellow]●[/] No models installed yet. Try [bold]/pull llama3.2[/].")

        console.print(Panel.fit(
            Text.from_markup(
                f"[bold cyan]om[/] · chat with Ollama\n"
                f"[dim]server[/] {st.base}   [dim]model[/] {escape(st.model or '(none)')}\n\n"
                "[dim]Enter[/] send · [dim]Ctrl+J[/]/[dim]Alt+Enter[/] newline · [dim]/help[/] commands\n"
                "[dim]Ctrl+C[/] stop a reply · [dim]Ctrl+D[/] quit"
            ),
            border_style="cyan",
        ))

        pending = first_prompt.strip()
        while True:
            try:
                text = pending or self.session.prompt([("bold fg:ansicyan", "you ▸ ")])
                pending = ""
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self.command(text):
                    break
                continue
            self.send(self.build_user_message(text))
        console.print("[dim]bye 👋[/]")

    def command(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/exit", "/quit", "/q"):
            return True
        fn = self.handlers.get(cmd)
        if not fn:
            self.console.print(f"[red]Unknown command[/] {escape(cmd)}. Try /help")
            return False
        try:
            fn(arg.strip())
        except OllamaError as e:
            self.console.print(f"[red]✗[/] {escape(str(e))}")
        except requests.RequestException as e:
            self.console.print(f"[red]✗[/] Can't reach Ollama: {escape(str(e))}")
        except KeyboardInterrupt:
            self.console.print("[dim]cancelled[/]")
        return False

    # ----- sending / streaming ---------------------------------------------

    def build_user_message(self, text: str) -> dict:
        content = "\n\n".join(self.attachments + [text])
        msg: dict = {"role": "user", "content": content}
        if self.images:
            msg["images"] = self.images
        self.attachments, self.images = [], []
        return msg

    def payload(self) -> dict:
        st = self.st
        msgs = list(st.messages)
        sp = st.system_prompt()
        if sp:
            msgs.insert(0, {"role": "system", "content": sp})
        p: dict = {"model": st.model, "messages": msgs, "stream": True}
        opts = {}
        if st.temperature is not None:
            opts["temperature"] = st.temperature
        if st.num_ctx:
            opts["num_ctx"] = st.num_ctx
        if opts:
            p["options"] = opts
        think = st.think_param()
        if think is not None:
            p["think"] = think
        return p

    def _consume(self, payload: dict, view: StreamView) -> dict:
        splitter = None if "thinking" in self.st.caps else ThinkSplitter()
        final: dict = {}
        stream = self.client.chat_stream(payload)
        try:
            with Live(view, console=self.console, refresh_per_second=15,
                      transient=True, vertical_overflow="visible"):
                for chunk in stream:
                    m = chunk.get("message") or {}
                    if m.get("thinking"):
                        view.thinking += m["thinking"]
                    if m.get("content"):
                        if splitter:
                            t, a = splitter.feed(m["content"])
                            view.thinking += t
                            view.answer += a
                        else:
                            view.answer += m["content"]
                    if chunk.get("done"):
                        final = chunk
                if splitter:
                    t, a = splitter.flush()
                    view.thinking += t
                    view.answer += a
        finally:
            stream.close()
        return final

    def send(self, msg: dict) -> None:
        st, console = self.st, self.console
        if not st.model:
            self.cmd_model("")
            if not st.model:
                console.print("[yellow]●[/] Pick a model first (/model or /pull).")
                return
        st.messages.append(msg)
        payload = self.payload()
        view = StreamView(st)
        final: dict = {}
        interrupted = False
        error: str | None = None

        console.print(f"\n[bold green]{escape(st.model)}[/]")
        try:
            try:
                final = self._consume(payload, view)
            except OllamaError as e:
                # Model turned out not to support `think`: retry once without it.
                if "think" in payload and "think" in str(e).lower():
                    payload.pop("think")
                    st.caps = [c for c in st.caps if c != "thinking"]
                    view.thinking = view.answer = ""
                    final = self._consume(payload, view)
                else:
                    raise
        except KeyboardInterrupt:
            interrupted = True
        except OllamaError as e:
            error = str(e)
        except requests.RequestException as e:
            error = f"Lost connection to Ollama ({e}). Is it running? Try: om --serve"

        answer = view.answer.strip()
        thinking = view.thinking.strip()
        if thinking and st.show_thoughts:
            console.print(Panel(
                Text(thinking, style="dim italic"),
                title="[dim]thinking[/]", title_align="left",
                border_style="dim", padding=(0, 1),
            ))
        if answer:
            console.print(Markdown(answer, code_theme=st.theme))
        if error:
            console.print(f"[red]✗[/] {escape(error)}")
        if interrupted:
            console.print("[dim italic]⏹ interrupted[/]")
        stats = self.stats_line(final)
        if stats:
            console.print(f"[dim]{stats}[/]")
        console.print()

        if answer:
            st.messages.append({"role": "assistant", "content": answer})
        else:
            st.messages.pop()  # nothing usable came back; drop the dangling user turn

    @staticmethod
    def stats_line(final: dict) -> str:
        n, d = final.get("eval_count"), final.get("eval_duration")
        if not n or not d:
            return ""
        parts = [f"{n} tokens", f"{n / (d / 1e9):.1f} tok/s"]
        if final.get("prompt_eval_count"):
            parts.insert(0, f"{final['prompt_eval_count']} prompt")
        if final.get("total_duration"):
            parts.append(f"{final['total_duration'] / 1e9:.1f}s")
        return " · ".join(parts)

    # ----- commands ---------------------------------------------------------

    def cmd_help(self, arg: str) -> None:
        t = Table(box=None, show_header=False, padding=(0, 2))
        for cmd, desc in COMMANDS:
            t.add_row(Text(cmd, style="cyan"), Text(desc, style="dim"))
        self.console.print(t)

    def cmd_models(self, arg: str) -> None:
        models = self.refresh_models()
        if not models:
            self.console.print("[dim]No models installed. Try /pull llama3.2[/]")
            return
        t = Table(box=None, header_style="dim")
        for col in ("model", "params", "quant", "size"):
            t.add_column(col)
        for m in models:
            d = m.get("details", {})
            mark = "[green]●[/] " if m["name"] == self.st.model else "  "
            t.add_row(mark + escape(m["name"]), d.get("parameter_size", ""),
                      d.get("quantization_level", ""), human_size(m.get("size", 0)))
        self.console.print(t)

    def cmd_model(self, arg: str) -> None:
        models = self.refresh_models()
        if arg:
            match = self.match_model(arg)
            if match:
                self.set_model(match)
            else:
                self.console.print(f"[yellow]●[/] {escape(arg)} isn't installed. Try [bold]/pull {escape(arg)}[/]")
            return
        if not models:
            self.console.print("[yellow]●[/] No models installed. Try [bold]/pull llama3.2[/]")
            return
        options = []
        for m in models:
            d = m.get("details", {})
            label = f"{m['name']:<38} {human_size(m.get('size', 0)):>9}  {d.get('parameter_size', ''):<7} {d.get('quantization_level', '')}"
            options.append((m["name"], label))
        choice = pick("Select a model", options, default=self.st.model)
        if choice:
            self.set_model(choice)

    def cmd_think(self, arg: str) -> None:
        st = self.st
        levels = ("on", "off", "low", "medium", "high")
        if not arg:
            self.console.print(f"thinking: [bold]{st.think}[/]  [dim](on/off/low/medium/high)[/]")
            return
        if arg.lower() not in levels:
            self.console.print(f"[red]Use one of:[/] {', '.join(levels)}")
            return
        st.think = arg.lower()
        self.cfg["think"] = st.think
        save_config(self.cfg)
        note = "" if "thinking" in st.caps else "  [dim](this model doesn't advertise thinking support)[/]"
        self.console.print(f"[green]●[/] thinking: [bold]{st.think}[/]{note}")

    def cmd_thoughts(self, arg: str) -> None:
        st = self.st
        st.show_thoughts = not st.show_thoughts
        self.cfg["show_thoughts"] = st.show_thoughts
        save_config(self.cfg)
        self.console.print(f"[green]●[/] thinking text: [bold]{'shown' if st.show_thoughts else 'hidden'}[/]")

    def cmd_lang(self, arg: str) -> None:
        st = self.st
        if not arg:
            self.console.print(f"reply language: [bold]{escape(st.lang or 'model default')}[/]")
            return
        st.lang = None if arg.lower() in ("off", "none", "auto") else arg
        self.cfg["lang"] = st.lang
        save_config(self.cfg)
        self.console.print(f"[green]●[/] reply language: [bold]{escape(st.lang or 'model default')}[/]")

    def cmd_system(self, arg: str) -> None:
        st = self.st
        if not arg:
            self.console.print(f"system prompt: {escape(st.system) if st.system else '[dim](none)[/]'}")
            return
        st.system = None if arg.lower() == "clear" else arg
        self.console.print("[green]●[/] system prompt " + ("cleared" if st.system is None else "set"))

    def cmd_temp(self, arg: str) -> None:
        st = self.st
        if not arg:
            self.console.print(f"temperature: {st.temperature if st.temperature is not None else 'model default'}")
            return
        if arg.lower() in ("off", "default"):
            st.temperature = None
        else:
            try:
                v = float(arg)
                if not 0 <= v <= 2:
                    raise ValueError
            except ValueError:
                self.console.print("[red]Give a number between 0 and 2, or 'off'.[/]")
                return
            st.temperature = v
        self.console.print(f"[green]●[/] temperature: {st.temperature if st.temperature is not None else 'model default'}")

    def cmd_ctx(self, arg: str) -> None:
        st = self.st
        if not arg:
            self.console.print(f"num_ctx: {st.num_ctx or 'model default'}")
            return
        if arg.lower() in ("off", "default"):
            st.num_ctx = None
        else:
            try:
                st.num_ctx = int(arg)
                if st.num_ctx < 256:
                    raise ValueError
            except ValueError:
                self.console.print("[red]Give a token count (e.g. 8192), or 'off'.[/]")
                return
        self.console.print(f"[green]●[/] num_ctx: {st.num_ctx or 'model default'}")

    def cmd_file(self, arg: str) -> None:
        if not arg:
            self.console.print("[dim]Usage: /file <path>   (text/code file or image)[/]")
            return
        p = Path(arg.strip("'\"")).expanduser()
        if not p.is_file():
            self.console.print(f"[red]✗[/] No such file: {escape(str(p))}")
            return
        if p.suffix.lower() in IMAGE_EXTS:
            if self.st.caps and "vision" not in self.st.caps:
                self.console.print("[yellow]●[/] This model doesn't advertise vision support; attaching anyway.")
            self.images.append(base64.b64encode(p.read_bytes()).decode())
            self.console.print(f"[green]📎[/] image attached: {escape(p.name)}")
            return
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > 200_000:
            text = text[:200_000]
            self.console.print("[yellow]●[/] File truncated to 200k characters.")
        self.attachments.append(f"File `{p.name}`:\n```{p.suffix.lstrip('.')}\n{text}\n```")
        self.console.print(f"[green]📎[/] attached: {escape(p.name)} [dim]({len(text):,} chars)[/]")

    def cmd_retry(self, arg: str) -> None:
        msgs = self.st.messages
        while msgs and msgs[-1]["role"] == "assistant":
            msgs.pop()
        if not msgs:
            self.console.print("[dim]Nothing to retry.[/]")
            return
        self.send(msgs.pop())

    def cmd_clear(self, arg: str) -> None:
        self.st.messages.clear()
        self.attachments, self.images = [], []
        self.console.print("[green]●[/] conversation cleared")

    def cmd_save(self, arg: str) -> None:
        st = self.st
        if not st.messages:
            self.console.print("[dim]Nothing to save yet.[/]")
            return
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(arg).name if arg else time.strftime("chat-%Y%m%d-%H%M%S")
        path = SAVE_DIR / (name if name.endswith(".md") else name + ".md")
        lines = [f"# om chat with {st.model}\n"]
        for m in st.messages:
            lines.append(f"## {'You' if m['role'] == 'user' else st.model}\n\n{m['content']}\n")
        path.write_text("\n".join(lines), encoding="utf-8")
        self.console.print(f"[green]●[/] saved to {path}")

    def cmd_pull(self, arg: str) -> None:
        if not arg:
            self.console.print("[dim]Usage: /pull <model>   e.g. /pull llama3.2[/]")
            return
        tasks: dict[str, int] = {}
        with Progress(
            TextColumn("[bold]{task.description}"), BarColumn(), DownloadColumn(),
            TransferSpeedColumn(), TimeRemainingColumn(), console=self.console,
        ) as progress:
            for ev in self.client.pull_stream(arg):
                digest = ev.get("digest")
                if digest and ev.get("total"):
                    if digest not in tasks:
                        tasks[digest] = progress.add_task(digest.split(":")[-1][:12], total=ev["total"])
                    progress.update(tasks[digest], completed=ev.get("completed", 0))
                else:
                    progress.console.print(f"[dim]{escape(ev.get('status', ''))}[/]")
        self.refresh_models()
        if not self.st.model and (match := self.match_model(arg)):
            self.set_model(match)

    def cmd_ps(self, arg: str) -> None:
        rows = self.client.running()
        if not rows:
            self.console.print("[dim]No models loaded in memory.[/]")
            return
        t = Table(box=None, header_style="dim")
        for col in ("model", "size", "vram", "unloads"):
            t.add_column(col)
        for m in rows:
            t.add_row(escape(m["name"]), human_size(m.get("size", 0)), human_size(m.get("size_vram", 0)),
                      str(m.get("expires_at", ""))[:19].replace("T", " "))
        self.console.print(t)

    def cmd_unload(self, arg: str) -> None:
        if not self.st.model:
            return
        self.client.unload(self.st.model)
        self.console.print(f"[green]●[/] unloaded {escape(self.st.model)}")

    def cmd_info(self, arg: str) -> None:
        if not self.st.model:
            self.console.print("[dim]No model selected.[/]")
            return
        info = self.client.show(self.st.model)
        d = info.get("details", {})
        ctx = next((v for k, v in info.get("model_info", {}).items() if k.endswith(".context_length")), None)
        t = Table.grid(padding=(0, 2))
        rows = [("model", self.st.model), ("family", d.get("family")), ("parameters", d.get("parameter_size")),
                ("quantization", d.get("quantization_level")), ("max context", f"{ctx:,}" if ctx else None),
                ("capabilities", ", ".join(info.get("capabilities") or []) or None)]
        for k, v in rows:
            if v:
                t.add_row(f"[dim]{k}[/]", escape(str(v)))
        self.console.print(t)

    def cmd_theme(self, arg: str) -> None:
        from pygments.styles import get_style_by_name
        from pygments.util import ClassNotFound

        if not arg:
            self.console.print(f"code theme: [bold]{self.st.theme}[/]  [dim](try monokai, dracula, github-dark, nord, one-dark…)[/]")
            return
        try:
            get_style_by_name(arg)
        except ClassNotFound:
            self.console.print(f"[red]✗[/] Unknown theme {escape(arg)}")
            return
        self.st.theme = arg
        self.cfg["theme"] = arg
        save_config(self.cfg)
        self.console.print(f"[green]●[/] code theme: [bold]{escape(arg)}[/]")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    if os.name == "nt":
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass

    ap = argparse.ArgumentParser(prog="om", description="Rich terminal chat for Ollama.")
    ap.add_argument("prompt", nargs="*", help="optional first message")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--serve", action="store_true", help="start `ollama serve` in the background")
    g.add_argument("--end", action="store_true", help="stop the server started with --serve")
    g.add_argument("--status", action="store_true", help="show server status")
    ap.add_argument("-m", "--model", help="model to use (default: last used, else pick)")
    ap.add_argument("--host", help="Ollama address (default: $OLLAMA_HOST or 127.0.0.1:11434)")
    args = ap.parse_args()

    console = Console(highlight=False)
    base = resolve_host(args.host)

    if args.serve:
        sys.exit(0 if start_server(console, base) else 1)
    if args.end:
        stop_server(console, base)
        return
    if args.status:
        show_status(console, base)
        return

    if not server_up(base):
        console.print(f"[yellow]●[/] No Ollama server at {base}.")
        if not Confirm.ask("  Start one in the background now?", default=True, console=console):
            return
        if not start_server(console, base):
            sys.exit(1)

    try:
        Chat(console, Client(base), load_config()).run(args.model, " ".join(args.prompt))
    except (OllamaError, requests.RequestException) as e:
        console.print(f"[red]✗[/] {escape(str(e))}")
        sys.exit(1)


if __name__ == "__main__":
    main()
