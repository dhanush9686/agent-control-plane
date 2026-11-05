// GET -> who is signed in. 200 {email, name, role, mode}, or 401 {mode} so the login page knows which form to show.
const users = require("./_users");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "private, no-store");
  const s = await users.authenticate(req);
  if (s) return res.status(200).json({ email: s.email, name: s.name, role: s.role, mode: s.kind });
  let mode = "shared";
  try { mode = (await users.countUsers()) ? "user" : "shared"; } catch (_) { /* unknown: show the simple form */ }
  return res.status(401).json({ error: "Not signed in.", mode });
};
