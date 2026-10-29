const $ = (sel) => document.querySelector(sel);

const state = {
  title: "prep",
  up: false,
  chats: [],
  models: [],
  chat: null,
  model: localStorage.getItem("prep.model") || "",
  streaming: false,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("err", isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), isError ? 6000 : 2600);
}

function escapeHtml(text) {
  return text.replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function renderMarkdown(text) {
  const blocks = [];
  let out = String(text).replace(/```[a-zA-Z]*\n?([\s\S]*?)```/g, (m, code) => {
    blocks.push(
      `<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`,
    );
    return `\u0000${blocks.length - 1}\u0000`;
  });
  out = escapeHtml(out)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  const lines = out.split("\n");
  const html = [];
  let paragraph = [];
  let list = null;
  let quote = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      html.push(`<ul>${list.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      html.push(`<blockquote>${quote.join("<br>")}</blockquote>`);
      quote = [];
    }
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (const line of lines) {
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const cited = line.match(/^&gt;\s?(.*)$/);
    if (!line.trim()) {
      flushAll();
    } else if (/^\u0000\d+\u0000$/.test(line.trim())) {
      flushAll();
      html.push(line.trim());
    } else if (heading) {
      flushAll();
      html.push(`<h3>${heading[2]}</h3>`);
    } else if (bullet) {
      flushParagraph();
      flushQuote();
      list = list || [];
      list.push(bullet[1]);
    } else if (cited) {
      flushParagraph();
      flushList();
      quote.push(cited[1]);
    } else {
      flushList();
      flushQuote();
      paragraph.push(line);
    }
  }
  flushAll();
  return html
    .join("")
    .replace(/\u0000(\d+)\u0000/g, (m, i) => blocks[Number(i)]);
}

async function readNdjson(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const handle = (line) => {
    const trimmed = line.trim();
    if (trimmed) onEvent(JSON.parse(trimmed));
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n")) >= 0) {
      handle(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 1);
    }
  }
  handle(buffer);
}


function setSidebar(open) {
  $("#app").classList.toggle("sidebar-closed", !open);
  try {
    localStorage.setItem("prep.sidebar", open ? "open" : "closed");
  } catch (_) {}
}

function initSidebar() {
  let saved = null;
  try {
    saved = localStorage.getItem("prep.sidebar");
  } catch (_) {}
  const open = saved ? saved === "open" : window.innerWidth > 720;
  $("#app").classList.toggle("sidebar-closed", !open);
}


async function loadState() {
  try {
    const data = await api("api/state");
    state.up = data.up;
    state.title = data.title || "prep";
    $("#brand-title").textContent = state.title;
    document.title = `${state.title} - ollama chat`;
    $("#server-dot").className = `dot ${data.up ? "up" : "down"}`;
    $("#server-label").textContent = data.up
      ? `ollama ${data.version}`
      : "ollama offline";
  } catch (err) {
    $("#server-label").textContent = "server error";
  }
}

async function loadChats() {
  const data = await api("api/chats");
  state.chats = data.chats;
  renderChatList();
}

function renderChatList() {
  const list = $("#chat-list");
  list.innerHTML = "";
  for (const chat of state.chats) {
    const item = document.createElement("li");
    item.className = `chat-item${state.chat && state.chat.id === chat.id ? " active" : ""}`;
    item.title = chat.title || "Untitled chat";

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = chat.title || "Untitled chat";
    item.append(name);

    const actions = document.createElement("span");
    actions.className = "row-actions";
    actions.append(
      iconButton("✎", "Rename chat", async () => {
        const title = prompt("Chat title:", chat.title);
        if (title === null) return;
        await api(`api/chats/${chat.id}/rename`, {
          method: "POST",
          body: JSON.stringify({ title }),
        });
        await loadChats();
        if (state.chat && state.chat.id === chat.id)
          $("#chat-title").value = title;
      }),
      iconButton("✕", "Delete chat", async () => {
        if (!confirm(`Delete "${chat.title || "Untitled chat"}"?`)) return;
        await api(`api/chats/${chat.id}`, { method: "DELETE" });
        if (state.chat && state.chat.id === chat.id) {
          state.chat = null;
          renderMessages();
        }
        await loadChats();
      }),
    );
    item.append(actions);
    item.addEventListener("click", () => openChat(chat.id));
    list.append(item);
  }
}

function iconButton(label, title, onClick) {
  const button = document.createElement("button");
  button.className = "btn ghost tiny";
  button.type = "button";
  button.textContent = label;
  button.title = title;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick().catch((err) => toast(err.message, true));
  });
  return button;
}

async function openChat(chatId) {
  state.chat = await api(`api/chats/${chatId}`);
  $("#chat-title").value = state.chat.title || "";
  if (state.chat.model) {
    state.model = state.chat.model;
    localStorage.setItem("prep.model", state.model);
  }
  renderChatList();
  renderMessages();
  renderModelOptions();
  if (window.innerWidth <= 720) setSidebar(false);
}

async function newChat() {
  const chat = await api("api/chats", {
    method: "POST",
    body: JSON.stringify({ model: state.model }),
  });
  await loadChats();
  await openChat(chat.id);
  $("#input").focus();
  return chat;
}


function messageNode(message) {
  const node = document.createElement("div");
  node.className = `msg ${message.role}`;
  if (message.role === "assistant") {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = message.model || "assistant";
    node.append(who);
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (message.thinking) node.append(thinkingNode(message.thinking));
  if (message.role === "user") bubble.textContent = message.content || "";
  else bubble.innerHTML = renderMarkdown(message.content || "");
  node.append(bubble);
  return node;
}

function thinkingNode(text) {
  const node = document.createElement("details");
  node.className = "thinking";
  const summary = document.createElement("summary");
  summary.textContent = "thinking";
  const body = document.createElement("div");
  body.textContent = text;
  node.append(summary, body);
  return node;
}

const SPARK = `<svg class="spark" width="34" height="34" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M16 3v26M3 16h26M6.8 6.8l18.4 18.4M25.2 6.8 6.8 25.2"/></svg>`;

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Burning the midnight oil";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

function emptyMarkup() {
  if (!state.up) {
    return `<div class="empty">${SPARK}<h1>Ollama is offline</h1><p>Run <code>ollama serve</code> and reload.</p></div>`;
  }
  const hint = state.models.length
    ? ""
    : `<p>No models yet — open <code>Models</code> in the sidebar to pull one.</p>`;
  return `<div class="empty">${SPARK}<h1>${greeting()}</h1>${hint}</div>`;
}

function renderMessages() {
  const box = $("#messages");
  box.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "stream-wrap";
  const isEmpty = !state.chat || !state.chat.messages.length;

  $("#main").classList.toggle("is-empty", isEmpty);
  $("#chat-title").hidden = !state.chat;
  $("#delete-chat").hidden = !state.chat;

  if (isEmpty) {
    wrap.innerHTML = emptyMarkup();
  } else {
    for (const message of state.chat.messages)
      wrap.append(messageNode(message));
  }
  box.append(wrap);
  box.scrollTop = box.scrollHeight;
}

function scrollToBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

async function sendMessage(text) {
  if (state.streaming) return;
  if (!state.model) {
    toast("Pick a model first", true);
    openModels();
    return;
  }
  if (!state.chat) await newChat();

  const chatId = state.chat.id;
  state.chat.messages.push({ role: "user", content: text });
  $("#input").value = "";
  autoGrow();
  renderMessages();

  const wrap = $("#messages .stream-wrap");
  const node = messageNode({
    role: "assistant",
    content: "",
    model: state.model,
  });
  const bubble = node.querySelector(".bubble");
  bubble.innerHTML = '<span class="cursor"></span>';
  wrap.append(node);
  scrollToBottom();

  state.streaming = true;
  $("#send").disabled = true;
  let raw = "";
  let thinking = "";
  let errored = false;

  try {
    const res = await fetch(`api/chats/${chatId}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: state.model, content: text }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `send failed: ${res.status}`);
    }
    await readNdjson(res, (event) => {
      if (event.type === "token") {
        raw += event.content;
        bubble.innerHTML = renderMarkdown(raw) + '<span class="cursor"></span>';
        scrollToBottom();
      } else if (event.type === "thinking") {
        thinking += event.content;
        let details = node.querySelector(".thinking");
        if (!details) {
          details = thinkingNode("");
          node.insertBefore(details, bubble);
        }
        details.querySelector("div").textContent = thinking;
      } else if (event.type === "done") {
        if (event.chat) {
          state.chat = event.chat;
          if (!$("#chat-title").value)
            $("#chat-title").value = event.chat.title || "";
        }
      } else if (event.type === "error") {
        errored = true;
        node.classList.add("error");
        bubble.innerHTML = raw
          ? renderMarkdown(raw) +
            `<p><strong>Error:</strong> ${escapeHtml(event.error)}</p>`
          : `<strong>Error:</strong> ${escapeHtml(event.error)}`;
      }
    });
  } catch (err) {
    errored = true;
    node.classList.add("error");
    bubble.innerHTML = `<strong>Error:</strong> ${escapeHtml(err.message)}`;
    toast(err.message, true);
  } finally {
    state.streaming = false;
    $("#send").disabled = false;
    bubble.querySelectorAll(".cursor").forEach((c) => c.remove());
    try {
      state.chat = await api(`api/chats/${chatId}`);
    } catch (_) {
      state.chat = null;
    }
    $("#chat-title").value = (state.chat && state.chat.title) || "";
    await loadChats();
    if (!errored && state.chat) renderMessages();
    $("#input").focus();
  }
}


async function loadModels() {
  try {
    const data = await api("api/models");
    state.up = data.up;
    state.models = data.models;
    if (data.error) showModelsError(data.error);
  } catch (err) {
    showModelsError(err.message);
  }
  renderModels();
  renderModelOptions();
}

function showModelsError(message) {
  const node = $("#models-error");
  node.textContent = message;
  node.hidden = false;
  setTimeout(() => (node.hidden = true), 8000);
}

function renderModelOptions() {
  const select = $("#model");
  const current = state.model;
  select.innerHTML = "";
  if (!state.models.length) {
    state.model = "";
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "no models";
    select.append(option);
    return;
  }
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model.name;
    option.textContent = model.name;
    select.append(option);
  }
  const stillThere = state.models.some((m) => m.name === current);
  state.model = stillThere ? current : state.models[0].name;
  select.value = state.model;
}

function renderModels() {
  const list = $("#model-list");
  list.innerHTML = "";
  if (!state.models.length) {
    const item = document.createElement("li");
    item.className = "model-meta";
    item.textContent = "No local models yet - pull one above.";
    list.append(item);
    return;
  }
  for (const model of state.models) {
    const item = document.createElement("li");

    const head = document.createElement("div");
    head.className = "model-head";
    const name = document.createElement("span");
    name.className = "model-name";
    name.textContent = model.name;
    head.append(name);
    if (model.running) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "loaded";
      head.append(badge);
    }

    const actions = document.createElement("div");
    actions.className = "model-actions";
    actions.append(
      iconButton("Rename", "Rename model", () => renameModel(model.name)),
      iconButton("Delete", "Delete model", () => removeModel(model.name)),
    );
    head.append(actions);

    const meta = document.createElement("div");
    meta.className = "model-meta";
    meta.textContent = [
      formatSize(model.size),
      model.parameter_size,
      model.quantization_level,
      model.modified_at ? `updated ${model.modified_at.slice(0, 10)}` : "",
    ]
      .filter(Boolean)
      .join(" · ");

    item.append(head, meta);
    list.append(item);
  }
}

function openModels() {
  $("#models-dialog").showModal();
}

async function renameModel(source) {
  const destination = prompt(`Rename "${source}" to:`, source);
  if (!destination || destination.trim() === source) return;
  try {
    const data = await api("api/models/rename", {
      method: "POST",
      body: JSON.stringify({ source, destination: destination.trim() }),
    });
    if (data.ok === false) throw new Error(data.error);
    toast(`renamed to ${destination.trim()}`);
    if (state.model === source) state.model = destination.trim();
    await loadModels();
  } catch (err) {
    toast(err.message, true);
  }
}

async function removeModel(name) {
  if (!confirm(`Delete model "${name}"? This frees its files.`)) return;
  try {
    await api("api/models/delete", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    toast(`deleted ${name}`);
    if (state.model === name) state.model = "";
    await loadModels();
  } catch (err) {
    toast(err.message, true);
  }
}

async function pullModel(event) {
  event.preventDefault();
  const input = $("#pull-name");
  const name = input.value.trim();
  if (!name) return;

  const progress = $("#pull-progress");
  const bar = $("#pull-bar");
  const status = $("#pull-status");
  $("#pull-btn").disabled = true;
  progress.hidden = false;
  bar.style.width = "0%";
  status.textContent = "starting…";

  try {
    const res = await fetch("api/models/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `pull failed: ${res.status}`);
    }
    let pullError = null;
    await readNdjson(res, (event2) => {
      if (event2.type === "error") {
        pullError = event2.error;
        return;
      }
      if (event2.percent !== undefined) {
        bar.style.width = `${event2.percent}%`;
        status.textContent = `${event2.status} — ${event2.percent}%`;
      } else {
        status.textContent = event2.status;
      }
      if (event2.type === "done") {
        bar.style.width = "100%";
        status.textContent = "done";
        toast(`pulled ${name}`);
        input.value = "";
      }
    });
    if (pullError) throw new Error(pullError);
    await loadModels();
  } catch (err) {
    status.textContent = "failed";
    showModelsError(err.message);
    toast(err.message, true);
  } finally {
    $("#pull-btn").disabled = false;
    setTimeout(() => (progress.hidden = true), 1500);
  }
}


function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

function wireEvents() {
  $("#toggle-sidebar").addEventListener("click", () =>
    setSidebar($("#app").classList.contains("sidebar-closed")),
  );
  $("#new-chat").addEventListener("click", () => {
    newChat().catch((err) => toast(err.message, true));
    if (window.innerWidth <= 720) setSidebar(false);
  });
  $("#open-models").addEventListener("click", openModels);
  $("#close-models").addEventListener("click", () =>
    $("#models-dialog").close(),
  );
  $("#pull-form").addEventListener("submit", pullModel);
  $("#model").addEventListener("change", (event) => {
    state.model = event.target.value;
    localStorage.setItem("prep.model", state.model);
  });
  $("#delete-chat").addEventListener("click", async () => {
    if (!state.chat) return;
    if (!confirm("Delete this chat?")) return;
    try {
      await api(`api/chats/${state.chat.id}`, { method: "DELETE" });
      state.chat = null;
      renderMessages();
      await loadChats();
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("#chat-title").addEventListener("change", async (event) => {
    if (!state.chat) return;
    try {
      const chat = await api(`api/chats/${state.chat.id}/rename`, {
        method: "POST",
        body: JSON.stringify({ title: event.target.value }),
      });
      state.chat = chat;
      event.target.value = chat.title;
      await loadChats();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("#input").value.trim();
    if (!text || state.streaming) return;
    sendMessage(text).catch((err) => toast(err.message, true));
  });
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#input").addEventListener("input", autoGrow);
}

async function init() {
  initSidebar();
  wireEvents();
  await Promise.all([loadState(), loadChats(), loadModels()]);
  renderMessages();
  if (state.chats.length) await openChat(state.chats[0].id);
  $("#input").focus();
}

init().catch((err) => toast(err.message, true));
