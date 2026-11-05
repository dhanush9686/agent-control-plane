// POST {tenant, preset} or {tenant, payload} -> queues a guardrail-simulator job for the worker. Needs a signed-in user.
// Presets are open to everyone signed in; custom payloads need the admin role. Limited to 10 a person an hour.
const users = require("./_users");
const { createJob } = require("./_sim");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  const session = await users.authenticate(req);
  if (!session) return res.status(401).json({ error: "Login required." });
  let body = req.body;
  if (typeof body === "string") { try { body = JSON.parse(body); } catch (_) { body = {}; } }
  try {
    const { code, body: out } = await createJob(body, session);
    return res.status(code).json(out);
  } catch (_) {
    return res.status(502).json({ error: "Could not queue the simulation." }); // never echo details
  }
};
