// POST {"email": "...", "password": "..."} -> sets an HttpOnly session cookie. The page shell is public; data is not.
// While NO accounts exist (a fresh deployment), {"password"} alone signs in with the shared DASHBOARD_PASSWORD, so the
// first admin can be created. Once any account exists, the shared password stops working and everyone signs in as themselves.
const { MAX_AGE_S, configuredPassword, safeEqual, issue, issueUser, sessionCookie } = require("./_auth");
const users = require("./_users");

const FAIL_DELAY_MS = Number(process.env.ACP_LOGIN_DELAY_MS || 800); // slows guessing; serverless has no shared counter
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const swallow = (p) => p.catch(() => {}); // the sign-in log and counters must never break signing in

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  let body = req.body;
  if (typeof body === "string") { try { body = JSON.parse(body); } catch (_) { body = {}; } }
  const given = body && typeof body.password === "string" ? body.password.slice(0, 200) : "";
  const email = body && typeof body.email === "string" ? users.normalizeEmail(body.email) : null;

  let count;
  try { count = await users.countUsers(); } catch (_) { return res.status(503).json({ error: "Sign-in is temporarily unavailable." }); }

  // ---- bootstrap: no accounts yet, shared password
  if (!count) {
    const pw = configuredPassword();
    if (!pw) return res.status(503).json({ error: "Login is not configured (set DASHBOARD_PASSWORD, at least 8 characters)." });
    if (!given || !safeEqual(given, pw)) { await sleep(FAIL_DELAY_MS); return res.status(401).json({ error: "Wrong password." }); }
    res.setHeader("Set-Cookie", sessionCookie(issue(pw), MAX_AGE_S));
    return res.status(200).json({ ok: true, mode: "shared" });
  }

  // ---- personal accounts
  if (!email || !given) { await sleep(FAIL_DELAY_MS); return res.status(401).json({ error: "Enter your email and password." }); }
  try {
    if (await users.isLocked(email)) return res.status(429).json({ error: "Too many wrong attempts. Try again in 15 minutes." });
    const u = await users.getUser(email);
    const good = await users.verifyPassword(u && u.hash, given); // runs a dummy hash for unknown emails: same time either way
    if (!u || u.disabled || !good) {
      await swallow(users.recordFailure(email));
      await swallow(users.logSignIn(email, false, !u ? "unknown" : u.disabled ? "disabled" : "password"));
      await sleep(FAIL_DELAY_MS);
      return res.status(401).json({ error: "Wrong email or password." });
    }
    const token = issueUser(u);
    if (!token) return res.status(503).json({ error: "Login is not configured (set SESSION_SECRET or DASHBOARD_PASSWORD)." });
    await swallow(users.clearFailures(email));
    await swallow(users.logSignIn(email, true, ""));
    res.setHeader("Set-Cookie", sessionCookie(token, MAX_AGE_S));
    return res.status(200).json({ ok: true, mode: "user" });
  } catch (_) {
    return res.status(503).json({ error: "Sign-in is temporarily unavailable." }); // never echo details
  }
};
