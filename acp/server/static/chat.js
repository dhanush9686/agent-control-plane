/* Upload-and-chat page. Plain JS, no build step. Every piece of server text is inserted with textContent,
   never innerHTML, so a malicious document or answer cannot inject markup. */
"use strict";

const $ = (s) => document.querySelector(s);
let KEY = "";
let selected = null; // {id, filename}
let pollTimer = null;

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ""));
  return el;
}

try { KEY = localStorage.getItem("acp-key") || ""; } catch (_) { /* storage blocked: the key just won't persist */ }
if (KEY) $("#key").value = KEY;

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-API-Key": KEY, ...(opts.headers || {}) } });
  let body = null;
  if (res.status !== 204) { try { body = await res.json(); } catch (_) { body = null; } }
  if (!res.ok) {
    const msg = (body && (body.error || body.detail)) || `Request failed (${res.status})`;
    const err = new Error(typeof msg === "string" ? msg : "Request failed");
    err.status = res.status; err.body = body; err.retryAfter = res.headers.get("Retry-After");
    throw err;
  }
  return body;
}

async function refreshMe() {
  if (!KEY) { $("#me").textContent = "Enter your API key to begin."; return false; }  // don't call the API without a key
  try {
    const me = await api("/v1/me");
    $("#me").textContent = `Signed in as ${me.name}. Left today: ${me.remaining_today.asks} questions, ${me.remaining_today.uploads} uploads.`;
    return true;
  } catch (e) {
    $("#me").textContent = KEY ? "That key was not accepted." : "Enter your API key to begin.";
    return false;
  }
}

$("#key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  KEY = $("#key").value.trim();
  try { localStorage.setItem("acp-key", KEY); } catch (_) {}
  if (await refreshMe()) loadDocs();
});

/* ---------- documents ---------- */
function statusBadge(d) {
  const state = d.status;
  const cls = state === "ready" ? "ready" : state === "failed" ? "failed" : "work";
  return h("span", { class: "badge " + cls }, state === "ready" ? "ready" : state === "failed" ? "failed" : state);
}

function renderDocs(docs) {
  const ul = $("#docs");
  ul.replaceChildren(...docs.map((d) => {
    const li = h("li", { class: "doc" + (selected && selected.id === d.id ? " sel" : "") },
      h("div", {}, h("span", { class: "name" }, d.filename), " ", statusBadge(d),
        h("div", { class: "muted" }, d.status === "ready" ? `${d.pages} pages, ${d.chunks} sections` : d.status === "failed" ? d.error : (d.job && d.job.message) || "")),
      h("div", {},
        d.status === "ready" ? h("button", { class: "link", type: "button", onclick: () => select(d) }, "Ask") : null,
        h("button", { class: "danger", type: "button", "aria-label": "Delete " + d.filename, onclick: () => remove(d) }, "Delete")));
    if (d.status !== "ready" && d.status !== "failed") {
      li.append(h("progress", { max: "100", value: String(d.job ? d.job.percent : 0), "aria-label": "Processing progress" }));
    }
    return li;
  }));
  const working = docs.some((d) => d.status !== "ready" && d.status !== "failed");
  clearTimeout(pollTimer);
  if (working) pollTimer = setTimeout(loadDocs, 1000);
}

async function loadDocs() {
  try { renderDocs((await api("/v1/documents")).documents); } catch (e) { $("#upload-msg").textContent = e.message; }
}

function select(d) {
  selected = { id: d.id, filename: d.filename };
  $("#chat-doc").textContent = `Asking about: ${d.filename}`;
  $("#q").disabled = false; $("#ask-btn").disabled = false; $("#q").focus();
  loadDocs();
}

async function remove(d) {
  if (!confirm(`Delete "${d.filename}" and everything derived from it?`)) return;
  try {
    await api(`/v1/documents/${d.id}`, { method: "DELETE" });
    if (selected && selected.id === d.id) { selected = null; $("#q").disabled = true; $("#ask-btn").disabled = true; $("#chat-doc").textContent = "Select a ready document above."; }
    loadDocs(); refreshMe();
  } catch (e) { $("#upload-msg").textContent = e.message; }
}

/* ---------- upload ---------- */
const drop = $("#drop"), fileInput = $("#file");
drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ""; });

async function upload(file) {
  const msg = $("#upload-msg");
  if (!KEY) { msg.textContent = "Save your API key first."; return; }
  msg.textContent = `Uploading ${file.name}...`;
  const form = new FormData(); form.append("file", file);
  try {
    const d = await api("/v1/documents", { method: "POST", body: form });
    msg.textContent = d.duplicate ? "You already uploaded this file." : "Uploaded. Processing...";
    loadDocs(); refreshMe();
  } catch (e) {
    msg.textContent = e.status === 429 ? `${e.message} Try again in ${e.retryAfter || "a while"} seconds.` : e.message;
  }
}

/* ---------- chat ---------- */
function addMsg(cls, ...kids) { const el = h("div", { class: "msg " + cls }, ...kids); $("#log").append(el); el.scrollIntoView({ block: "end" }); return el; }

$("#ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("#q").value.trim();
  if (!q || !selected) return;
  $("#q").value = ""; $("#ask-btn").disabled = true;
  addMsg("user", q);
  const pending = addMsg("bot", "Thinking...");
  try {
    const a = await api(`/v1/documents/${selected.id}/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
    pending.replaceChildren(a.answer);
    if (a.refused) pending.append(h("div", { class: "meta" }, h("span", { class: "badge failed" }, "Not in the document")));
    if (a.citations.length) {
      const chips = h("div", { class: "chips" });
      const snip = h("div", {});
      a.citations.forEach((c) => chips.append(h("button", { class: "chip", type: "button", "aria-label": `Show source ${c.label}`,
        onclick: () => snip.replaceChildren(h("div", { class: "snippet" }, (c.section ? c.section + " | " : "") + c.label + "\n" + c.snippet)) }, c.label)));
      pending.append(chips, snip);
    }
    const meta = h("div", { class: "meta" }, a.cached ? "from cache" : "", `${a.asks_remaining_today} questions left today`);
    if (a.trace_id) meta.append(h("button", { class: "link", type: "button", onclick: () => showTrace(a.trace_id) }, "view trace"));
    pending.append(meta);
    refreshMe();
  } catch (e) {
    pending.className = "msg err";
    pending.textContent = e.status === 429 ? `${e.message} Try again in ${e.retryAfter || "a little while"} seconds.` : e.message;
  } finally { $("#ask-btn").disabled = false; }
});

async function showTrace(id) {
  const body = $("#trace-body");
  body.textContent = "Loading...";
  $("#trace-dialog").showModal();
  try {
    const t = await api(`/v1/traces/${id}`);
    body.replaceChildren(
      h("p", { class: "muted" }, `${Math.round(t.run.latency_ms)} ms, ${t.run.input_tokens}+${t.run.output_tokens} tokens, ${t.run.llm_calls} model calls`),
      h("table", {}, h("thead", {}, h("tr", {}, ["Step", "ms", "Tokens in/out", "Detail"].map((x) => h("th", {}, x)))),
        h("tbody", {}, t.spans.map((s) => h("tr", {}, h("td", {}, s.name), h("td", {}, String(Math.round(s.latency_ms))),
          h("td", {}, s.input_tokens != null ? `${s.input_tokens}/${s.output_tokens}` : ""),
          h("td", {}, s.attributes.model || s.attributes.reasoning || (s.attributes.query ? "query: " + s.attributes.query : "")))))));
  } catch (e) { body.textContent = e.message; }
}

refreshMe().then((ok) => { if (ok) loadDocs(); });
