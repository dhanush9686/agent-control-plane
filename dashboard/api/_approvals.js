// Deciding an approval: the one write the approvals page makes. A signed-in admin approves or rejects a drafted refund.
// The decision is stored under its own key with SET NX, so the first decision wins and a second one cannot overwrite it.
// The worker later copies each decision into the tamper-evident audit log (acp/assure/approvals.py).
// Nothing here moves money: the ledger behind the demo is a sample.
const { writeConfig, pipeline } = require("./_events");
const { maskPayload } = require("./_sim");

const APPROVAL_OPS = new Set(["GET", "SET"]);
const ID = /^apr_[a-f0-9]{16}$/;
const TTL_S = 7 * 24 * 3600, NOTE_MAX = 300, SENIOR_NOTE_MIN = 5;

// Returns {code, body}. `session` is {email, name, role}.
async function decide(body, session) {
  if (session.role !== "admin") return { code: 403, body: { error: "Only admins can approve or reject." } };
  const cfg = writeConfig();
  if (!cfg) return { code: 503, body: { error: "Approvals are not configured on this deployment." } };
  const b = body && typeof body === "object" ? body : {};
  if (typeof b.id !== "string" || !ID.test(b.id)) return { code: 400, body: { error: "Unknown approval." } };
  if (b.decision !== "approve" && b.decision !== "reject") return { code: 400, body: { error: "Choose approve or reject." } };
  const note = maskPayload(String(typeof b.note === "string" ? b.note : "").trim()).slice(0, NOTE_MAX);

  const [raw] = await pipeline(cfg, [["GET", "acp:live:approval:" + b.id]], APPROVAL_OPS);
  let rec = null;
  try { rec = raw ? JSON.parse(raw) : null; } catch (_) { rec = null; }
  if (!rec) return { code: 404, body: { error: "That approval no longer exists. It may have expired." } };
  if (rec.tier === "senior" && b.decision === "approve" && note.length < SENIOR_NOTE_MIN) {
    return { code: 400, body: { error: "A senior-tier approval needs a written reason." } };
  }

  const decision = { decision: b.decision === "approve" ? "approved" : "rejected", by: String(session.name || "admin").slice(0, 80), note, at: new Date().toISOString() };
  const [stored] = await pipeline(cfg, [["SET", "acp:live:approval:" + b.id + ":decision", JSON.stringify(decision), "NX", "EX", TTL_S]], APPROVAL_OPS);
  if (stored !== "OK") return { code: 409, body: { error: "Someone already decided this one." } };
  return { code: 200, body: { ok: true, state: decision.decision } };
}

module.exports = { decide, APPROVAL_OPS };
