// Personal accounts: the register lives in Redis (written by `make dashboard-user`, read here with the READ-ONLY token),
// passwords are salted scrypt hashes, and sign-in attempts are counted and logged.
//
// One deliberate write path, like the webhook (_events.js): failed-attempt counters and the sign-in log need the write
// token, and are confined to AUTH_WRITE_OPS below. If the write token is absent the lockout is skipped (the fixed
// delay on a wrong guess still applies) and nothing else changes.
const crypto = require("crypto");
const { config: readConfig } = require("./_redis");
const { writeConfig } = require("./_events");
const { sessionOf } = require("./_auth");

const AUTH_WRITE_OPS = new Set(["GET", "INCR", "EXPIRE", "DEL", "LPUSH", "LTRIM"]);
const READ_OPS = new Set(["GET", "SCARD", "SMEMBERS", "MGET", "LRANGE"]);
const MAX_FAILS = 5, LOCK_S = 900, LOG_KEPT = 100, TIMEOUT_MS = 5000;
const cacheMs = () => Number(process.env.ACP_AUTH_CACHE_MS ?? 30_000);
const EMAIL = /^[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$/;
// A fixed throwaway hash so an unknown email costs the same time as a wrong password (no user enumeration by timing).
const DUMMY = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + Buffer.alloc(64).toString("base64");

async function send(cfg, commands, allowed) {
  for (const [op] of commands) if (!allowed.has(op)) throw new Error("command not allowed: " + op);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(cfg.url + "/pipeline", {
      method: "POST", headers: { Authorization: "Bearer " + cfg.token, "Content-Type": "application/json" },
      body: JSON.stringify(commands), signal: ctl.signal,
    });
    if (!r.ok) throw new Error("redis HTTP " + r.status);
    return (await r.json()).map((o) => (o && !o.error ? o.result : null));
  } finally { clearTimeout(timer); }
}
const read = (cmds) => { const c = readConfig(); if (!c) throw new Error("redis not configured"); return send(c, cmds, READ_OPS); };

const normalizeEmail = (e) => (typeof e === "string" && EMAIL.test(e.trim().toLowerCase()) ? e.trim().toLowerCase() : null);
const parse = (raw) => { try { return typeof raw === "string" ? JSON.parse(raw) : null; } catch (_) { return null; } };
const userKey = (email) => "acp:user:" + email;

// null when Redis is not configured (accounts unavailable), else how many accounts exist.
async function countUsers() {
  if (!readConfig()) return null;
  return Number((await read([["SCARD", "acp:users"]]))[0]) || 0;
}

async function getUser(email) {
  return parse((await read([["GET", userKey(email)]]))[0]);
}

function scryptAsync(password, salt, N, r, p, len) {
  return new Promise((resolve, reject) =>
    crypto.scrypt(password, salt, len, { N, r, p, maxmem: 64 * 1024 * 1024 }, (e, k) => (e ? reject(e) : resolve(k))));
}

// stored = "scrypt$N$r$p$<salt b64>$<hash b64>" (see acp/dashboard_users.py). Constant-time compare.
async function verifyPassword(stored, password) {
  const parts = typeof stored === "string" ? stored.split("$") : [];
  const ok = parts.length === 6 && parts[0] === "scrypt";
  const [, N, r, p, salt, want] = ok ? parts : DUMMY.split("$");
  const wantBuf = Buffer.from(want, "base64");
  const n = Number(N), rr = Number(r), pp = Number(p);
  if (!(n >= 1024 && n <= 65536 && rr >= 1 && rr <= 16 && pp >= 1 && pp <= 4) || !wantBuf.length) return false;
  const got = await scryptAsync(String(password), Buffer.from(salt, "base64"), n, rr, pp, wantBuf.length);
  return ok && crypto.timingSafeEqual(got, wantBuf);
}

const failKey = (email) => "acp:auth:fail:" + crypto.createHash("sha256").update(email).digest("hex").slice(0, 24);

async function isLocked(email) {
  const cfg = writeConfig();
  if (!cfg) return false;
  return Number((await send(cfg, [["GET", failKey(email)]], AUTH_WRITE_OPS))[0]) >= MAX_FAILS;
}
async function recordFailure(email) {
  const cfg = writeConfig();
  if (!cfg) return;
  const [n] = await send(cfg, [["INCR", failKey(email)]], AUTH_WRITE_OPS);
  if (n === 1) await send(cfg, [["EXPIRE", failKey(email), LOCK_S]], AUTH_WRITE_OPS);
}
async function clearFailures(email) {
  const cfg = writeConfig();
  if (cfg) await send(cfg, [["DEL", failKey(email)]], AUTH_WRITE_OPS);
}
// Who signed in, never passwords or IP addresses.
async function logSignIn(email, ok, reason) {
  const cfg = writeConfig();
  if (!cfg) return;
  const line = JSON.stringify({ t: new Date().toISOString(), email: email || "", ok, reason: reason || "" });
  await send(cfg, [["LPUSH", "acp:auth:log", line], ["LTRIM", "acp:auth:log", 0, LOG_KEPT - 1]], AUTH_WRITE_OPS);
}

// Signature check, then (for personal accounts) the register: a deleted or disabled account is refused. Fails closed
// if the register cannot be read. Results are cached for 30 s so a polling page does not hit Redis on every request.
const cache = new Map();
async function authenticate(req) {
  const s = sessionOf(req);
  if (!s || s.kind !== "user") return s;
  const hit = cache.get(s.email);
  if (hit && hit.until > Date.now()) return hit.ok ? s : null;
  let ok = false;
  try { const u = await getUser(s.email); ok = !!u && !u.disabled; } catch (_) { return null; }
  cache.set(s.email, { ok, until: Date.now() + cacheMs() });
  return ok ? s : null;
}

async function listUsers() {
  const emails = ((await read([["SMEMBERS", "acp:users"]]))[0] || []).filter((e) => EMAIL.test(e)).sort();
  const rows = emails.length ? (await read([["MGET", ...emails.map(userKey)]]))[0] || [] : [];
  const log = (await read([["LRANGE", "acp:auth:log", 0, 29]]))[0] || [];
  return {
    users: rows.map(parse).filter(Boolean).map((u) => ({ email: u.email, name: u.name, role: u.role, created_at: u.created_at, disabled: !!u.disabled })),
    signins: log.map(parse).filter(Boolean),
  };
}

module.exports = { normalizeEmail, countUsers, getUser, verifyPassword, isLocked, recordFailure, clearFailures, logSignIn, authenticate, listUsers, MAX_FAILS };
