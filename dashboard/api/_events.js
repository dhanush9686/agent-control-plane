// The write side of live events: verify a Razorpay webhook, keep only the fields we use, queue it for the worker.
// This is the ONE place in the dashboard that writes to Redis. It uses the integration's write token
// (KV_REST_API_TOKEN) and only ever sends the commands listed in WRITE_OPS. The page-serving code (_redis.js)
// still uses the read-only token only.
const crypto = require("crypto");

const PREFIX = "acp:live:";
const QUEUE = PREFIX + "queue", EVENTS = PREFIX + "events";
const ACCEPTED = new Set(["payment.authorized", "payment.captured", "payment.failed"]);
const WRITE_OPS = new Set(["SET", "LPUSH", "LTRIM", "EXPIRE", "LLEN"]);
const QUEUE_MAX = 50, EVENTS_KEPT = 100, EVENT_TTL_S = 7 * 24 * 3600, SEEN_TTL_S = 24 * 3600;
const MAX_AGE_S = 48 * 3600; // Razorpay retries for about a day; anything older is a replay
const TIMEOUT_MS = 5000;

function writeConfig() {
  const url = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;
  return url && url.startsWith("https://") && token ? { url: url.replace(/\/$/, ""), token } : null;
}

async function pipeline(cfg, commands, allowed = WRITE_OPS) {
  for (const [op] of commands) if (!allowed.has(op)) throw new Error("command not allowed: " + op);
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
    return out.map((o) => (o && !o.error ? o.result : null));
  } finally {
    clearTimeout(timer);
  }
}

// Ask GitHub Actions to start the hosted worker (see .github/workflows/live-worker.yml). Optional: with no token or repo
// configured nothing happens and the hourly schedule is the backstop. At most one ping a minute, and a failure here never
// affects the webhook's answer to Razorpay.
async function dispatchWorker(cfg, eventType) {
  const token = process.env.GITHUB_DISPATCH_TOKEN, repo = process.env.GITHUB_REPO;
  if (!token || !/^[\w.-]+\/[\w.-]+$/.test(repo || "")) return false;
  try {
    const [first] = await pipeline(cfg, [["SET", PREFIX + "dispatch", "1", "NX", "EX", 60]]);
    if (first !== "OK") return false;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 4000);
    try {
      const r = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
        method: "POST", signal: ctl.signal,
        headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType }),
      });
      return r.status === 204;
    } finally { clearTimeout(timer); }
  } catch (_) { return false; }
}

function verifySignature(raw, signature, secret) {
  if (!secret || secret.length < 8 || typeof signature !== "string") return false;
  const want = crypto.createHmac("sha256", secret).update(raw).digest("hex");
  const a = Buffer.from(want), b = Buffer.from(signature.trim());
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// Only the fields the worker's own whitelist reads. Email, phone, VPA, card and bank fields never get past here.
function slimPayment(p) {
  if (!p || typeof p !== "object" || typeof p.id !== "string" || !/^pay_[A-Za-z0-9]{1,40}$/.test(p.id)) return null;
  if (!Number.isInteger(p.amount) || p.amount < 0 || p.amount > 100_000_000) return null;
  const notes = p.notes && typeof p.notes === "object" && !Array.isArray(p.notes) ? p.notes : {};
  const str = (v, n) => (typeof v === "string" ? v.slice(0, n) : null);
  return {
    id: p.id,
    amount: p.amount,
    status: str(p.status, 20),
    method: str(p.method, 20),
    created_at: Number.isInteger(p.created_at) ? p.created_at : null,
    error_code: str(p.error_code, 60),
    error_reason: str(p.error_reason, 60),
    amount_refunded: Number.isInteger(p.amount_refunded) ? p.amount_refunded : 0,
    notes: { tenant: str(notes.tenant, 40), memo: str(notes.memo, 300) },
  };
}

function eventId(headerId, raw) {
  const h = typeof headerId === "string" ? headerId.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64) : "";
  return h || "h" + crypto.createHash("sha256").update(raw).digest("hex").slice(0, 32);
}

// Returns {code, body}. `raw` is the exact bytes Razorpay sent: the signature is over these, not over parsed JSON.
async function ingest(raw, headers) {
  const secret = process.env.RAZORPAY_WEBHOOK_SECRET;
  if (!secret || secret.length < 8) return { code: 503, body: { error: "Webhook is not configured." } };
  if (!verifySignature(raw, headers["x-razorpay-signature"], secret)) return { code: 401, body: { error: "Bad signature." } };
  const cfg = writeConfig();
  if (!cfg) return { code: 503, body: { error: "Event store is not configured." } };

  let body;
  try { body = JSON.parse(raw.toString("utf8")); } catch (_) { return { code: 400, body: { error: "Not JSON." } }; }
  if (!ACCEPTED.has(body && body.event)) return { code: 200, body: { ignored: "event type" } };
  const age = Math.floor(Date.now() / 1000) - (Number.isInteger(body.created_at) ? body.created_at : 0);
  if (Number.isInteger(body.created_at) && age > MAX_AGE_S) return { code: 200, body: { ignored: "too old" } };
  const payment = slimPayment(body.payload && body.payload.payment && body.payload.payment.entity);
  if (!payment) return { code: 200, body: { ignored: "no usable payment" } };

  const id = eventId(headers["x-razorpay-event-id"], raw);
  const [fresh, queued] = await pipeline(cfg, [["SET", PREFIX + "seen:" + id, "1", "NX", "EX", SEEN_TTL_S], ["LLEN", QUEUE]]);
  if (fresh !== "OK") return { code: 200, body: { duplicate: true } };
  if (Number(queued) >= QUEUE_MAX) return { code: 200, body: { queued: false, reason: "queue full" } };

  const now = new Date().toISOString();
  const record = {
    id, event: body.event, received_at: now, state: "queued", payment, steps: [
      { t: now, text: `Payment ${payment.id} received (${body.event}), signature verified`, tone: "neutral" },
      { t: now, text: "Queued for the agent", tone: "neutral" },
    ],
  };
  await pipeline(cfg, [
    ["SET", PREFIX + "event:" + id, JSON.stringify(record), "EX", EVENT_TTL_S],
    ["LPUSH", EVENTS, id], ["LTRIM", EVENTS, 0, EVENTS_KEPT - 1],
    ["LPUSH", QUEUE, id], ["EXPIRE", QUEUE, EVENT_TTL_S],
  ]);
  const pinged = await dispatchWorker(cfg, "acp-event");
  return { code: 200, body: { queued: true, id, worker_pinged: pinged } };
}

module.exports = { ingest, verifySignature, slimPayment, eventId, writeConfig, pipeline, dispatchWorker, WRITE_OPS };
