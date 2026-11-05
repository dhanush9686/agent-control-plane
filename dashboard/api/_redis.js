// Reads live results from Upstash Redis over its REST API. READ-ONLY by construction:
//  * it only ever uses the read-only token (a write token in the environment is ignored, never used);
//  * it only ever sends GET, LRANGE, MGET and LLEN commands.
// (The only writers are the narrow write paths: _events.js, _sim.js, _users.js and _approvals.js.)
// Values are JSON strings written by `python -m acp.assure.live` (see acp/assure/publish.py for the key schema).
const TIMEOUT_MS = 5000;

function config() {
  const url = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_READ_ONLY_TOKEN || process.env.UPSTASH_REDIS_REST_READ_ONLY_TOKEN;
  return url && url.startsWith("https://") && token ? { url: url.replace(/\/$/, ""), token } : null;
}

function parse(raw) {
  if (typeof raw !== "string") return null;
  try { return JSON.parse(raw); } catch (_) { return null; } // one bad value must not break the page
}

async function readLive() {
  const cfg = config();
  if (!cfg) return null;
  const commands = [
    ["GET", "acp:live:latest"],
    ["GET", "acp:live:snapshot"],
    ["GET", "acp:live:audit"],
    ["LRANGE", "acp:live:runs", 0, 199],
    ["LRANGE", "acp:live:history", 0, 59],
  ];
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(cfg.url + "/pipeline", {
      method: "POST",
      headers: { Authorization: "Bearer " + cfg.token, "Content-Type": "application/json" },
      body: JSON.stringify(commands),
      signal: ctl.signal,
    });
    if (!r.ok) throw new Error("redis HTTP " + r.status);
    const out = await r.json();
    const val = (i) => (out[i] && !out[i].error ? out[i].result : null);
    const list = (i) => (Array.isArray(val(i)) ? val(i).map(parse).filter(Boolean) : []);
    return {
      via: "redis",
      latest: parse(val(0)),
      snapshot: parse(val(1)),
      audit: parse(val(2)),
      runs: list(3), // newest first
      history: list(4), // newest first
    };
  } finally {
    clearTimeout(timer);
  }
}

// What the browser may see of an event: never the payment note (it is free text), only whether there was one.
function publicEvent(e) {
  if (!e || typeof e !== "object") return null;
  const p = e.payment || {};
  return {
    id: e.id, event: e.event, state: e.state, received_at: e.received_at, started_at: e.started_at, finished_at: e.finished_at,
    payment: { id: p.id, amount: p.amount, status: p.status, method: p.method, tenant: p.notes && p.notes.tenant, has_note: !!(p.notes && p.notes.memo), error: p.error_reason || p.error_code || null },
    steps: Array.isArray(e.steps) ? e.steps.slice(-60) : [],
    verdict: e.verdict || null, cost_usd: e.cost_usd || 0, error: e.error || null,
    runs: Array.isArray(e.runs) ? e.runs.map((r) => ({
      mode: r.mode, scenario_id: r.scenario_id, category: r.category, passed: r.passed, attempted: r.attempted, effective: r.effective,
      withheld: r.withheld, answer: typeof r.answer === "string" ? r.answer.slice(0, 400) : "", events: r.events, failed_checks: r.failed_checks,
    })) : [],
  };
}

async function readEvents() {
  const cfg = config();
  if (!cfg) return { via: "none", configured: false, events: [], queue: 0 };
  const send = async (commands) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      const r = await fetch(cfg.url + "/pipeline", {
        method: "POST",
        headers: { Authorization: "Bearer " + cfg.token, "Content-Type": "application/json" },
        body: JSON.stringify(commands),
        signal: ctl.signal,
      });
      if (!r.ok) throw new Error("redis HTTP " + r.status);
      return (await r.json()).map((o) => (o && !o.error ? o.result : null));
    } finally {
      clearTimeout(timer);
    }
  };
  const [ids, queue] = await send([["LRANGE", "acp:live:events", 0, 29], ["LLEN", "acp:live:queue"]]);
  const keys = (Array.isArray(ids) ? ids : []).filter((i) => /^[A-Za-z0-9_-]{1,64}$/.test(i)).map((i) => "acp:live:event:" + i);
  const rows = keys.length ? (await send([["MGET", ...keys]]))[0] : [];
  return {
    via: "redis", configured: true, queue: Number(queue) || 0,
    events: (Array.isArray(rows) ? rows : []).map(parse).map(publicEvent).filter(Boolean),
  };
}

// A simulator job as the browser may see it. The attacker's raw model answer and who queued it stay server-side
// (the person's email is shown to admins only).
function publicJob(j, isAdmin) {
  if (!j || typeof j !== "object") return null;
  const runs = {};
  for (const m of ["guarded", "unguarded"]) {
    const r = j.runs && j.runs[m];
    if (r) runs[m] = { mode: r.mode, matrix: r.matrix, outcome: r.outcome, events: r.events, withheld: r.withheld, latency_ms: r.latency_ms, cost_usd: r.cost_usd, error: r.error || "", answer_shown: typeof r.answer_shown === "string" ? r.answer_shown.slice(0, 600) : "" };
  }
  return {
    id: j.id, kind: j.kind, preset: j.preset, tenant: j.tenant, state: j.state, title: j.title || null, attacker: j.attacker || null,
    prompt: j.prompt || j.payload || "", created_at: j.created_at, started_at: j.started_at, finished_at: j.finished_at,
    steps: Array.isArray(j.steps) ? j.steps.slice(-40) : [], runs: j.runs ? runs : null, verdict: j.verdict || null,
    cost_usd: j.cost_usd || 0, audit: j.audit || null, error: j.error || null, by: isAdmin ? j.by || null : null,
  };
}

async function readSim(isAdmin) {
  const cfg = config();
  if (!cfg) return { via: "none", configured: false, queue: 0, jobs: [] };
  const send = async (commands) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      const r = await fetch(cfg.url + "/pipeline", { method: "POST", headers: { Authorization: "Bearer " + cfg.token, "Content-Type": "application/json" }, body: JSON.stringify(commands), signal: ctl.signal });
      if (!r.ok) throw new Error("redis HTTP " + r.status);
      return (await r.json()).map((o) => (o && !o.error ? o.result : null));
    } finally { clearTimeout(timer); }
  };
  const [ids, queue] = await send([["LRANGE", "acp:live:sim:jobs", 0, 19], ["LLEN", "acp:live:sim:queue"]]);
  const keys = (Array.isArray(ids) ? ids : []).filter((i) => /^sim_[a-f0-9]{16}$/.test(i)).map((i) => "acp:live:sim:job:" + i);
  const rows = keys.length ? (await send([["MGET", ...keys]]))[0] : [];
  return { via: "redis", configured: true, queue: Number(queue) || 0, jobs: (Array.isArray(rows) ? rows : []).map(parse).map((j) => publicJob(j, isAdmin)).filter(Boolean) };
}

// One request to Redis with the read-only token. Returns each command's result (null on a per-command error).
async function send(cfg, commands) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(cfg.url + "/pipeline", { method: "POST", headers: { Authorization: "Bearer " + cfg.token, "Content-Type": "application/json" }, body: JSON.stringify(commands), signal: ctl.signal });
    if (!r.ok) throw new Error("redis HTTP " + r.status);
    return (await r.json()).map((o) => (o && !o.error ? o.result : null));
  } finally { clearTimeout(timer); }
}

// An approval as the browser may see it: ids, amounts, who decided and when. The person's email is never stored on it.
function publicApproval(rec, dec, aud) {
  if (!rec || typeof rec !== "object") return null;
  const src = rec.source || {};
  return {
    id: rec.id, tenant: rec.tenant, payment_id: rec.payment_id, amount_paise: rec.amount_paise, tier: rec.tier === "senior" ? "senior" : "standard",
    reason: typeof rec.reason === "string" ? rec.reason.slice(0, 200) : "", created_at: rec.created_at,
    source: { kind: src.kind === "event" ? "event" : "sim", id: String(src.id || "").slice(0, 64), title: String(src.title || "").slice(0, 80) },
    state: dec && (dec.decision === "approved" || dec.decision === "rejected") ? dec.decision : "pending",
    decision: dec ? { by: String(dec.by || "").slice(0, 80), note: String(dec.note || "").slice(0, 300), at: dec.at } : null,
    audit: aud && typeof aud.hash === "string" ? { hash: aud.hash } : null,
  };
}

async function readApprovals() {
  const cfg = config();
  if (!cfg) return { via: "none", configured: false, approvals: [] };
  const [ids] = await send(cfg, [["LRANGE", "acp:live:approvals", 0, 49]]);
  const list = (Array.isArray(ids) ? ids : []).filter((i) => /^apr_[a-f0-9]{16}$/.test(i));
  if (!list.length) return { via: "redis", configured: true, approvals: [] };
  const [recs, decs, auds] = await send(cfg, ["", ":decision", ":audit"].map((suffix) => ["MGET", ...list.map((i) => "acp:live:approval:" + i + suffix)]));
  const at = (a, i) => parse(Array.isArray(a) ? a[i] : null);
  return { via: "redis", configured: true, approvals: list.map((_, i) => publicApproval(at(recs, i), at(decs, i), at(auds, i))).filter(Boolean) };
}

// The compliance report is built by the worker (next to the audit log) and only stored here; this reads it back.
const REPORT_FORMATS = { html: "text/html; charset=utf-8", json: "application/json; charset=utf-8", csv: "text/csv; charset=utf-8" };

async function readReportMeta() {
  const cfg = config();
  if (!cfg) return { via: "none", configured: false, report: null };
  const [raw] = await send(cfg, [["GET", "acp:live:report:meta"]]);
  return { via: "redis", configured: true, report: parse(raw) };
}

async function readReportFile(format) {
  const cfg = config();
  if (!cfg || !Object.prototype.hasOwnProperty.call(REPORT_FORMATS, format)) return null;
  const [raw] = await send(cfg, [["GET", "acp:live:report:" + format]]);
  return typeof raw === "string" ? raw : null;
}

module.exports = { readLive, readEvents, readSim, readApprovals, readReportMeta, readReportFile, REPORT_FORMATS, publicEvent, publicJob, publicApproval, config };
