// Shared login helpers. Two kinds of session, both HttpOnly cookies that expire on their own:
//  * v1, the bootstrap shared password (DASHBOARD_PASSWORD): "<expiry>.<HMAC(password, expiry)>". Only issued while no
//    user accounts exist; dies the moment the password is changed.
//  * v2, a personal account: "<base64url({sub, role, name, exp})>.<HMAC(session key, payload)>". The key is
//    SESSION_SECRET, or (if unset) derived from DASHBOARD_PASSWORD. The accounts themselves live in Redis (_users.js).
const crypto = require("crypto");

const COOKIE = "acp_session";
const MAX_AGE_S = 8 * 3600;
const MIN_PASSWORD_LENGTH = 8;

// Fail closed: with no usable password configured, nobody gets in.
function configuredPassword() {
  const p = process.env.DASHBOARD_PASSWORD;
  return typeof p === "string" && p.length >= MIN_PASSWORD_LENGTH ? p : null;
}

function sign(exp, password) {
  return crypto.createHmac("sha256", password).update("acp-session-v1:" + exp).digest("hex");
}

// Compare by hashing both sides first, so length differences leak nothing and the comparison is constant-time.
function safeEqual(a, b) {
  const ha = crypto.createHash("sha256").update(String(a)).digest();
  const hb = crypto.createHash("sha256").update(String(b)).digest();
  return crypto.timingSafeEqual(ha, hb);
}

function issue(password, nowMs = Date.now()) {
  const exp = Math.floor(nowMs / 1000) + MAX_AGE_S;
  return exp + "." + sign(exp, password);
}

function verify(token, password, nowMs = Date.now()) {
  if (!password || typeof token !== "string") return false;
  const parts = token.split(".");
  if (parts.length !== 2 || !/^\d{1,12}$/.test(parts[0]) || !parts[1]) return false;
  if (Number(parts[0]) < nowMs / 1000) return false;
  return safeEqual(parts[1], sign(parts[0], password));
}

// ---- v2 (personal account) sessions
function sessionKey() {
  const s = process.env.SESSION_SECRET;
  if (typeof s === "string" && s.length >= 16) return s;
  const pw = configuredPassword();
  return pw ? crypto.createHash("sha256").update("acp-session-key:" + pw).digest("hex") : null;
}

function signV2(payloadB64, key) {
  return crypto.createHmac("sha256", key).update("acp-session-v2:" + payloadB64).digest("hex");
}

function issueUser(user, nowMs = Date.now()) {
  const key = sessionKey();
  if (!key) return null;
  const body = { v: 2, sub: user.email, role: user.role, name: user.name, exp: Math.floor(nowMs / 1000) + MAX_AGE_S };
  const b64 = Buffer.from(JSON.stringify(body)).toString("base64url");
  return b64 + "." + signV2(b64, key);
}

function verifyUser(token, nowMs = Date.now()) {
  const key = sessionKey();
  if (!key || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 2 || /^\d+$/.test(parts[0]) || !/^[A-Za-z0-9_-]{10,600}$/.test(parts[0])) return null;
  if (!safeEqual(parts[1], signV2(parts[0], key))) return null;
  try {
    const b = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8"));
    if (b.v !== 2 || typeof b.sub !== "string" || !Number.isInteger(b.exp) || b.exp < nowMs / 1000) return null;
    return { email: b.sub, role: b.role === "admin" ? "admin" : "viewer", name: String(b.name || b.sub), exp: b.exp };
  } catch (_) { return null; }
}

function readCookie(req, name = COOKIE) {
  const raw = (req.headers && req.headers.cookie) || "";
  for (const part of raw.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

function sessionCookie(token, maxAge) {
  return `${COOKIE}=${token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${maxAge}`;
}

// Signature and expiry only (no network). Personal-account sessions are additionally re-checked against the register
// by authenticate() in _users.js, so a disabled account loses access within a minute.
function sessionOf(req) {
  const token = readCookie(req);
  const u = verifyUser(token);
  if (u) return { ...u, kind: "user" };
  const pw = configuredPassword();
  if (pw && verify(token, pw)) return { email: "shared-password", name: "Shared password", role: "admin", kind: "shared" };
  return null;
}

function isLoggedIn(req) {
  return sessionOf(req) !== null;
}

module.exports = { COOKIE, MAX_AGE_S, configuredPassword, safeEqual, issue, verify, issueUser, verifyUser, sessionKey, sessionOf, readCookie, sessionCookie, isLoggedIn };
