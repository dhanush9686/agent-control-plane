// Reads live results from Upstash Redis over its REST API. READ-ONLY by construction:
//  * it only ever uses the read-only token (a write token in the environment is ignored, never used);
//  * it only ever sends GET and LRANGE commands.
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

module.exports = { readLive, config };
