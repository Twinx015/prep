const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

const root = document.documentElement.style;
document.title = INIT.title || document.title;
if (INIT.title) $("#app-title").textContent = INIT.title;
for (const [k, v] of [["accent", "--accent"], ["background", "--bg"], ["surface", "--surface"],
                      ["panel", "--panel"], ["foreground", "--fg"], ["dim", "--dim"],
                      ["line", "--line"], ["log_bg", "--log-bg"]]) {
  if (INIT[k]) root.setProperty(v, INIT[k]);
}

function note(text, err = false) {
  const el = $("#status");
  el.textContent = text;
  el.className = err ? "msg err" : "msg";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

let ctrl = null; // AbortController for the stream in flight

async function stream(path, body, onEvent) {
  ctrl = new AbortController();
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (line) onEvent(JSON.parse(line.slice(6)));
      }
    }
  } finally {
    ctrl = null;
  }
}

function rows(table, items, render, onPick) {
  const tb = table.querySelector("tbody");
  tb.innerHTML = items.map((it, i) => `<tr data-i="${i}">${render(it)}</tr>`).join("");
  tb.onclick = (ev) => {
    const tr = ev.target.closest("tr");
    if (!tr) return;
    tb.querySelectorAll("tr").forEach((r) => r.classList.toggle("sel", r === tr));
    onPick && onPick(items[+tr.dataset.i], +tr.dataset.i);
  };
}

function show(tab) {
  $$("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $$("section").forEach((s) => s.classList.toggle("on", s.id === "tab-" + tab));
  if (tab === "jobs") { loadJobs(); startJobs(); } else stopJobs();
}
$$("nav button").forEach((b) => (b.onclick = () => show(b.dataset.tab)));

async function loadState() {
  try {
    const s = await api("/api/state");
    note(s.ollama_ok ? `ollama ${s.version} · ${s.helper || "no aur helper"} · ${s.packages.installed || 0} installed`
                     : `ollama offline at ${s.host}`, !s.ollama_ok);
    if (!INIT.allow_root_actions) {
      $$(".rootonly").forEach((el) => el.classList.add("hidden"));
    }
  } catch (e) { note(e.message, true); }
}

// chat

let active = null, streaming = false;

function bubble(who, text, extra = "") {
  const div = document.createElement("div");
  div.className = "bubble " + who;
  div.innerHTML = `<div class="who">${who === "user" ? "you" : "assistant"}</div>
                   <div class="body"></div>${extra}`;
  $("#msgs").appendChild(div);
  return div;
}

function drawMessage(m) {
  if (m.role === "user") { bubble("user", "", "").querySelector(".body").innerHTML = esc(m.content); return; }
  const st = m.stats ? `<div class="stats">${Math.round(m.stats.tps || 0)} tok/s · ${(+m.stats.total_s || 0).toFixed(1)}s</div>` : "";
  const b = bubble("assistant", "", st);
  setBody(b, m.content, m.thinking || "");
}

function setBody(el, content, thinking) {
  const body = el.querySelector(".body");
  const think = thinking ? `<div class="think">${esc(thinking.slice(-600))}</div>` : "";
  const parts = content.split(/```/).map((chunk, i) =>
    i % 2 ? `<pre>${esc(chunk.replace(/^[^\n]*\n/, ""))}</pre>` : `<div>${esc(chunk)}</div>`);
  body.innerHTML = think + parts.join("");
}

async function loadChats() {
  const list = await api("/api/chats");
  const cur = await api("/api/chat");
  active = cur.id;
  const sel = $("#chat-sel");
  sel.innerHTML = list.map((c) => `<option value="${c.id}" ${c.id === cur.id ? "selected" : ""}>${esc(c.title)}</option>`).join("");
  $("#msgs").innerHTML = "";
  if (!cur.messages.length) {
    $("#msgs").innerHTML = `<div class="bubble assistant"><div class="who">sys</div>
      <div class="body muted">Pick a model and start typing.</div></div>`;
  }
  cur.messages.forEach(drawMessage);
  $("#msgs").scrollTop = $("#msgs").scrollHeight;
}

async function loadModels() {
  try {
    const models = await api("/api/models");
    $("#model-sel").innerHTML = models.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
    rows($("#models-t"), models, (m) =>
      `<td><code>${esc(m.name)}</code></td><td>${((m.size || 0) / 1e9).toFixed(1)} GB</td>
       <td class="muted">${esc((m.modified_at || "").slice(0, 19))}</td>
       <td><button data-act="unload" data-name="${esc(m.name)}">unload</button>
           <button data-act="delete" data-name="${esc(m.name)}">delete</button></td>`,
      () => {});
    $("#models-t").onclick = async (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      ev.stopPropagation();
      const name = b.dataset.name;
      try {
        if (b.dataset.act === "unload") await api("/api/models/unload", { method: "POST", body: { name } });
        if (b.dataset.act === "delete") { if (!confirm(`Delete ${name}?`)) return; await api("/api/models/" + encodeURIComponent(name), { method: "DELETE" }); }
        loadModels();
      } catch (e) { note(e.message, true); }
    };
    const cur = await api("/api/chat");
    if (cur.model) $("#model-sel").value = cur.model;
  } catch (e) { note(e.message, true); }
}

async function pullModel() {
  const name = $("#pull-name").value.trim();
  if (!name) return;
  $("#pull-go").disabled = true;
  const out = $("#pull-msg");
  try {
    await stream("/api/models/pull", { name }, (o) => {
      if (o.error) throw new Error(o.error);
      const p = o.total ? Math.round((o.completed / o.total) * 100) + "%" : o.status || "";
      out.textContent = p;
    });
    out.textContent = "done";
    loadModels();
  } catch (e) { out.textContent = e.message; note(e.message, true); }
  $("#pull-go").disabled = false;
}

$("#pull-go").onclick = pullModel;
$("#models-reload").onclick = loadModels;

async function sendChat(text) {
  if (streaming || !text.trim()) return;
  streaming = true;
  $("#send button").disabled = true;
  $("#msgs").querySelectorAll(".bubble.assistant .body.muted").forEach((e) => e.closest(".bubble").remove());
  drawMessage({ role: "user", content: text });
  const b = bubble("assistant", "", "");
  b.querySelector(".body").innerHTML = `<span class="muted">…</span>`;
  const acc = { content: "", thinking: "" };
  try {
    await stream("/api/chat/send", { text }, (o) => {
      if (o.type === "delta") {
        acc.content += o.content;
        acc.thinking += o.thinking;
        setBody(b, acc.content, acc.thinking);
        $("#msgs").scrollTop = $("#msgs").scrollHeight;
      } else if (o.type === "error") {
        b.querySelector(".body").innerHTML = `<span class="msg err">${esc(o.error)}</span>`;
      } else if (o.type === "done") {
        if (o.stats) {
          b.querySelector(".body").parentElement.insertAdjacentHTML(
            "beforeend",
            `<div class="stats">${Math.round(o.stats.tps || 0)} tok/s · ${(+o.stats.total_s || 0).toFixed(1)}s</div>`);
        }
        loadChats();
      }
    });
  } catch (e) {
    if (e.name === "AbortError") {
      note("stopped");
    } else {
      note(e.message, true);
      b.querySelector(".body").innerHTML = `<span class="msg err">${esc(e.message)}</span>`;
    }
  }
  streaming = false;
  $("#send button").disabled = false;
}

$("#send").onsubmit = (ev) => {
  ev.preventDefault();
  const text = $("#input").value;
  $("#input").value = "";
  sendChat(text);
};
$("#input").onkeydown = (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#send").requestSubmit(); }
};
$("#stop").onclick = () => {
  if (!ctrl) return note("nothing is streaming");
  ctrl.abort();
  note("stopping — the partial answer is kept");
};
$("#new-chat").onclick = async () => { await api("/api/chats", { method: "POST" }); await loadModels(); loadChats(); };
$("#del-chat").onclick = async () => {
  if (!active || !confirm("Delete this chat?")) return;
  await api("/api/chats/" + active, { method: "DELETE" });
  await loadModels(); loadChats();
};
$("#chat-sel").onchange = async (ev) => {
  await api("/api/chats/select", { method: "POST", body: { id: ev.target.value } });
  loadChats();
};
$("#model-sel").onchange = async (ev) => {
  try {
    const r = await api("/api/chat/model", { method: "POST", body: { name: ev.target.value } });
    note("model: " + r.model);
  } catch (e) { note(e.message, true); }
};

// cheats

async function loadCheats() {
  const q = $("#cheat-q").value;
  const tool = $("#cheat-sheet").value;
  const items = await api(`/api/cheats?q=${encodeURIComponent(q)}&tool=${encodeURIComponent(tool)}`);
  rows($("#cheats-t"), items, (c) =>
    `<td class="muted">${esc(c.tool)}/${esc(c.category)}</td><td><code>${esc(c.command)}</code></td><td>${esc(c.description)}</td>`,
    (c) => { $("#new-tool").value = c.tool; $("#new-cmd").value = c.command; $("#new-desc").value = c.description; });
  if (!$("#cheat-sheet").options.length) {
    const sheets = await api("/api/cheats/sheets");
    $("#cheat-sheet").innerHTML = `<option value="">all sheets</option>` +
      sheets.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  }
}

$("#cheat-q").oninput = () => loadCheats().catch((e) => note(e.message, true));
$("#cheat-sheet").onchange = () => loadCheats().catch((e) => note(e.message, true));
$("#cheat-reload").onclick = () => loadCheats().catch((e) => note(e.message, true));
$("#cheat-save").onclick = async () => {
  try {
    const r = await api("/api/cheats", {
      method: "POST",
      body: { tool: $("#new-tool").value, command: $("#new-cmd").value, description: $("#new-desc").value },
    });
    note("added to " + r.sheet);
    $("#new-cmd").value = ""; $("#new-desc").value = "";
    $("#cheat-sheet").innerHTML = "";
    loadCheats();
  } catch (e) { note(e.message, true); }
};
$("#cheat-run").onclick = async () => {
  const cmd = $("#new-cmd").value.trim();
  if (!cmd) return;
  try {
    const r = await api("/api/cheats/run", { method: "POST", body: { command: cmd } });
    note("job " + r.id.slice(-6) + " started");
    show("jobs");
  } catch (e) { note(e.message, true); }
};

// packages

async function loadPkgs() {
  const q = $("#pkg-q").value.trim();
  const r = await api(`/api/packages?q=${encodeURIComponent(q)}`);
  $("#pkg-hint").textContent = r.counts.installed
    ? `${r.counts.installed} installed · ${r.counts.repo} repo · ${r.counts.aur || 0} aur-ish`
    : "index empty — press reindex";
  rows($("#pkgs-t"), r.matches, (p) =>
    `<td><code>${esc(p.name)}</code></td><td class="muted">${esc(p.source)}</td>
     <td>${p.installed ? "installed" : ""}</td>`,
    (p) => { $("#pkg-q").value = p.name; });
}

let pkgDebounce = 0;
$("#pkg-q").oninput = () => {
  clearTimeout(pkgDebounce);
  pkgDebounce = setTimeout(() => loadPkgs().catch((e) => note(e.message, true)), 180);
};

async function pkgRun(action) {
  const names = $("#pkg-q").value.trim().split(/\s+/).filter(Boolean);
  try {
    const r = await api("/api/packages/run", {
      method: "POST",
      body: { action, names, password: $("#pkg-pw").value },
    });
    if (r.need_pick) return pickFlow(action, r.need_pick);
    note(`${action}: job ${r.id.slice(-6)}`);
    show("jobs");
  } catch (e) { note(e.message, true); }
}

function pickFlow(action, need) {
  const box = $("#pick-box");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="muted">which '${esc(need.term)}'?</div>` +
    need.options.map(([n, s]) => `<button data-n="${esc(n)}" data-s="${esc(s)}">${esc(n)} (${esc(s)})</button>`).join(" ");
  box.onclick = async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    box.classList.add("hidden");
    try {
      const r = await api("/api/packages/run", {
        method: "POST",
        body: { action, names: [], picks: [[b.dataset.n, b.dataset.s]], password: $("#pkg-pw").value },
      });
      if (r.need_pick) return;
      note(`${action}: job ${r.id.slice(-6)}`);
      show("jobs");
    } catch (e) { note(e.message, true); }
  };
}

["install", "remove", "update", "check", "orphans", "clean"].forEach((a) => {
  const btn = $("#pkg-" + a);
  if (btn) btn.onclick = () => pkgRun(a);
});
$("#pkg-index").onclick = async () => {
  note("reindexing…");
  try { await api("/api/packages/index", { method: "POST" }); loadPkgs(); note("index rebuilt"); }
  catch (e) { note(e.message, true); }
};

// jobs

let jobTimer = null, jobSel = null;
const startJobs = () => { if (!jobTimer) jobTimer = setInterval(loadJobs, 1500); };
const stopJobs = () => { clearInterval(jobTimer); jobTimer = null; };

async function loadJobs() {
  try {
    const jobs = await api("/api/jobs");
    rows($("#jobs-t"), jobs, (j) =>
      `<td class="muted">${esc(j.id.slice(-6))}</td><td>${esc(j.title)}</td>
       <td>${j.st === "running" ? "⏳" : j.st === "ok" ? "✓" : "✗"} ${esc(j.st)}</td>
       <td class="muted">${esc(j.time)}</td>`,
      async (j) => { jobSel = j.id; loadLog(); });
    if (!jobSel && jobs.length) { jobSel = jobs[0].id; loadLog(); }
    else if (jobSel) loadLog();
  } catch (e) { note(e.message, true); }
}

async function loadLog() {
  if (!jobSel) return;
  try {
    const r = await api(`/api/jobs/${jobSel}/log?offset=${loadLog.off || 0}`);
    if (r.text) loadLog.off = r.offset;
    if (r.text) $("#job-log").textContent += r.text;
    $("#job-log").scrollTop = $("#job-log").scrollHeight;
  } catch (e) { /* job may be gone */ }
}
loadLog.off = 0;

$("#jobs-reload").onclick = loadJobs;
$("#jobs-kill").onclick = async () => {
  if (!jobSel) return;
  try { await api(`/api/jobs/${jobSel}/kill`, { method: "POST" }); loadJobs(); }
  catch (e) { note(e.message, true); }
};
$("#jobs-clear").onclick = async () => {
  try { await api("/api/jobs", { method: "DELETE" }); jobSel = null; $("#job-log").textContent = ""; loadJobs(); }
  catch (e) { note(e.message, true); }
};

// boot

(async function boot() {
  await Promise.all([loadChats(), loadModels(), loadCheats(), loadPkgs(), loadJobs()]);
  await loadState();
})().catch((e) => note(e.message, true));
