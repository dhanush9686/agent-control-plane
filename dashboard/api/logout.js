const { sessionCookie } = require("./_auth");

module.exports = (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  res.setHeader("Set-Cookie", sessionCookie("", 0));
  return res.status(200).json({ ok: true });
};
