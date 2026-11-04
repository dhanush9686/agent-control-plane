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

let DATA = null; // currently selected evaluation view: {eval, runs, ...}
let ALL = null;
const EXTRA = { history: null, comparison: null, judge: null }; // optional side files; views handle absence
let trendChart = null;
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
  render(); // redraw so charts pick up the new theme colors
});

/* ---------- views ---------- */
function kpi(label, value, hint) {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), h("div", { class: "hint" }, hint));
}

function ci(s, metric, fallback) {
  const c = s.ci95 && s.ci95[metric];
  return c ? `95% CI ${pct(c[0])} – ${pct(c[1])} · n=${s.counts[metric].n}` : fallback;
}

function judgeCard() {
  const j = EXTRA.judge && EXTRA.judge.reports;
  if (!j || !j.length) return null;
  return h("section", { class: "card", style: "margin-bottom:16px" }, h("h2", {}, "Grader accuracy on labelled answers"),
    h("p", { class: "muted" }, "How often the grounding judge agrees with answers whose correctness we constructed on purpose (swapped names, changed numbers, true-but-absent facts). Labels are author-written, not independent annotators."),
    h("table", {}, h("thead", {}, h("tr", {}, ["Grader model", "n", "Accuracy (95% CI)", "Precision", "Recall", "Missed hallucinations", "False alarms"].map((t) => h("th", {}, t)))),
      h("tbody", {}, j.map((r) => h("tr", {}, h("td", { class: "mono" }, r.judge_model), h("td", {}, r.n), h("td", {}, `${pct(r.accuracy)} (${pct(r.accuracy_ci95[0])}–${pct(r.accuracy_ci95[1])})`), h("td", {}, pct(r.precision)), h("td", {}, pct(r.recall)), h("td", {}, r.confusion.fn), h("td", {}, r.confusion.fp))))));
}

function overview() {
  const s = DATA.eval.summary;
  const cats = Object.entries(s.per_category);
  const view = h("div", {},
    evalPicker(),
    judgeCard(),
    h("div", { class: "kpis" },
      kpi("Overall pass rate", pct(s.pass_rate), ci(s, "pass_rate", `${s.n_cases} golden questions`)),
      kpi("Answered correctly", pct(s.answered_correctly_rate), ci(s, "answered_correctly_rate", "of answerable questions")),
      kpi("Hallucination rate", pct(s.hallucination_rate), ci(s, "hallucination_rate", "answers with ≥1 unsupported claim")),
      kpi("Refusal correctness", pct(s.refusal_correctness), ci(s, "refusal_correctness", "answered vs refused as expected")),
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

function evalPicker() {
  if (ALL.evals.length < 2) return null;
  const sel = h("select", { id: "eval-pick", "aria-label": "Choose evaluation run", onchange: (e) => { selectEval(e.target.value); render(); } },
    ALL.evals.map((e, i) => h("option", { value: e.eval_id, selected: e.eval_id === DATA.eval.eval_id }, `${i === 0 ? "Latest" : "Earlier"} · ${e.created_at.slice(0, 16).replace("T", " ")} · pass ${pct(e.summary.pass_rate)}`)));
  return h("div", { class: "picker" }, h("label", { for: "eval-pick" }, "Evaluation run "), sel);
}
function selectEval(id) {
  const e = ALL.evals.find((x) => x.eval_id === id) || ALL.evals[0];
  DATA = { ...ALL, eval: e, runs: e.runs };
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

function lineageView() {
  const e = DATA.eval, runs = DATA.runs;
  const resultFor = Object.fromEntries(e.results.map((r) => [r.run_id, r]));
  const docs = e.doc_stats || [];
  const sel = h("select", { id: "lin-pick", "aria-label": "Choose a run", onchange: (ev) => { location.hash = "lineage/" + ev.target.value; } },
    runs.map((r) => h("option", { value: r.run_id, selected: r.run_id === lineageSel }, `${r.case_id} · ${r.question.slice(0, 70)}`)));
  const cur = runs.find((r) => r.run_id === lineageSel) || runs[0];
  const res = resultFor[cur.run_id];
  return h("div", {},
    h("p", { class: "muted" }, "Lineage: which retrieved chunk backs each claim in an answer. A claim with no chunk is unsupported. The same graph can be loaded into Neo4j (see README)."),
    h("section", { class: "card", style: "margin-bottom:16px" }, h("h2", {}, "Which documents earn their place?"),
      h("table", {}, h("caption", {}, "Across the whole evaluation"), h("thead", {}, h("tr", {}, h("th", {}, "Document"), h("th", { class: "num" }, "Times retrieved"), h("th", { class: "num" }, "Claims it supported"), h("th", { class: "num" }, "Unsupported claims in runs that retrieved it"))),
        h("tbody", {}, docs.map((d) => h("tr", {}, h("td", {}, d.document), h("td", { class: "num" }, d.retrieved), h("td", { class: "num" }, d.supported_claims), h("td", { class: "num" }, d.unsupported_claims_in_runs)))))),
    h("section", { class: "card" }, h("div", { class: "picker" }, h("label", { for: "lin-pick" }, "Run "), sel),
      h("h2", {}, cur.question), h("div", { class: "meta" }, badge(res?.passed), h("code", {}, cur.case_id)),
      h("h3", {}, "Answer"), h("div", { class: "answer" }, cur.answer),
      h("h3", {}, "Claims and their evidence"),
      (res?.claims || []).length ? res.claims.map((c) => {
        const ch = c.chunk_index != null ? cur.retrieved[c.chunk_index] : null;
        return h("div", { class: "claim " + (c.supported ? "ok" : "bad") }, h("span", { class: "tag" }, c.supported ? "✓" : "✕"), c.claim,
          ch ? h("q", {}, `← chunk ${c.chunk_index + 1} of "${ch.source}"`) : h("q", {}, c.supported ? "supported (quote not matched to a single chunk)" : "no retrieved chunk supports this"));
      }) : h("p", { class: "muted" }, "No factual claims (a refusal)."),
      h("h3", {}, `Retrieved chunks (${cur.retrieved.length})`),
      cur.retrieved.map((c, i) => h("div", { class: "chunk" }, h("b", {}, `#${i + 1} · ${c.source}`), c.text))));
}
let lineageSel = null;

function compareView() {
  const c = EXTRA.comparison;
  if (!c) return h("div", { class: "card empty" }, "No comparison exported yet. Run two evals (--agent graph and --agent naive), then `python -m acp.evals.compare <evalA> <evalB>` and `make export`.");
  const A = c.a, B = c.b, rows = [["Answered correctly", "answered_correctly_rate"], ["Hallucination rate", "hallucination_rate"], ["Refusal correctness", "refusal_correctness"], ["Citation rate", "citation_rate"], ["Overall pass rate", "pass_rate"]];
  const nm = (x) => `${x.agent} · ${x.model}`;
  return h("div", {},
    h("p", { class: "muted" }, `Two agents answered the same ${c.n_shared} golden questions. “graph” = retrieve → judge → refine loop → synthesize. “naive” = retrieve → synthesize (no judge, no retry). Same retriever, prompt and model, so the difference is the judge loop.`),
    h("section", { class: "card" }, h("table", {}, h("caption", {}, `Graded by ${A.summary.judge_model || "the grader"}`),
      h("thead", {}, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "num" }, nm(A)), h("th", { class: "num" }, nm(B)))),
      h("tbody", {}, rows.map(([l, k]) => h("tr", {}, h("td", {}, l), h("td", { class: "num" }, pct(A.summary[k])), h("td", { class: "num" }, pct(B.summary[k])))),
        h("tr", {}, h("td", {}, "Avg latency"), h("td", { class: "num" }, ms(A.summary.avg_latency_ms)), h("td", { class: "num" }, ms(B.summary.avg_latency_ms))),
        h("tr", {}, h("td", {}, "Avg cost / question (est.)"), h("td", { class: "num" }, usd(A.summary.avg_cost_usd)), h("td", { class: "num" }, usd(B.summary.avg_cost_usd))),
        h("tr", {}, h("td", {}, "Avg LLM calls / question"), h("td", { class: "num" }, A.avg_llm_calls), h("td", { class: "num" }, B.avg_llm_calls))))),
    h("section", { class: "card", style: "margin-top:16px" }, h("h2", {}, "Paired result"),
      h("p", {}, `Only ${A.agent} passed ${c.only_a_passes.length} question(s): ${c.only_a_passes.join(", ") || "none"}.`),
      h("p", {}, `Only ${B.agent} passed ${c.only_b_passes.length} question(s): ${c.only_b_passes.join(", ") || "none"}.`),
      h("p", { class: "muted" }, `Sign test p = ${c.sign_test_p}. A small p means the gap is unlikely to be luck; with few discordant questions it rarely gets small, so read it as a hint, not proof.`)));
}

function trendsView() {
  const hist = EXTRA.history;
  if (!hist || !hist.length) return h("div", { class: "card empty" }, "No history yet. Each full eval run appends a point to results/history.jsonl.");
  const agents = [...new Set(hist.map((e) => e.agent))];
  const view = h("div", {},
    h("p", { class: "muted" }, "One point per full evaluation. Points from different golden-set sizes are not directly comparable; hover for n."),
    h("section", { class: "card" }, h("h2", {}, "Quality over time"), h("div", { class: "chart-wrap" }, h("canvas", { id: "trend-chart", role: "img", "aria-label": "Line chart of pass rate and hallucination rate across evaluation runs; same data in the table below." }))),
    h("section", { class: "card", style: "margin-top:16px" }, h("table", {}, h("caption", {}, "All recorded evaluations"),
      h("thead", {}, h("tr", {}, ["Date", "Agent", "Model", "Grader", "n", "Pass", "Halluc.", "Answered", "Latency"].map((t, i) => h("th", { class: i >= 4 ? "num" : "" }, t)))),
      h("tbody", {}, [...hist].reverse().map((e) => h("tr", {}, h("td", {}, e.created_at.slice(0, 16).replace("T", " ")), h("td", {}, e.agent), h("td", { class: "mono" }, e.agent_model || ""), h("td", { class: "mono" }, e.judge_model || ""), h("td", { class: "num" }, e.n_cases), h("td", { class: "num" }, pct(e.pass_rate)), h("td", { class: "num" }, pct(e.hallucination_rate)), h("td", { class: "num" }, pct(e.answered_correctly_rate)), h("td", { class: "num" }, ms(e.avg_latency_ms))))))));
  queueMicrotask(() => {
    const canvas = $("#trend-chart");
    if (!canvas) return;
    if (trendChart) { trendChart.destroy(); trendChart = null; }
    if (typeof Chart === "undefined") { canvas.replaceWith(h("p", { class: "muted" }, "Chart library unavailable; see the table.")); return; }
    const colors = [cssVar("--accent"), cssVar("--ok"), cssVar("--warn"), cssVar("--bad")];
    const datasets = agents.flatMap((a, i) => [
      { label: `${a}: pass rate`, data: hist.map((e) => (e.agent === a ? e.pass_rate * 100 : null)), borderColor: colors[i % 4], backgroundColor: colors[i % 4], spanGaps: true, tension: 0.2 },
      { label: `${a}: hallucination rate`, data: hist.map((e) => (e.agent === a && e.hallucination_rate != null ? e.hallucination_rate * 100 : null)), borderColor: colors[i % 4], borderDash: [5, 4], backgroundColor: colors[i % 4], spanGaps: true, tension: 0.2 },
    ]);
    trendChart = new Chart(canvas, { type: "line", data: { labels: hist.map((e) => e.created_at.slice(5, 16).replace("T", " ") + ` (n=${e.n_cases})`), datasets },
      options: { maintainAspectRatio: false, animation: false, scales: { y: { min: 0, max: 100, ticks: { color: cssVar("--muted"), callback: (v) => v + "%" }, grid: { color: cssVar("--border") } }, x: { ticks: { color: cssVar("--muted") }, grid: { display: false } } }, plugins: { legend: { labels: { color: cssVar("--text") } } } } });
  });
  return view;
}

/* ---------- router ---------- */
function render() {
  if (!DATA) return;
  const [view, arg] = (location.hash.slice(1) || "overview").split("/");
  const name = ["overview", "runs", "failures", "lineage", "compare", "trends"].includes(view) ? view : "overview";
  if (name === "lineage") lineageSel = arg || lineageSel || DATA.runs[0]?.run_id;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  const main = $("#main");
  const views = { runs: () => runsView(arg), failures: failuresView, lineage: lineageView, compare: compareView, trends: trendsView };
  main.replaceChildren((views[name] || overview)());
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
window.addEventListener("hashchange", render);

async function init() {
  if (REPO_URL) { const a = $("#repo-link"); a.href = REPO_URL; a.hidden = false; }
  try {
    const res = await fetch("data/traces.json", { cache: "no-cache" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    ALL = await res.json();
    for (const [key, file] of [["history", "history.json"], ["comparison", "comparison.json"], ["judge", "judge.json"]]) {
      try { const r = await fetch("data/" + file, { cache: "no-cache" }); if (r.ok) EXTRA[key] = await r.json(); } catch (_) { /* optional */ }
    }
    selectEval(ALL.evals[0].eval_id);
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
