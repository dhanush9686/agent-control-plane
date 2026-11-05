// POST {"password": "..."} -> sets an HttpOnly session cookie. The page shell is public; data is not (see data.js).
const { MAX_AGE_S, configuredPassword, safeEqual, issue, sessionCookie } = require("./_auth");

const FAIL_DELAY_MS = Number(process.env.ACP_LOGIN_DELAY_MS || 800); // slows guessing; serverless has no shared counter

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  const pw = configuredPassword();
  if (!pw) return res.status(503).json({ error: "Login is not configured (set DASHBOARD_PASSWORD, at least 8 characters)." });
  let body = req.body;
  if (typeof body === "string") { try { body = JSON.parse(body); } catch (_) { body = {}; } }
  const given = body && typeof body.password === "string" ? body.password.slice(0, 200) : "";
  if (!given || !safeEqual(given, pw)) {
    await new Promise((r) => setTimeout(r, FAIL_DELAY_MS));
    return res.status(401).json({ error: "Wrong password." });
  }
  res.setHeader("Set-Cookie", sessionCookie(issue(pw), MAX_AGE_S));
  return res.status(200).json({ ok: true });
};
