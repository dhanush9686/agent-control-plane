// Shared login helpers. The password lives only in the DASHBOARD_PASSWORD environment variable.
// A session is "<expiry>.<HMAC-SHA256(password, expiry)>": it cannot be forged without the password, expires on
// its own, and every session dies the moment the password is changed. No user database, no stored secrets.
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

function isLoggedIn(req) {
  const pw = configuredPassword();
  return !!pw && verify(readCookie(req), pw);
}

module.exports = { COOKIE, MAX_AGE_S, configuredPassword, safeEqual, issue, verify, readCookie, sessionCookie, isLoggedIn };
