// Reads live results from Upstash Redis over its REST API. READ-ONLY by construction:
//  * it only ever uses the read-only token (a write token in the environment is ignored, never used);
//  * it only ever sends GET, LRANGE, MGET and LLEN commands.
// (The webhook that queues events is the only writer; see _events.js.)
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

module.exports = { readLive, readEvents, readSim, publicEvent, publicJob, config };
