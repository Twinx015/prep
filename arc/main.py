#!/usr/bin/env python3
# pkgm - pacman/AUR manager TUI, jobs run detached so closing the terminal is safe
# dep: python-textual  ->  sudo pacman -S python-textual
import asyncio, difflib, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time
import urllib.parse, urllib.request
from pathlib import Path

JOBS = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "pkgm" / "jobs"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
IS_ROOT = os.geteuid() == 0
# yay first, paru as fallback (helpers refuse to run as root)
AUR = None if IS_ROOT else next((h for h in ("yay", "paru") if shutil.which(h)), None)
AUR_EXTRA = {"yay": ["--answerdiff", "None", "--answerclean", "None", "--answeredit", "None"],
             "paru": ["--skipreview"]}
SUDO = [] if IS_ROOT else ["sudo", "-A"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


# ---------- command builders ----------

def pac(*a):
    return shlex.join([*SUDO, "pacman", *a])


def aurcmd(*a):
    return shlex.join([AUR, "--noconfirm", "--sudoflags", "-A", *AUR_EXTRA[AUR], *a])


def run(*cmd):
    # stdout split into words, [] on any failure
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.stdout.split() if r.returncode == 0 else []
    except Exception:
        return []


# ---------- package index + fuzzy matching ----------

def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


class Index:
    def __init__(self):
        self.repo, self.installed, self.keys = set(), set(), {}

    def refresh(self):
        repo, inst, keys = set(run("pacman", "-Slq")), set(run("pacman", "-Qq")), {}
        for p in repo | inst:
            keys.setdefault(norm(p), []).append(p)
        self.repo, self.installed, self.keys = repo, inst, keys

    def match(self, term, pool=None, deep=False):
        # ranked names: same letters first (zenbrowser -> zen-browser), then substring, then typos
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


def aur_names(term):
    # ask the AUR RPC: exact probes with dash variants + prefix suggestions
    t = term.lower()
    variants = ({t} | {t[:i] + "-" + t[i:] for i in range(1, len(t))}) if len(t) <= 40 else {t}

    def get(url):
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)

    found = []
    try:
        q = "&".join("arg[]=" + urllib.parse.quote(v) for v in sorted(variants))
        found += [x["Name"] for x in get(f"https://aur.archlinux.org/rpc/v5/info?{q}").get("results", [])]
        found += [s for s in get(f"https://aur.archlinux.org/rpc/v5/suggest/{urllib.parse.quote(t)}") if s not in found]
    except Exception:
        pass
    found.sort(key=lambda p: (p != t, norm(p) != norm(t)))
    return found


def resolve_install(idx, tok):
    # -> ("ok", name, "repo"|"aur") | ("pick", [(name, src)]) | ("none",)
    if tok in idx.repo:
        return ("ok", tok, "repo")
    if tok in idx.installed and AUR:
        return ("ok", tok, "aur")
    n = norm(tok)
    loc = idx.match(tok, idx.repo, deep=True)
    eq = [p for p in loc if norm(p) == n]
    if len(eq) == 1:
        return ("ok", eq[0], "repo")
    aur = aur_names(tok) if AUR else []
    if tok in aur:
        return ("ok", tok, "aur")
    aeq = [p for p in aur if norm(p) == n]
    if not eq and len(aeq) == 1:
        return ("ok", aeq[0], "aur")
    opts = [(p, "repo") for p in loc] + [(p, "aur") for p in aur if p not in loc]
    return ("pick", opts[:10]) if opts else ("none",)


def resolve_remove(idx, tok):
    if tok in idx.installed:
        return ("ok", tok, "local")
    c = idx.match(tok, idx.installed, deep=True)
    eq = [p for p in c if norm(p) == norm(tok)]
    if len(eq) == 1:
        return ("ok", eq[0], "local")
    return ("pick", [(p, "local") for p in c]) if c else ("none",)


# ---------- detached jobs (own session, log + rc files on disk) ----------

def sudo_nopass():
    # True when sudo works without any password (NOPASSWD)
    return subprocess.run(["sudo", "-n", "-k", "true"], capture_output=True).returncode == 0


def sudo_ok(pw):
    r = subprocess.run(["sudo", "-S", "-k", "-v"], input=pw + "\n", capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def new_job(title, script, password=None):
    jid = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while (JOBS / jid).exists():
        n += 1
        jid = time.strftime("%Y%m%d-%H%M%S") + f"-{n}"
    d = JOBS / jid
    d.mkdir(parents=True)
    env = os.environ.copy()
    askpass = None
    if password is not None:
        # sudo -A reads the password from this 0700 helper, deleted when the job ends
        askpass = RUNTIME / f"pkgm-askpass-{jid}"
        fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(password) + "\n")
        env["SUDO_ASKPASS"] = str(askpass)
    rm = f"rm -f {shlex.quote(str(askpass))}; " if askpass else ""
    wrapper = (f"trap '' HUP; printf '$ %s\\n' {shlex.quote(script)}; {script}; rc=$?; "
               f"echo \"[exit $rc]\"; echo $rc > {shlex.quote(str(d / 'rc'))}; {rm}exit $rc")
    p = subprocess.Popen(["sh", "-c", wrapper], stdin=subprocess.DEVNULL, stdout=open(d / "log", "ab"),
                         stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd=Path.home())
    threading.Thread(target=p.wait, daemon=True).start()  # reap so it never lingers as a zombie
    (d / "meta.json").write_text(json.dumps({"title": title, "cmd": script, "start": time.time(), "pid": p.pid}))
    return jid


def job_info(d):
    try:
        m = json.loads((d / "meta.json").read_text())
        rc = (d / "rc").read_text().strip() if (d / "rc").exists() else None
    except (OSError, ValueError):
        return None
    if rc is not None:
        st = "ok" if rc == "0" else f"failed ({rc})"
    elif alive(m["pid"]):
        st = "running"
    else:
        st = "killed"
    return {"id": d.name, "st": st, "title": m["title"], "pid": m["pid"],
            "time": time.strftime("%H:%M:%S", time.localtime(m["start"]))}


# ---------- TUI ----------

def run_tui():
    try:
        from rich.text import Text
        from textual import on, work
        from textual.app import App
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical
        from textual.screen import ModalScreen
        from textual.suggester import Suggester
        from textual.widgets import DataTable, Footer, Header, Input, OptionList, RichLog, Static
    except ImportError:
        sys.exit("missing dep -> sudo pacman -S python-textual   (or: pip install textual)")

    class PkgSuggester(Suggester):
        # inline grey completion for the word being typed (accept with Right arrow)
        def __init__(self, idx):
            super().__init__(use_cache=False, case_sensitive=True)
            self.idx = idx

        async def get_suggestion(self, value):
            word = value.split(" ")[-1]
            if len(word) < 2:
                return None
            best = [p for p in self.idx.match(word) if p.startswith(word)]
            return value[: len(value) - len(word)] + best[0] if best else None

    class Pick(ModalScreen):
        BINDINGS = [Binding("escape", "cancel", "cancel")]

        def __init__(self, term, opts):
            super().__init__()
            self.term, self.opts = term, opts

        def compose(self):
            with Vertical(id="pick"):
                yield Static(Text(f"'{self.term}' not found - did you mean:"))
                yield OptionList(*[Text.assemble(n, "   ", (s, "dim")) for n, s in self.opts])

        def on_mount(self):
            ol = self.query_one(OptionList)
            ol.highlighted = 0
            ol.focus()

        def on_option_list_option_selected(self, e):
            self.dismiss(self.opts[e.option_index])

        def action_cancel(self):
            self.dismiss(None)

    class Pass(ModalScreen):
        BINDINGS = [Binding("escape", "cancel", "cancel")]

        def compose(self):
            with Vertical(id="pass"):
                yield Static("sudo password (kept in memory only, never saved)")
                yield Input(password=True, id="pw")
                yield Static("", id="err")

        def on_mount(self):
            self.query_one(Input).focus()

        @on(Input.Submitted)
        def submit(self, e):
            e.stop()
            self.check(e.value)

        @work
        async def check(self, pw):
            if await asyncio.to_thread(sudo_ok, pw):
                self.dismiss(pw)
            else:
                self.query_one("#err", Static).update("wrong password, try again")
                self.query_one(Input).value = ""

        def action_cancel(self):
            self.dismiss(None)

    class Pkgm(App):
        TITLE = "pkgm"
        SUB_TITLE = f"pacman + {AUR}" if AUR else "pacman only"
        CSS = """
        #left {width: 48; border-right: solid $primary;}
        #hint {height: 2; padding: 0 1;}
        #jobs {height: 1fr;}
        #log {width: 1fr;}
        Pick, Pass {align: center middle;}
        #pick, #pass {width: 64; height: auto; max-height: 20; border: round $primary; background: $surface; padding: 1 2;}
        """
        BINDINGS = [
            Binding("slash", "focus_input", "name"),
            Binding("escape", "focus_table", "jobs", show=False),
            Binding("i", "install", "install"),
            Binding("r", "remove", "remove"),
            Binding("u", "update", "update"),
            Binding("o", "orphans", "orphans"),
            Binding("c", "clean", "clean cache"),
            Binding("k", "check", "check updates"),
            Binding("s", "search", "search"),
            Binding("x", "kill", "kill job"),
            Binding("d", "clear", "clear done"),
            Binding("q", "quit", "quit"),
        ]

        def __init__(self):
            super().__init__()
            self.idx = Index()
            self.pw = self.nopass = self.cur = self.want = None
            self.off, self.sig, self.last = 0, None, {}

        def compose(self):
            yield Header()
            with Horizontal():
                with Vertical(id="left"):
                    yield Input(placeholder="package name(s), Enter = install", id="pkg",
                                suggester=PkgSuggester(self.idx))
                    yield Static("", id="hint")
                    yield DataTable(id="jobs", cursor_type="row")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=5000)
            yield Footer()

        def on_mount(self):
            t = self.query_one("#jobs", DataTable)
            t.add_columns("ID", "Status", "Job", "Time")
            t.focus()
            self.load_index()
            self.set_interval(0.5, self.tick)

        # ----- index + live hint -----

        @work(thread=True, exclusive=True, group="idx")
        def load_index(self):
            self.idx.refresh()
            self.call_from_thread(self.show_hint)

        @on(Input.Changed, "#pkg")
        def changed(self, e):
            self.show_hint()

        def show_hint(self):
            word = (self.query_one("#pkg", Input).value.split() or [""])[-1]
            msg = ""
            if len(norm(word)) >= 2:
                c = self.idx.match(word)
                if word in c:
                    msg = "exists locally" + (" (installed)" if word in self.idx.installed else " (repo)")
                elif c:
                    msg = "did you mean: " + ", ".join(c[:4])
                else:
                    msg = "no local match (AUR is checked on Enter)"
            self.query_one("#hint", Static).update(Text(msg, style="dim"))

        # ----- actions -----

        def action_focus_input(self):
            self.query_one("#pkg", Input).focus()

        def action_focus_table(self):
            self.query_one("#jobs", DataTable).focus()

        def typed(self):
            # names from the input box, or jump to it when empty
            names = self.query_one("#pkg", Input).value.split()
            if not names:
                self.action_focus_input()
                self.notify("type a package name first")
            return names

        @on(Input.Submitted, "#pkg")
        def submitted(self, e):
            self.action_install()

        def action_install(self):
            names = self.typed()
            if names:
                self.do_pkg(names, "install")

        def action_remove(self):
            names = self.typed()
            if names:
                self.do_pkg(names, "remove")

        def action_update(self):
            parts = [pac("-Syu", "--noconfirm")] + ([aurcmd("-Sua")] if AUR else [])
            self.go("update system", " && ".join(parts))

        def action_clean(self):
            cmd = shlex.join([*SUDO, "paccache", "-rk2"]) if shutil.which("paccache") else pac("-Sc", "--noconfirm")
            self.go("clean cache", cmd)

        def action_check(self):
            base = "checkupdates" if shutil.which("checkupdates") else "pacman -Qu"
            self.go("check updates", base + (f"; {AUR} -Qua" if AUR else "") + "; true", root=False)

        def action_search(self):
            names = self.typed()
            if names:
                cmd = shlex.join(["pacman", "-Ss", *names]) + (f"; {shlex.join([AUR, '-Ss', *names])}" if AUR else "")
                self.go("search " + " ".join(names), cmd + "; true", root=False)

        def action_orphans(self):
            self.do_orphans()

        def action_kill(self):
            info = self.cur and job_info(JOBS / self.cur)
            if info and info["st"] == "running":
                try:
                    os.killpg(info["pid"], signal.SIGTERM)
                    self.notify("SIGTERM sent")
                except OSError:
                    pass

        def action_clear(self):
            for d in JOBS.glob("*"):
                i = job_info(d)
                if i and i["st"] != "running":
                    shutil.rmtree(d, ignore_errors=True)
            self.cur, self.sig = None, None
            self.query_one("#log", RichLog).clear()
            self.tick()

        # ----- workers -----

        @work
        async def go(self, title, script, root=True):
            await self.launch(title, script, root)

        @work
        async def do_orphans(self):
            names = await asyncio.to_thread(run, "pacman", "-Qdtq")
            if not names:
                self.notify("no orphans")
                return
            await self.launch("remove orphans", pac("-Rns", "--noconfirm", *names))

        @work(exclusive=True, group="resolve")
        async def do_pkg(self, names, mode):
            picks = []
            for tok in names:
                fn = resolve_install if mode == "install" else resolve_remove
                res = await asyncio.to_thread(fn, self.idx, tok)
                if res[0] == "none":
                    self.notify(f"'{tok}' not found", severity="error")
                    return
                if res[0] == "pick":
                    choice = await self.push_screen_wait(Pick(tok, res[1]))
                    if choice is None:
                        return
                    picks.append(choice)
                else:
                    if res[1] != tok:
                        self.notify(f"{tok} -> {res[1]}")
                    picks.append((res[1], res[2]))
            title = f"{mode} " + " ".join(n for n, _ in picks)
            if mode == "install":
                repo = [n for n, s in picks if s == "repo"]
                aur = [n for n, s in picks if s == "aur"]
                parts = []
                if repo:
                    parts.append(pac("-S", "--needed", "--noconfirm", *repo))  # no -y: avoids partial upgrades
                if aur:
                    parts.append(aurcmd("-S", "--needed", *aur))
                await self.launch(title, " && ".join(parts))
            else:
                await self.launch(title, pac("-Rns", "--noconfirm", *[n for n, _ in picks]))
            self.query_one("#pkg", Input).value = ""

        async def launch(self, title, script, root=True):
            pw = None
            if root and not IS_ROOT:
                if self.nopass is None:
                    self.nopass = await asyncio.to_thread(sudo_nopass)
                if not self.nopass:
                    if self.pw is None:
                        self.pw = await self.push_screen_wait(Pass())
                        if self.pw is None:
                            return
                    pw = self.pw
            self.want = new_job(title, script, pw)
            self.tick()

        # ----- job table + log tail -----

        def tick(self):
            infos = [i for i in map(job_info, sorted(JOBS.glob("*"), reverse=True)[:40]) if i]
            for i in infos:
                old = self.last.get(i["id"])
                if old == "running" and i["st"] != "running":
                    self.notify(f"{i['title']}: {i['st']}", severity="information" if i["st"] == "ok" else "error")
                    self.load_index()
                self.last[i["id"]] = i["st"]
            sig = [(i["id"], i["st"]) for i in infos]
            if sig != self.sig:
                self.sig = sig
                keep, self.want = self.want or self.cur, None
                t = self.query_one("#jobs", DataTable)
                t.clear()
                for i in infos:
                    color = "yellow" if i["st"] == "running" else "green" if i["st"] == "ok" else "red"
                    t.add_row(i["id"][-6:], Text(i["st"], style=color), i["title"], i["time"], key=i["id"])
                ids = [s[0] for s in sig]
                if keep in ids:
                    t.move_cursor(row=ids.index(keep))
            self.pump()

        @on(DataTable.RowHighlighted)
        def picked(self, e):
            jid = e.row_key.value if e.row_key else None
            if jid and jid != self.cur:
                self.cur, self.off = jid, 0
                self.query_one("#log", RichLog).clear()
                self.pump()

        def pump(self):
            # append new log bytes of the selected job (whole lines only until the job ends)
            if not self.cur:
                return
            try:
                with open(JOBS / self.cur / "log", "rb") as fh:
                    fh.seek(self.off)
                    data = fh.read()
            except OSError:
                return
            if self.last.get(self.cur) == "running":
                data = data[: data.rfind(b"\n") + 1]
            if not data:
                return
            self.off += len(data)
            log = self.query_one("#log", RichLog)
            text = ANSI.sub("", data.decode(errors="replace")).replace("\r\n", "\n")
            text = text[:-1] if text.endswith("\n") else text
            for line in text.split("\n"):
                log.write(Text(line.split("\r")[-1]))

    Pkgm().run()


def main():
    if not shutil.which("pacman"):
        sys.exit("pacman not found")
    JOBS.mkdir(parents=True, exist_ok=True)
    run_tui()


if __name__ == "__main__":
    main()
