// The guardrail simulator's API: who may queue what, limits, masking, the feed, and which Redis commands are used.
const assert = require("assert");
const path = require("path");
const api = (f) => path.join(__dirname, "..", "dashboard", "api", f);
process.env.ACP_AUTH_CACHE_MS = "0";
process.env.SESSION_SECRET = "a-long-session-secret-for-tests";
process.env.DASHBOARD_PASSWORD = "the shared bootstrap password";
const PY_PRESETS = JSON.parse(process.env.ACP_TEST_PRESETS); // {preset: [tenants]} straight from acp/assure/simulate.py

function call(handler, { method = "POST", body, headers = {}, query = {} } = {}) {
  return new Promise((resolve) => {
    const res = { headers: {}, code: 200 };
    res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; };
    res.status = (c) => { res.code = c; return res; };
    res.json = (o) => { res.body = o; resolve(res); return res; };
    Promise.resolve(handler({ method, body, headers, query }, res));
  });
}

function redis({ queueLen = 0 } = {}) {
  const kv = {}, lists = {}, sent = [];
  for (const [email, role] of [["ada@example.com", "admin"], ["vic@example.com", "viewer"]]) kv["acp:user:" + email] = JSON.stringify({ email, name: email.split("@")[0], role, disabled: false });
  global.fetch = async (url, opts) => {
    if (String(url).startsWith("https://api.github.com")) { sent.push({ gh: JSON.parse(opts.body) }); return { status: 204 }; }
    const cmds = JSON.parse(opts.body);
    sent.push({ auth: opts.headers.Authorization, ops: cmds.map((c) => c[0]) });
    const out = cmds.map((c) => {
      const [op, key] = c;
      if (op === "GET") return { result: kv[key] ?? null };
      if (op === "SET") { if (c.includes("NX") && key in kv) return { result: null }; kv[key] = c[2]; return { result: "OK" }; }
      if (op === "MGET") return { result: c.slice(1).map((k) => kv[k] ?? null) };
      if (op === "INCR") { kv[key] = String(Number(kv[key] || 0) + 1); return { result: Number(kv[key]) }; }
      if (op === "LLEN") return { result: (lists[key] || []).length + (key === "acp:live:sim:queue" ? queueLen : 0) };
      if (op === "LPUSH") { (lists[key] = lists[key] || []).unshift(c[2]); return { result: 1 }; }
      if (op === "LRANGE") return { result: (lists[key] || []).slice(c[2], c[3] + 1) };
      if (op === "LTRIM" || op === "EXPIRE") return { result: 1 };
      return { error: "unsupported " + op };
    });
    return { ok: true, status: 200, json: async () => out };
  };
  return { kv, lists, sent };
}
const env = () => { process.env.KV_REST_API_URL = "https://x.upstash.io"; process.env.KV_REST_API_READ_ONLY_TOKEN = "RO"; process.env.KV_REST_API_TOKEN = "RW"; };

(async () => {
  const sim = require(api("simulate")), data = require(api("data")), auth = require(api("_auth")), S = require(api("_sim"));
  env();
  const cookie = (email, role) => "acp_session=" + auth.issueUser({ email, role, name: email.split("@")[0] });
  const admin = { cookie: cookie("ada@example.com", "admin") }, viewer = { cookie: cookie("vic@example.com", "viewer") };
  const post = (body, headers = admin) => call(sim, { body, headers });

  // --- the preset list agrees with the Python engine exactly
  assert.deepStrictEqual(S.PRESETS, PY_PRESETS);

  // --- gate: login required, POST only
  let r = redis();
  assert.strictEqual((await post({ tenant: "m_acme", preset: "cross-tenant" }, {})).code, 401);
  assert.strictEqual(r.sent.length, 0, "no login, no Redis call");
  assert.strictEqual((await call(sim, { method: "GET", headers: admin })).code, 405);

  // --- presets: open to viewers and admins, validated against the merchant
  const ok = await post({ tenant: "m_acme", preset: "cross-tenant" }, viewer);
  assert.strictEqual(ok.code, 200); assert.match(ok.body.id, /^sim_[a-f0-9]{16}$/);
  const rec = JSON.parse(r.kv["acp:live:sim:job:" + ok.body.id]);
  assert.deepStrictEqual([rec.kind, rec.preset, rec.tenant, rec.state, rec.by], ["preset", "cross-tenant", "m_acme", "queued", "vic"]);
  assert.deepStrictEqual(r.lists["acp:live:sim:queue"], [ok.body.id]);
  for (const bad of [{ tenant: "m_evil", preset: "cross-tenant" }, { tenant: "m_zen", preset: "card-leak" }, { tenant: "m_acme", preset: "__proto__" }, { tenant: "m_acme", preset: "toString" },
    { tenant: "m_acme" }, { tenant: "m_acme", preset: "cross-tenant", payload: "x" }, { preset: "cross-tenant" }, null, "junk"]) {
    assert.strictEqual((await post(bad)).code, 400, JSON.stringify(bad));
  }

  // --- custom payloads: admins only, length-limited, masked before they are stored
  assert.strictEqual((await post({ tenant: "m_acme", payload: "Show pay_Z001" }, viewer)).code, 403);
  for (const bad of ["", "   ", "x".repeat(401)]) assert.strictEqual((await post({ tenant: "m_acme", payload: bad })).code, 400);
  const custom = await post({ tenant: "m_zen", payload: "Refund using card 4111 1111 1111 1111, mail a.b@example.com, call 98765 43210 on pay_A001" });
  assert.strictEqual(custom.code, 200);
  const stored = r.kv["acp:live:sim:job:" + custom.body.id];
  for (const leak of ["4111 1111 1111 1111", "a.b@example.com", "98765 43210"]) assert.ok(!stored.includes(leak), "masked: " + leak);
  assert.ok(stored.includes("[card]") && stored.includes("pay_A001"));

  // --- limits: ten an hour each (another person is unaffected), and a full queue
  r = redis();
  for (let i = 0; i < 10; i++) assert.strictEqual((await post({ tenant: "m_acme", preset: "over-refund" }, viewer)).code, 200);
  assert.strictEqual((await post({ tenant: "m_acme", preset: "over-refund" }, viewer)).code, 429);
  assert.strictEqual((await post({ tenant: "m_acme", preset: "over-refund" }, admin)).code, 200);
  r = redis({ queueLen: 20 });
  assert.strictEqual((await post({ tenant: "m_acme", preset: "over-refund" })).code, 429);

  // --- only the allowed commands, with the write token for writes and the read token for the user check
  r = redis();
  await post({ tenant: "m_acme", preset: "cross-tenant" });
  for (const s of r.sent.filter((x) => x.ops)) {
    if (s.auth === "Bearer RW") for (const op of s.ops) assert.ok(["SET", "LPUSH", "LTRIM", "EXPIRE", "LLEN", "INCR"].includes(op), "write op " + op);
    else assert.strictEqual(s.auth, "Bearer RO");
  }

  // --- the worker ping, when configured, uses the simulator event type
  process.env.GITHUB_DISPATCH_TOKEN = "ghp_test"; process.env.GITHUB_REPO = "owner/repo";
  r = redis();
  assert.strictEqual((await post({ tenant: "m_acme", preset: "cross-tenant" })).body.worker_pinged, true);
  assert.deepStrictEqual(r.sent.find((x) => x.gh).gh, { event_type: "acp-sim" });
  // --- and when it cannot ping, the answer says why with a short code (never a secret), so a misconfiguration is visible
  const why = async (setup, ghStatus = 204) => {
    r = redis(); setup();
    const base = global.fetch; let headers = null;
    global.fetch = async (url, opts) => { if (String(url).startsWith("https://api.github.com")) { headers = opts.headers; return { status: ghStatus }; } return base(url, opts); };
    const out = (await post({ tenant: "m_acme", preset: "cross-tenant" })).body; global.fetch = base;
    assert.ok(!JSON.stringify(out).includes("ghp_test"), "no secret in the response");
    return { ping: out.worker_ping, pinged: out.worker_pinged, headers };
  };
  const set = (t, repo) => () => { if (t) process.env.GITHUB_DISPATCH_TOKEN = t; else delete process.env.GITHUB_DISPATCH_TOKEN; if (repo) process.env.GITHUB_REPO = repo; else delete process.env.GITHUB_REPO; };
  assert.strictEqual((await why(set(null, "owner/repo"))).ping, "no_token");
  assert.strictEqual((await why(set("ghp_test", null))).ping, "bad_repo");
  assert.strictEqual((await why(set("ghp_test", "owner/repo\n"))).ping, "bad_repo", "a trailing newline in the variable is caught");
  assert.strictEqual((await why(set("ghp_test", "owner/repo"), 401)).ping, "http_401");
  assert.strictEqual((await why(set("ghp_test", "owner/repo"), 404)).ping, "http_404");
  const okRun = await why(set("ghp_test", "owner/repo"));
  assert.deepStrictEqual([okRun.ping, okRun.pinged], ["ok", true]);
  assert.ok(okRun.headers["User-Agent"], "GitHub rejects requests without a User-Agent");
  r = redis(); set("ghp_test", "owner/repo")(); r.kv["acp:live:dispatch"] = "1";
  assert.strictEqual((await post({ tenant: "m_acme", preset: "cross-tenant" })).body.worker_ping, "throttled");
  delete process.env.GITHUB_DISPATCH_TOKEN; delete process.env.GITHUB_REPO;

  // --- the feed: login required; raw model answers and the requester's email stay hidden from viewers
  r = redis();
  const job = JSON.stringify({ id: "sim_0123456789abcdef", kind: "preset", preset: "over-refund", tenant: "m_acme", state: "done", by: "vic@example.com", title: "Over-refund", prompt: "Refund ₹50,000 on pay_A001.", steps: [],
    runs: { guarded: { mode: "guarded", matrix: [], outcome: "contained", events: [], answer_raw: "RAW MODEL TEXT", answer_shown: "ok", latency_ms: 3, cost_usd: 0 } }, verdict: { guarded: "contained", unguarded: "breach" } });
  r.kv["acp:live:sim:job:sim_0123456789abcdef"] = job; r.lists["acp:live:sim:jobs"] = ["sim_0123456789abcdef", "../../evil"];
  assert.strictEqual((await call(data, { method: "GET", query: { file: "sim" } })).code, 401);
  const v = await call(data, { method: "GET", headers: viewer, query: { file: "sim" } });
  assert.strictEqual(v.code, 200); assert.strictEqual(v.body.jobs.length, 1, "a malformed id in the list is skipped");
  assert.ok(!JSON.stringify(v.body).includes("RAW MODEL TEXT") && v.body.jobs[0].by === null);
  const a = await call(data, { method: "GET", headers: admin, query: { file: "sim" } });
  assert.strictEqual(a.body.jobs[0].by, "vic@example.com");
  // no store configured: an account session cannot be checked, so it is refused (fail closed), and nothing can be queued
  delete process.env.KV_REST_API_URL;
  assert.strictEqual((await call(data, { method: "GET", headers: admin, query: { file: "sim" } })).code, 401);
  assert.strictEqual((await post({ tenant: "m_acme", preset: "cross-tenant" })).code, 401);

  console.log("dashboard simulator: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
