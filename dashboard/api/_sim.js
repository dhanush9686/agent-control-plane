// The guardrail simulator's write side: validate a request, rate-limit the person, queue a job for the worker.
// Same pattern as the webhook (_events.js): one narrow write path, confined to SIM_WRITE_OPS. The attack runs elsewhere,
// in the worker, against the real policy engine; this function only queues it.
const crypto = require("crypto");
const { writeConfig, pipeline, dispatchStatus } = require("./_events");

const PREFIX = "acp:live:sim:";
const QUEUE = PREFIX + "queue", JOBS = PREFIX + "jobs";
const SIM_WRITE_OPS = new Set(["SET", "LPUSH", "LTRIM", "EXPIRE", "LLEN", "INCR"]);
const TENANTS = ["m_acme", "m_zen"];
// Must match PRESETS in acp/assure/simulate.py (a test compares them).
const PRESETS = {
  "cross-tenant": ["m_acme", "m_zen"], "over-refund": ["m_acme", "m_zen"], "prompt-override": ["m_acme", "m_zen"],
  "hidden-instruction": ["m_acme"], "card-leak": ["m_acme"], "valid-refund": ["m_acme", "m_zen"],
};
const PAYLOAD_MAX = 400, PER_HOUR = 10, QUEUE_MAX = 20, JOB_TTL_S = 7 * 24 * 3600, JOBS_KEPT = 50;

// Card, email and phone shapes are replaced before anything is stored or queued.
function maskPayload(s) {
  return s
    .replace(/\b(?:\d[ -]?){12,19}\b/g, "[card]")
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[email]")
    .replace(/(?:\+?91[ -]?)?\b\d{5}[ -]?\d{5}\b/g, "[phone]");
}

// Returns {code, body}. `session` is {email, role}.
async function createJob(body, session) {
  const cfg = writeConfig();
  if (!cfg) return { code: 503, body: { error: "The simulator is not configured on this deployment." } };
  const b = body && typeof body === "object" ? body : {};
  const tenant = b.tenant;
  if (!TENANTS.includes(tenant)) return { code: 400, body: { error: "Choose a merchant." } };
  const hasPreset = typeof b.preset === "string", hasPayload = typeof b.payload === "string";
  if (hasPreset === hasPayload) return { code: 400, body: { error: "Send either a preset or a custom payload." } };
  let job;
  const now = new Date().toISOString();
  if (hasPreset) {
    if (!Object.prototype.hasOwnProperty.call(PRESETS, b.preset) || !PRESETS[b.preset].includes(tenant)) return { code: 400, body: { error: "That preset is not available for this merchant." } };
    job = { kind: "preset", preset: b.preset };
  } else {
    if (session.role !== "admin") return { code: 403, body: { error: "Custom payloads are for admins. Viewers can run the presets." } };
    const text = b.payload.trim();
    if (!text || text.length > PAYLOAD_MAX) return { code: 400, body: { error: `The payload must be 1 to ${PAYLOAD_MAX} characters.` } };
    job = { kind: "custom", preset: null, payload: maskPayload(text) };
  }

  const who = crypto.createHash("sha256").update(String(session.email)).digest("hex").slice(0, 16);
  const [n, depth] = await pipeline(cfg, [["INCR", PREFIX + "rate:" + who], ["LLEN", QUEUE]], SIM_WRITE_OPS);
  if (Number(n) === 1) await pipeline(cfg, [["EXPIRE", PREFIX + "rate:" + who, 3600]], SIM_WRITE_OPS);
  if (Number(n) > PER_HOUR) return { code: 429, body: { error: `At most ${PER_HOUR} simulations an hour each. Try again later.` } };
  if (Number(depth) >= QUEUE_MAX) return { code: 429, body: { error: "The simulator queue is full. Try again in a minute." } };

  const id = "sim_" + crypto.randomBytes(8).toString("hex");
  const record = { id, ...job, tenant, by: String(session.name || "user").slice(0, 60), created_at: now, state: "queued", steps: [{ t: now, text: "Job queued for the worker", tone: "neutral" }] };
  await pipeline(cfg, [
    ["SET", PREFIX + "job:" + id, JSON.stringify(record), "EX", JOB_TTL_S],
    ["LPUSH", JOBS, id], ["LTRIM", JOBS, 0, JOBS_KEPT - 1],
    ["LPUSH", QUEUE, id], ["EXPIRE", QUEUE, JOB_TTL_S],
  ], SIM_WRITE_OPS);
  const ping = await dispatchStatus(cfg, "acp-sim");
  return { code: 200, body: { id, worker_pinged: ping === "ok", worker_ping: ping } };
}

module.exports = { createJob, maskPayload, PRESETS, TENANTS, SIM_WRITE_OPS };
