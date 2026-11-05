// Personal accounts: sign-in, lockout, roles, revocation, forged sessions, and that only the allowed Redis commands are sent.
// Password hashes are produced by acp/dashboard_users.py (pytest passes them in), so this also proves Python and Node agree.
const assert = require("assert");
const crypto = require("crypto");
const path = require("path");
const api = (f) => path.join(__dirname, "..", "dashboard", "api", f);
process.env.ACP_LOGIN_DELAY_MS = "1";
process.env.ACP_AUTH_CACHE_MS = "0";
process.env.DASHBOARD_PASSWORD = "the shared bootstrap password";
process.env.SESSION_SECRET = "a-long-session-secret-for-tests";
const SEED = JSON.parse(process.env.ACP_TEST_USERS); // [{email,name,role,hash,disabled,password}]

function call(handler, { method = "GET", body, headers = {}, query = {} } = {}) {
  return new Promise((resolve) => {
    const res = { headers: {}, code: 200 };
    res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; };
    res.status = (c) => { res.code = c; return res; };
    res.json = (o) => { res.body = o; resolve(res); return res; };
    Promise.resolve(handler({ method, body, headers, query }, res));
  });
}
const cookieOf = (res) => (res.headers["set-cookie"] || "").split(";")[0];

function redis(users, { down = false } = {}) {
  const kv = {}, sets = { "acp:users": new Set() }, lists = {}, sent = [];
  for (const u of users) { const { password, ...rec } = u; kv["acp:user:" + u.email] = JSON.stringify(rec); sets["acp:users"].add(u.email); }
  global.fetch = async (url, opts) => {
    if (down) throw new Error("ECONNREFUSED");
    const cmds = JSON.parse(opts.body);
    sent.push({ auth: opts.headers.Authorization, ops: cmds.map((c) => c[0]) });
    const out = cmds.map((c) => {
      const [op, key] = c;
      if (op === "GET") return { result: kv[key] ?? null };
      if (op === "SCARD") return { result: (sets[key] || new Set()).size };
      if (op === "SMEMBERS") return { result: [...(sets[key] || [])] };
      if (op === "MGET") return { result: c.slice(1).map((k) => kv[k] ?? null) };
      if (op === "INCR") { kv[key] = String(Number(kv[key] || 0) + 1); return { result: Number(kv[key]) }; }
      if (op === "EXPIRE") return { result: 1 };
      if (op === "DEL") { delete kv[key]; return { result: 1 }; }
      if (op === "LPUSH") { (lists[key] = lists[key] || []).unshift(c[2]); return { result: 1 }; }
      if (op === "LTRIM") return { result: "OK" };
      if (op === "LRANGE") return { result: (lists[key] || []).slice(c[2], c[3] + 1) };
      return { error: "unsupported " + op };
    });
    return { ok: true, status: 200, json: async () => out };
  };
  return { kv, lists, sent };
}
const env = () => { process.env.KV_REST_API_URL = "https://x.upstash.io"; process.env.KV_REST_API_READ_ONLY_TOKEN = "RO"; process.env.KV_REST_API_TOKEN = "RW"; };

(async () => {
  const login = require(api("login")), me = require(api("me")), data = require(api("data")), auth = require(api("_auth"));
  const [ada, vic, dis] = SEED;
  env();

  // --- bootstrap: no accounts yet -> the shared password signs in (and is reported as such)
  let r = redis([]);
  const boot = await call(login, { method: "POST", body: { password: process.env.DASHBOARD_PASSWORD } });
  assert.strictEqual(boot.code, 200); assert.strictEqual(boot.body.mode, "shared");
  const bootMe = await call(me, { headers: { cookie: cookieOf(boot) } });
  assert.strictEqual(bootMe.body.mode, "shared"); assert.strictEqual(bootMe.body.role, "admin");
  assert.strictEqual((await call(me)).body.mode, "shared", "logged-out visitors are told which form to show");

  // --- once an account exists the shared password stops working
  r = redis(SEED);
  assert.strictEqual((await call(me)).body.mode, "user");
  assert.strictEqual((await call(login, { method: "POST", body: { password: process.env.DASHBOARD_PASSWORD } })).code, 401);
  assert.strictEqual((await call(login, { method: "POST", body: { email: ada.email, password: process.env.DASHBOARD_PASSWORD } })).code, 401);

  // --- a good sign-in (hash made by Python) sets a v2 cookie; the cookie carries no password or hash
  const ok = await call(login, { method: "POST", body: { email: " " + ada.email.toUpperCase() + " ", password: ada.password } });
  assert.strictEqual(ok.code, 200); assert.strictEqual(ok.body.mode, "user");
  const raw = ok.headers["set-cookie"];
  for (const f of ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=28800"]) assert.ok(raw.includes(f), f);
  const adaCookie = cookieOf(ok);
  assert.ok(!adaCookie.includes(ada.password) && !Buffer.from(adaCookie.split("=")[1].split(".")[0], "base64url").toString().includes("scrypt"));
  const who = await call(me, { headers: { cookie: adaCookie } });
  assert.deepStrictEqual([who.code, who.body.email, who.body.role, who.body.mode], [200, ada.email, "admin", "user"]);
  assert.strictEqual((await call(data, { headers: { cookie: adaCookie }, query: { file: "assure" } })).code, 200);

  // --- wrong password and unknown email look identical; failures are counted and logged
  const bad = await call(login, { method: "POST", body: { email: ada.email, password: "wrong wrong wrong" } });
  const unknown = await call(login, { method: "POST", body: { email: "nobody@example.com", password: "wrong wrong wrong" } });
  assert.strictEqual(bad.code, 401); assert.deepStrictEqual(bad.body, unknown.body);
  assert.ok(r.lists["acp:auth:log"].some((l) => JSON.parse(l).ok === false) && r.lists["acp:auth:log"].some((l) => JSON.parse(l).ok === true));
  assert.ok(!JSON.stringify(r.lists).includes(ada.password), "passwords are never logged");

  // --- five wrong guesses lock the account, even against the right password
  r = redis(SEED);
  for (let i = 0; i < 5; i++) assert.strictEqual((await call(login, { method: "POST", body: { email: vic.email, password: "nope nope nope nope" } })).code, 401);
  const locked = await call(login, { method: "POST", body: { email: vic.email, password: vic.password } });
  assert.strictEqual(locked.code, 429); assert.ok(!locked.headers["set-cookie"]);
  assert.strictEqual((await call(login, { method: "POST", body: { email: ada.email, password: ada.password } })).code, 200, "other accounts are unaffected");

  // --- a disabled account cannot sign in
  r = redis(SEED);
  assert.strictEqual((await call(login, { method: "POST", body: { email: dis.email, password: dis.password } })).code, 401);

  // --- roles: viewers get data but not the register; admins get it without hashes
  const vicCookie = cookieOf(await call(login, { method: "POST", body: { email: vic.email, password: vic.password } }));
  assert.strictEqual((await call(data, { headers: { cookie: vicCookie }, query: { file: "assure" } })).code, 200);
  assert.strictEqual((await call(data, { headers: { cookie: vicCookie }, query: { file: "users" } })).code, 403);
  const reg = await call(data, { headers: { cookie: adaCookie }, query: { file: "users" } });
  assert.strictEqual(reg.code, 200); assert.strictEqual(reg.body.users.length, 3);
  assert.ok(!JSON.stringify(reg.body).includes("scrypt"), "hashes never leave the server");

  // --- revocation: disabling or deleting an account ends its existing session
  const rec = JSON.parse(r.kv["acp:user:" + vic.email]); rec.disabled = true; r.kv["acp:user:" + vic.email] = JSON.stringify(rec);
  assert.strictEqual((await call(data, { headers: { cookie: vicCookie }, query: { file: "assure" } })).code, 401);
  delete r.kv["acp:user:" + ada.email];
  assert.strictEqual((await call(me, { headers: { cookie: adaCookie } })).code, 401);

  // --- forged, tampered, expired and foreign-key sessions
  r = redis(SEED);
  const fresh = cookieOf(await call(login, { method: "POST", body: { email: vic.email, password: vic.password } })).split("=")[1];
  const [b64, sig] = fresh.split(".");
  const body = JSON.parse(Buffer.from(b64, "base64url").toString());
  const swap = (o) => Buffer.from(JSON.stringify({ ...body, ...o })).toString("base64url");
  const mk = (payload, key) => payload + "." + crypto.createHmac("sha256", key).update("acp-session-v2:" + payload).digest("hex");
  const forged = [
    swap({ role: "admin" }) + "." + sig,                              // role edited, old signature
    mk(swap({ role: "admin" }), "some other key entirely"),            // signed with the wrong key
    mk(swap({ exp: Math.floor(Date.now() / 1000) - 5 }), process.env.SESSION_SECRET), // correctly signed but expired
    b64 + ".deadbeef", b64, "." + sig,
  ];
  for (const c of forged) assert.strictEqual((await call(data, { headers: { cookie: "acp_session=" + c }, query: { file: "assure" } })).code, 401, c.slice(0, 40));
  assert.strictEqual(auth.verifyUser(mk(swap({ role: "admin" }), process.env.SESSION_SECRET)).role, "admin", "control: a correctly signed token is accepted by the verifier");

  // --- Redis down: sign-in is unavailable (503) and an account session is refused (fail closed)
  r = redis(SEED);
  const goodCookie = cookieOf(await call(login, { method: "POST", body: { email: ada.email, password: ada.password } }));
  redis(SEED, { down: true });
  const down = await call(login, { method: "POST", body: { email: ada.email, password: ada.password } });
  assert.strictEqual(down.code, 503); assert.ok(!JSON.stringify(down.body).includes("ECONNREFUSED"));
  assert.strictEqual((await call(data, { headers: { cookie: goodCookie }, query: { file: "assure" } })).code, 401);

  // --- only the allowed commands, with the right token for each
  r = redis(SEED);
  await call(login, { method: "POST", body: { email: ada.email, password: ada.password } });
  await call(data, { headers: { cookie: adaCookie }, query: { file: "users" } });
  const READ = ["GET", "SCARD", "SMEMBERS", "MGET", "LRANGE"], WRITE = ["GET", "INCR", "EXPIRE", "DEL", "LPUSH", "LTRIM"];
  for (const s of r.sent) {
    if (s.auth === "Bearer RO") for (const op of s.ops) assert.ok(READ.includes(op), "read token sent " + op);
    else { assert.strictEqual(s.auth, "Bearer RW"); for (const op of s.ops) assert.ok(WRITE.includes(op), "write token sent " + op); }
  }
  console.log("dashboard users: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
