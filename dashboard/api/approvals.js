// POST {id, decision: "approve"|"reject", note?} -> records an admin's decision on a drafted refund.
const users = require("./_users");
const { decide } = require("./_approvals");

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
    const { code, body: out } = await decide(body, session);
    return res.status(code).json(out);
  } catch (_) {
    return res.status(502).json({ error: "Could not record the decision." }); // never echo details
  }
};
