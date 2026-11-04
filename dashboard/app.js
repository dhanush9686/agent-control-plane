/* Agent Control Plane dashboard: vanilla JS, no build step.
   Everything is rendered with textContent (via h()) so data can never inject HTML. */
"use strict";

const REPO_URL = ""; // filled in at release time (see README)
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
const badge = (ok) => h("span", { class: "badge " + (ok ? "pass" : "fail") }, ok ? "✓ pass" : "✕ fail");

let DATA = null;
let chart = null;

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
  if (location.hash.startsWith("#overview") || !location.hash) render();
});

/* ---------- views ---------- */
function kpi(label, value, hint) {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), h("div", { class: "hint" }, hint));
}

function overview() {
  const s = DATA.eval.summary;
  const cats = Object.entries(s.per_category);
  const view = h("div", {},
    h("div", { class: "kpis" },
      kpi("Overall pass rate", pct(s.pass_rate), `${s.n_cases} golden questions`),
      kpi("Answered correctly", pct(s.answered_correctly_rate), "of answerable questions"),
      kpi("Hallucination rate", pct(s.hallucination_rate), "answers with ≥1 unsupported claim"),
      kpi("Refusal correctness", pct(s.refusal_correctness), "answered vs refused as expected"),
      kpi("Avg latency", ms(s.avg_latency_ms), "includes free-tier rate-limit waits"),
      kpi("Avg cost / question", usd(s.avg_cost_usd), "estimated from token counts"),
    ),
    h("div", { class: "grid2" },
      h("section", { class: "card" }, h("h2", {}, "Pass rate by category"),
        h("div", { class: "chart-wrap" }, h("canvas", { id: "cat-chart", role: "img", "aria-label": "Bar chart of pass rate per question category; same numbers are in the table." }))),
      h("section", { class: "card" }, h("h2", {}, "Per-category scores"),
        h("table", {}, h("caption", {}, "Evaluation " + DATA.eval.eval_id + " · " + DATA.eval.created_at),
          h("thead", {}, h("tr", {}, h("th", {}, "Category"), h("th", { class: "num" }, "n"), h("th", { class: "num" }, "Pass"), h("th", { class: "num" }, "Halluc."))),
          h("tbody", {}, cats.map(([c, v]) => h("tr", {}, h("td", {}, c.replace(/_/g, " ")), h("td", { class: "num" }, v.n), h("td", { class: "num" }, pct(v.pass_rate)), h("td", { class: "num" }, v.hallucinations))))))),
  );
  queueMicrotask(() => drawChart(cats));
  return view;
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function drawChart(cats) {
  const canvas = $("#cat-chart");
  if (!canvas) return;
  if (chart) { chart.destroy(); chart = null; }
  if (typeof Chart === "undefined") { canvas.replaceWith(h("p", { class: "muted" }, "Chart library unavailable; see the table.")); return; }
  chart = new Chart(canvas, {
    type: "bar",
    data: { labels: cats.map(([c]) => c.replace(/_/g, " ")), datasets: [{ label: "Pass rate", data: cats.map(([, v]) => (v.pass_rate ?? 0) * 100), backgroundColor: cssVar("--accent"), borderRadius: 6 }] },
    options: {
      maintainAspectRatio: false, animation: false,
      scales: { y: { min: 0, max: 100, ticks: { color: cssVar("--muted"), callback: (v) => v + "%" }, grid: { color: cssVar("--border") } }, x: { ticks: { color: cssVar("--muted") }, grid: { display: false } } },
      plugins: { legend: { display: false } },
    },
  });
}

function runsView(selected) {
  const runs = DATA.runs;
  const resultFor = Object.fromEntries(DATA.eval.results.map((r) => [r.run_id, r]));
  const cur = runs.find((r) => r.run_id === selected) || runs[0];
  const list = h("ul", { class: "run-list", "aria-label": "Runs" }, runs.map((r) => {
    const res = resultFor[r.run_id];
    return h("li", {}, h("a", { href: "#runs/" + r.run_id, "aria-current": r === cur ? "true" : "false" },
      h("span", {}, badge(res?.passed), " ", h("span", { class: "badge cat" }, r.category.replace(/_/g, " "))),
      h("span", { class: "q" }, r.question), h("span", { class: "muted mono" }, `${r.case_id} · ${ms(r.latency_ms)}`)));
  }));
  return h("div", { class: "runs" }, h("section", { class: "card" }, h("h2", {}, "Runs"), list), runDetail(cur, resultFor[cur.run_id]));
}

function runDetail(r, res) {
  const total = Math.max(r.latency_ms, 1);
  const spans = r.spans;
  const rows = spans.flatMap((s) => {
    const left = (s.start_ms / total) * 100, width = (s.latency_ms / total) * 100;
    const cls = s.status === "error" ? "err" : s.kind;
    const label = `${s.name}: ${ms(s.latency_ms)}` + (s.input_tokens != null ? `, ${s.input_tokens} in / ${s.output_tokens} out tokens` : "");
    return [
      h("div", { class: "name mono", title: s.name }, (s.kind === "llm" ? "↳ " : "") + s.name),
      h("div", { class: "track", role: "img", "aria-label": label }, h("div", { class: "bar " + cls, style: `left:${left.toFixed(2)}%;width:${Math.max(width, 0.4).toFixed(2)}%` })),
      h("div", { class: "ms" }, ms(s.latency_ms)),
    ];
  });
  const llm = spans.filter((s) => s.kind === "llm");
  return h("section", { class: "card" },
    h("h2", {}, r.question),
    h("div", { class: "meta" }, badge(res?.passed), h("span", {}, "Run ", h("code", {}, r.run_id)), h("span", {}, ms(r.latency_ms)),
      h("span", {}, `${r.input_tokens}+${r.output_tokens} tokens`), h("span", {}, usd(r.cost_usd)), h("span", {}, `${r.llm_calls} LLM calls`),
      h("span", {}, `${r.retries} rate-limit retries`), h("span", {}, `${r.attempts} search attempt(s)`)),
    h("div", { class: "answer" }, r.answer),
    h("h3", {}, "Trace waterfall"),
    h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:var(--node)" }), "graph node"), h("span", {}, h("i", { style: "background:var(--llm)" }), "LLM call"), h("span", {}, h("i", { style: "background:var(--run)" }), "whole run")),
    h("div", { class: "wf" }, rows),
    h("h3", {}, "Spans"),
    h("table", {}, h("caption", {}, "Per-step detail"), h("thead", {}, h("tr", {}, ["Span", "Latency", "In", "Out", "Cost", "Retries", "Detail"].map((t, i) => h("th", { class: i > 0 && i < 6 ? "num" : "" }, t)))),
      h("tbody", {}, spans.map((s) => h("tr", {}, h("td", { class: "mono" }, s.name), h("td", { class: "num" }, ms(s.latency_ms)), h("td", { class: "num" }, s.input_tokens ?? "–"), h("td", { class: "num" }, s.output_tokens ?? "–"), h("td", { class: "num" }, s.cost_usd != null ? usd(s.cost_usd) : "–"), h("td", { class: "num" }, s.retries ?? "–"), h("td", {}, spanDetail(s)))))),
    llm.length ? null : h("p", { class: "muted" }, "No LLM spans recorded."),
  );
}

function spanDetail(s) {
  const a = s.attributes || {};
  if (s.name === "node.judge") return `sufficient=${a.sufficient}: ${a.reasoning ?? ""}`;
  if (s.name === "node.retrieve") return `query “${a.query}” → ${a.new_chunks} new chunk(s)`;
  if (s.name === "node.refine_query") return `new query “${a.new_query}”`;
  return "";
}

function failuresView() {
  const bad = DATA.eval.results.filter((r) => !r.passed);
  if (!bad.length) return h("div", { class: "card empty" }, "No failing questions in this evaluation. 🎉");
  const runById = Object.fromEntries(DATA.runs.map((r) => [r.run_id, r]));
  return h("div", {}, h("p", { class: "muted" }, `${bad.length} of ${DATA.eval.results.length} questions failed. Each card shows what was asked, what was retrieved, what the agent said, and which claim the judge could not support.`),
    bad.map((r) => {
      const run = runById[r.run_id];
      return h("article", { class: "card fail-card" },
        h("h2", {}, r.question),
        h("div", { class: "meta" }, h("span", { class: "badge cat" }, r.category.replace(/_/g, " ")), h("span", {}, "expected: " + r.expect), h("code", {}, r.case_id),
          run ? h("a", { href: "#runs/" + run.run_id }, "open trace →") : null),
        h("h3", {}, "Why it failed"), h("ul", {}, r.failures.map((f) => h("li", {}, f))),
        h("h3", {}, "Answer"), h("div", { class: "answer" }, r.answer),
        h("h3", {}, "Claims checked against retrieved chunks"),
        r.claims.length ? r.claims.map((c) => h("div", { class: "claim " + (c.supported ? "ok" : "bad") }, h("span", { class: "tag" }, c.supported ? "✓ supported" : "✕ NOT supported"), c.claim, c.evidence ? h("q", {}, "evidence: “" + c.evidence + "”") : null)) : h("p", { class: "muted" }, "No factual claims extracted."),
        h("details", {}, h("summary", {}, `Retrieved chunks (${run ? run.retrieved.length : 0})`), (run?.retrieved || []).map((c) => h("div", { class: "chunk" }, h("b", {}, c.source), c.text))));
    }));
}

/* ---------- router ---------- */
function render() {
  if (!DATA) return;
  const [view, arg] = (location.hash.slice(1) || "overview").split("/");
  const name = ["overview", "runs", "failures"].includes(view) ? view : "overview";
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  const main = $("#main");
  main.replaceChildren(name === "runs" ? runsView(arg) : name === "failures" ? failuresView() : overview());
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
window.addEventListener("hashchange", render);

async function init() {
  if (REPO_URL) { const a = $("#repo-link"); a.href = REPO_URL; a.hidden = false; }
  try {
    const res = await fetch("data/traces.json", { cache: "no-cache" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    DATA = await res.json();
  } catch (e) {
    $("#main").replaceChildren(h("div", { class: "card empty" }, "Could not load data/traces.json (" + e.message + "). Run `make export`, and serve this folder over HTTP."));
    $("#subtitle").textContent = "No data";
    return;
  }
  const s = DATA.eval.summary;
  $("#subtitle").textContent = `${DATA.model} · ${s.n_cases} golden questions · exported ${DATA.generated_at.slice(0, 10)}`;
  render();
}
window.addEventListener("load", init); // after deferred Chart.js has executed
