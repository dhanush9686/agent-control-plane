/* Assurance dashboard: vanilla JS, no build step, no third-party scripts.
   Everything is rendered with textContent (via h()) so data can never inject HTML. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ""));
  return el;
}
const pct = (v) => (v == null ? "n/a" : (v * 100).toFixed(1) + "%");
const ms = (v) => (v == null ? "n/a" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
const usd = (v) => (v == null ? "n/a" : "$" + v.toFixed(5));
const rupees = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let A = null; // the saved assurance data: {real_run, replays, audit_demo}
let LIVE = null; // live data from Redis (or the bundled copy): {via, latest, runs, history, snapshot, audit}
let onlyInteresting = true;

/* ---------- theme ---------- */
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
}
try { applyTheme(localStorage.getItem("acp-theme")); } catch (_) { /* storage blocked: fine */ }
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("acp-theme", next); } catch (_) {}
});

/* ---------- shared pieces ---------- */
function kpi(label, value, hint) {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), h("div", { class: "hint" }, hint));
}

const KIND_LABEL = {
  cross_merchant: "Another merchant's data reached the model",
  unauthorized_refund: "Refund nobody asked for",
  unapproved_execution: "Money moved without approval",
  pii_shown: "Card / ID data shown",
  unsupported_amount: "Figure not in any ledger record",
};

/* ---------- results ---------- */
function resultsView() {
  const R = A.real_run, g = R.summary.guarded, u = R.summary.unguarded;
  const nScen = g.n / R.repeats;
  const cell = (r) => `${r.k} / ${r.n}`;
  const kinds = Object.keys(g.by_kind);
  return h("div", {},
    h("section", { class: "card", style: "margin-bottom:16px" },
      h("h2", {}, "Assurance layer for agents that touch money"),
      h("p", {}, "A payments-ops agent calls tools against a synthetic ledger. Between the model and the ledger sits a policy engine (tenant scope, refund provenance and limits, human approval), card/ID masking, a check that every rupee figure came from the ledger, and a tamper-evident audit log. Every scenario is run twice with the same model, tools and prompt: guarded and unguarded. The gap is what enforcement is worth."),
      h("p", { class: "muted small" }, "Synthetic data; not affiliated with or used by any payment company. The unguarded mode is a deliberately unprotected counterfactual. Repeats of a scenario are not independent, so the intervals are optimistic.")),
    h("div", { class: "kpis" },
      kpi("Critical outcomes, guarded", cell(g.critical_effective), `95% CI ${pct(g.critical_effective.ci95[0])} – ${pct(g.critical_effective.ci95[1])}`),
      kpi("Critical outcomes, unguarded", cell(u.critical_effective), `${pct(u.critical_effective.rate)} of runs · CI ${pct(u.critical_effective.ci95[0])} – ${pct(u.critical_effective.ci95[1])}`),
      kpi("Scenarios passed, guarded", pct(g.pass.rate), `unguarded ${pct(u.pass.rate)} · ${nScen} scenarios × ${R.repeats}`),
      kpi("Model asked for a violation", pct(g.critical_attempted.rate), "of guarded runs; the guards stopped all of them"),
      kpi("Cost / latency per task", usd(g.avg_cost_usd), ms(g.avg_latency_ms) + " · " + R.model.replace("apac.", "")),
    ),
    h("div", { class: "grid2" },
      h("section", { class: "card" }, h("h2", {}, "What the model tried vs what happened"),
        h("table", {}, h("caption", {}, "Counts of runs (attempted = the model asked; effective = it happened or reached the user)"),
          h("thead", {}, h("tr", {}, h("th", {}, "Violation"), h("th", { class: "num" }, "Guarded att./eff."), h("th", { class: "num" }, "Unguarded att./eff."))),
          h("tbody", {}, kinds.map((k) => h("tr", {}, h("td", {}, KIND_LABEL[k] || k), h("td", { class: "num" }, `${g.by_kind[k].attempted} / ${g.by_kind[k].effective}`), h("td", { class: "num" }, `${u.by_kind[k].attempted} / ${u.by_kind[k].effective}`)))))),
      h("section", { class: "card" }, h("h2", {}, "Pass by category"),
        h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Category"), h("th", { class: "num" }, "Guarded"), h("th", { class: "num" }, "Unguarded"))),
          h("tbody", {}, Object.entries(g.per_category).map(([c, v]) => h("tr", {}, h("td", {}, c.replace(/_/g, " ")), h("td", { class: "num" }, `${v.passed}/${v.n}`), h("td", { class: "num" }, `${u.per_category[c].passed}/${u.per_category[c].n}`))))))),
    h("section", { class: "card", style: "margin:16px 0" },
      h("h2", {}, "Guarded failures, not hidden"),
      g.failures.length ? h("ul", {}, g.failures.map((f) => h("li", {}, h("code", {}, f.id), ": " + f.why.join("; ")))) : h("p", {}, "None."),
      R.notes ? h("p", { class: "muted small" }, R.notes) : null));
}

/* ---------- attack replay ---------- */
function stepRow(s) {
  const args = Object.entries(s.args).map(([k, v]) => k + "=" + v).join(", ");
  return h("li", { class: "step " + (s.allowed ? "allowed" : "denied") },
    h("code", {}, s.tool + "(" + args + ")"),
    h("span", { class: "badge " + (s.allowed ? "pass" : "fail") }, s.allowed ? "ran" : "blocked: " + s.code),
    s.effect === "executed" ? h("span", { class: "badge fail" }, "MONEY MOVED") : null,
    s.effect === "draft" ? h("span", { class: "badge cat" }, "draft: needs human approval") : null,
    !s.allowed && s.reason ? h("div", { class: "muted small" }, s.reason) : null);
}

function replayColumn(title, r) {
  const shown = r.user_sees !== r.model_said;
  return h("section", { class: "card replay " + (r.ok ? "ok" : "bad") },
    h("h3", {}, title),
    h("div", { class: "label" }, "Tool calls the scripted model made"),
    h("ol", { class: "steps" }, r.steps.map(stepRow)),
    h("div", { class: "label" }, "What the model said"), h("div", { class: "answer" }, r.model_said),
    shown ? h("div", { class: "label" }, r.withheld ? "What the user sees (answer withheld)" : "What the user sees (after masking)") : null,
    shown ? h("div", { class: "answer" }, r.user_sees) : null,
    r.refunds_created.length ? h("div", { class: "label" }, "Ledger") : null,
    ...r.refunds_created.map((x) => h("div", { class: "small" }, `${x.id}: ${rupees(x.amount_paise)} on ${x.payment_id}, status ${x.status}`)),
    h("div", { class: "outcome " + (r.ok ? "ok" : "bad") }, r.ok ? "✓ No critical outcome" : "✕ " + r.bad_outcomes.join(" ")));
}

function replayView(sel) {
  const cases = A.replays;
  const c = cases.find((x) => x.id === sel) || cases[0];
  return h("section", { class: "card" },
    h("h2", {}, "Sandbox: replay an attack"),
    h("p", { class: "muted" }, "A deterministic scripted attacker stands in for a misbehaving model and is run through the real policy engine, masking, numeric check and ledger, once with the guards off and once on. It shows what enforcement changes; it is not a model and says nothing about how often a real model misbehaves (the live results measure that). Data is synthetic."),
    h("div", { class: "pills", role: "group", "aria-label": "Choose an attack" },
      cases.map((x) => h("a", { class: "pill", href: "#replay/" + x.id, "aria-current": x.id === c.id ? "true" : null }, x.title))),
    h("p", {}, h("b", {}, "Scenario: "), c.story),
    h("p", { class: "small" }, h("b", {}, "User asked: "), h("q", {}, c.prompt)),
    h("div", { class: "grid2e" }, replayColumn("Without guards (baseline)", c.unguarded), replayColumn("With guards (real configuration)", c.guarded)));
}

/* ---------- audit log ---------- */
function auditView() {
  const a = A.audit_demo;
  return h("section", { class: "card" },
    h("h2", {}, "Tamper-evident audit log"),
    h("p", { class: "muted" }, "Every run and tool call is appended to a hash chain: each row's hash covers the previous row's hash. This chain was written by replaying the injection attack with guards on, then verified, then one row was edited."),
    h("table", {}, h("thead", {}, h("tr", {}, ["#", "Action", "Hash", "Previous hash"].map((t) => h("th", {}, t)))),
      h("tbody", {}, a.rows.map((r) => h("tr", { class: r.seq === a.edited_seq ? "edited" : null }, h("td", {}, r.seq), h("td", {}, r.action), h("td", { class: "mono" }, r.hash + "…"), h("td", { class: "mono" }, r.prev + "…"))))),
    h("p", {}, h("span", { class: "badge pass" }, "✓ intact"), ` verified ${a.before.rows} rows before any change.`),
    h("p", {}, h("span", { class: "badge fail" }, "✕ broken"), ` after editing row ${a.edited_seq}: ${a.after.reason} (first bad row ${a.after.first_bad_seq}).`),
    h("p", { class: "muted small" }, "Tamper-evident, not tamper-proof: someone who can rewrite the whole table can rebuild a consistent chain. The production step is to anchor the latest hash somewhere the database admin cannot write."));
}


/* ---------- live ---------- */
const SOURCE_LABEL = { "razorpay-test": "Razorpay test mode", synthetic: "Synthetic sandbox" };
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ""));
  return el;
}
function ageText(iso) {
  const m = Math.max(0, (Date.now() - Date.parse(iso)) / 60000);
  if (!isFinite(m)) return "unknown time";
  if (m < 60) return Math.max(1, Math.round(m)) + " min ago";
  if (m < 2880) return Math.round(m / 60) + " h ago";
  return Math.round(m / 1440) + " days ago";
}
function freshness(latest, via) {
  if (via === "bundle") return { cls: "warn", text: "Bundled snapshot, not live" };
  const hours = (Date.now() - Date.parse(latest.finished_at)) / 3600000;
  const every = latest.expected_interval_hours || 168;
  if (hours <= every * 1.25) return { cls: "pass", text: "Fresh" };
  if (hours <= every * 2) return { cls: "warn", text: "Getting stale" };
  return { cls: "fail", text: "Stale: a sync is overdue" };
}

function trendChart(history, src) {
  const pts = history.slice().reverse().map((h0) => ({ t: h0.finished_at, s: h0.sources && h0.sources[src] })).filter((p) => p.s);
  if (pts.length < 2) return h("p", { class: "muted" }, "The trend appears after the second sync (" + pts.length + " so far).");
  const W = 640, H = 190, L = 40, T = 12, B = 28, iw = W - L - 12, ih = H - T - B;
  const x = (i) => L + (pts.length === 1 ? iw / 2 : (i * iw) / (pts.length - 1));
  const y = (r) => T + ih - r * ih;
  const line = (key, cls) => {
    const ok = pts.map((p, i) => [i, p.s[key]]).filter(([, v]) => v != null);
    return [svg("polyline", { class: "ln " + cls, fill: "none", points: ok.map(([i, v]) => x(i) + "," + y(v)).join(" ") }),
      ...ok.map(([i, v]) => svg("circle", { class: "pt " + cls, cx: x(i), cy: y(v), r: 4 }, svg("title", {}, `${pts[i].t.slice(0, 16).replace("T", " ")}: ${pct(v)}`)))];
  };
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "trend", role: "img", "aria-label": "Pass rate per sync, guarded and unguarded" },
    [0, 0.5, 1].map((r) => svg("g", {}, svg("line", { class: "grid", x1: L, x2: W - 12, y1: y(r), y2: y(r) }), svg("text", { class: "axis", x: L - 6, y: y(r) + 4, "text-anchor": "end" }, Math.round(r * 100) + "%"))),
    line("unguarded_pass", "u"), line("guarded_pass", "g"),
    svg("text", { class: "axis", x: L, y: H - 8 }, pts[0].t.slice(0, 10)), svg("text", { class: "axis", x: W - 12, y: H - 8, "text-anchor": "end" }, pts[pts.length - 1].t.slice(0, 10)));
}

function runItem(r) {
  const blocked = r.attempted.filter((k) => !r.effective.includes(k));
  const happened = r.effective;
  return h("details", { class: "run " + (r.passed ? "ok" : "bad") },
    h("summary", {},
      h("span", { class: "muted small" }, ageText(r.started_at)),
      h("span", { class: "badge " + (r.mode === "guarded" ? "cat" : "warn") }, r.mode),
      h("code", {}, r.scenario_id),
      h("span", { class: "badge " + (r.passed ? "pass" : "fail") }, r.passed ? "pass" : "fail"),
      ...blocked.map((k) => h("span", { class: "chip ok" }, "blocked: " + (KIND_LABEL[k] || k))),
      ...happened.map((k) => h("span", { class: "chip bad" }, "HAPPENED: " + (KIND_LABEL[k] || k))),
      r.withheld ? h("span", { class: "chip ok" }, "answer withheld") : null),
    h("div", { class: "label" }, "Tool calls"),
    r.events.length ? h("ol", { class: "steps" }, r.events.map(stepRow)) : h("p", { class: "muted small" }, "No tool calls."),
    h("div", { class: "label" }, "Answer shown to the user (masked)"), h("div", { class: "answer" }, r.answer || "(none)"),
    r.failed_checks.length ? h("div", { class: "small" }, h("b", {}, "Why it failed: "), r.failed_checks.join("; ")) : null,
    r.pii_found.length ? h("div", { class: "small" }, "Card or ID data in the model's text: " + r.pii_found.join(", ") + " (masked).") : null,
    h("div", { class: "muted small" }, `${r.tool_calls} tool calls, ${r.denied_calls} denied · ${ms(r.latency_ms)} · ${usd(r.cost_usd)}`));
}

function liveAudit() {
  const a = LIVE.audit;
  if (!a) return null;
  return h("section", { class: "card", style: "margin-top:16px" },
    h("h2", {}, "Audit chain of this sync"),
    h("p", {}, h("span", { class: "badge " + (a.chain_ok ? "pass" : "fail") }, a.chain_ok ? "✓ intact" : "✕ broken"),
      ` ${a.total_rows} entries verified ${ageText(a.verified_at)}. Head hash `, h("code", {}, (a.head || "").slice(0, 16) + "…")),
    a.chain_ok ? null : h("p", { class: "muted" }, a.reason),
    h("details", {}, h("summary", {}, `Last ${a.rows.length} entries`),
      h("table", {}, h("thead", {}, h("tr", {}, ["#", "Action", "Run", "Hash", "Previous"].map((t) => h("th", {}, t)))),
        h("tbody", {}, a.rows.map((r) => h("tr", {}, h("td", {}, r.seq), h("td", {}, r.action), h("td", { class: "mono" }, r.run_id || ""), h("td", { class: "mono" }, r.hash + "…"), h("td", { class: "mono" }, r.prev + "…")))))));
}

function liveView(arg) {
  if (!LIVE || !LIVE.latest) {
    return h("div", { class: "card empty" }, LIVE && LIVE.error ? LIVE.error : "No live data yet. Run `make live-sync` (see docs/LIVE_DATA.md); the first sync appears here without a redeploy.");
  }
  const L = LIVE.latest;
  const sources = Object.keys(L.sources);
  const src = sources.includes(arg) ? arg : sources[0];
  const S = L.sources[src].summary, g = S.guarded, u = S.unguarded;
  const fr = freshness(L, LIVE.via);
  const snap = LIVE.snapshot || L.snapshot;
  const runs = (LIVE.runs || []).filter((r) => r.source === src && (!onlyInteresting || r.attempted.length || !r.passed || r.withheld));
  return h("div", {},
    h("section", { class: "card", style: "margin-bottom:16px" },
      h("div", { class: "livehead" },
        h("h2", { style: "margin:0" }, "Live results"),
        h("span", { class: "badge " + fr.cls }, fr.text)),
      h("p", {}, `Last sync ${ageText(L.finished_at)} (${L.finished_at.slice(0, 16).replace("T", " ")} UTC) · model ${L.model.replace("apac.", "")} · cost ${usd(L.cost_usd)}`,
        L.aborted ? " · stopped early at the spend cap" : ""),
      h("ul", { class: "small provenance" }, (L.provenance || []).map((t) => h("li", {}, t)))),
    h("div", { class: "pills", role: "group", "aria-label": "Choose a data source" },
      sources.map((k) => h("a", { class: "pill", href: "#live/" + k, "aria-current": k === src ? "true" : null }, SOURCE_LABEL[k] || k))),
    snap && src === "razorpay-test" ? h("section", { class: "card small", style: "margin-bottom:16px" },
      h("b", {}, "Source snapshot: "),
      `${snap.payments} test payments (${Object.entries(snap.payments_by_tenant || {}).map(([k, v]) => k + ": " + v).join(", ")}), ${snap.refunds} refunds. `,
      `Settlements ${snap.settlements_available ? "returned by the API" : "not available in test mode (settlement scenarios run on the synthetic source)"}. `,
      "Snapshot ", h("code", {}, (snap.sha256 || "").slice(0, 12)), " taken " + ageText(snap.fetched_at) + ". Payment contents are not published.") : null,
    h("div", { class: "kpis" },
      kpi("Critical outcomes, guarded", `${g.critical_effective.k} / ${g.critical_effective.n}`, `95% CI ${pct(g.critical_effective.ci95[0])} – ${pct(g.critical_effective.ci95[1])}`),
      kpi("Critical outcomes, unguarded", u ? `${u.critical_effective.k} / ${u.critical_effective.n}` : "n/a", u ? `${pct(u.critical_effective.rate)} of runs` : ""),
      kpi("Scenarios passed, guarded", pct(g.pass.rate), u ? `unguarded ${pct(u.pass.rate)}` : ""),
      kpi("Model asked for a violation", pct(g.critical_attempted.rate), "of guarded runs"),
      kpi("Latency per task", ms(g.avg_latency_ms), `${g.n} guarded runs this sync`)),
    h("section", { class: "card", style: "margin-bottom:16px" },
      h("h2", {}, "Pass rate per sync"),
      h("div", { class: "legend" }, h("span", {}, h("i", { class: "key g" }), "guarded"), h("span", {}, h("i", { class: "key u" }), "unguarded")),
      trendChart(LIVE.history || [], src)),
    h("section", { class: "card" },
      h("h2", {}, "Recent runs"),
      h("label", { class: "small" }, h("input", { type: "checkbox", checked: onlyInteresting ? true : null, onchange: (e) => { onlyInteresting = e.target.checked; render(); } }),
        " Only runs where something was attempted, withheld or failed"),
      runs.length ? runs.slice(0, 60).map(runItem) : h("p", { class: "muted" }, "Nothing to show for this filter.")),
    liveAudit());
}

/* ---------- router ---------- */
function render() {
  if (!A) return;
  const landing = LIVE && LIVE.latest ? "live" : "results";
  const [view, arg] = (location.hash.slice(1) || landing).split("/");
  const name = ["live", "results", "replay", "audit"].includes(view) ? view : landing;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  const R = A.real_run;
  $("#subtitle").textContent =
    name === "live" && LIVE && LIVE.latest
      ? `Live · ${LIVE.latest.model.replace("apac.", "")} · periodic capture of test-mode data`
      : `Payments-agent assurance · ${R.model.replace("apac.", "")} · ${R.summary.guarded.n / R.repeats} scenarios × ${R.repeats}`;
  const views = { live: () => liveView(arg), results: resultsView, replay: () => replayView(arg), audit: auditView };
  $("#main").replaceChildren(views[name]());
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
window.addEventListener("hashchange", render);

/* Data comes through the login-protected /api/data function when deployed, or from data/ when this folder is served locally. */
let VIA_API = false;
async function loadData(name) {
  try {
    const r = await fetch("/api/data?file=" + name, { cache: "no-store", credentials: "same-origin" });
    if (r.status === 401) { location.replace("login.html"); return new Promise(() => {}); }
    if (r.ok && (r.headers.get("content-type") || "").includes("json")) { VIA_API = true; return await r.json(); }
  } catch (_) { /* no function here: local static server */ }
  const r2 = await fetch("data/" + name + ".json", { cache: "no-cache" });
  if (!r2.ok) throw new Error("HTTP " + r2.status);
  return r2.json();
}

/* Live data is optional: a missing or unreachable store just means the saved views are shown. */
async function loadLive() {
  try {
    const r = await fetch("/api/data?file=live", { cache: "no-store", credentials: "same-origin" });
    if (r.status === 401) { location.replace("login.html"); return new Promise(() => {}); }
    if (r.ok) { VIA_API = true; return await r.json(); }
    if (r.status === 502) return { error: "The live data store could not be reached. Showing saved results." };
    if (r.status !== 404) return null;
  } catch (_) { /* no function here: local static server */ }
  try {
    const r2 = await fetch("data/live.json", { cache: "no-cache" });
    return r2.ok ? { via: "bundle", ...(await r2.json()) } : null;
  } catch (_) { return null; }
}

async function init() {
  try {
    A = await loadData("assure");
    LIVE = await loadLive();
  } catch (e) {
    $("#main").replaceChildren(h("div", { class: "card empty" }, "Could not load the dashboard data (" + e.message + "). Locally, run `make dashboard-build` and serve this folder over HTTP."));
    $("#subtitle").textContent = "No data";
    return;
  }
  if (VIA_API) $("#logout").hidden = false;
  render();
}
$("#logout").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); } catch (_) { /* leaving anyway */ }
  location.replace("login.html");
});
window.addEventListener("load", init);
