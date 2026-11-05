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

module.exports = { readLive, readEvents, publicEvent, config };
