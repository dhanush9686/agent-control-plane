// The live-data path of the dashboard function: login gate, read-only Redis access, graceful failures. Run by pytest.
const assert = require("assert");
const path = require("path");
const api = (f) => path.join(__dirname, "..", "dashboard", "api", f);
const PW = "correct horse battery";
process.env.DASHBOARD_PASSWORD = PW;

function call(handler, { headers = {}, query = {} } = {}) {
  return new Promise((resolve) => {
    const res = { headers: {}, code: 200 };
    res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; };
    res.status = (c) => { res.code = c; return res; };
    res.json = (o) => { res.body = o; resolve(res); return res; };
    Promise.resolve(handler({ method: "GET", headers, query }, res));
  });
}

const ENV_KEYS = ["UPSTASH_REDIS_REST_URL", "KV_REST_API_URL", "KV_REST_API_READ_ONLY_TOKEN", "UPSTASH_REDIS_REST_READ_ONLY_TOKEN", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN"];
const clearEnv = () => ENV_KEYS.forEach((k) => delete process.env[k]);

function fakeRedis(store, { status = 200 } = {}) {
  const sent = [];
  global.fetch = async (url, opts) => {
    const cmds = JSON.parse(opts.body);
    sent.push({ url, auth: opts.headers.Authorization, cmds });
    if (status !== 200) return { ok: false, status, json: async () => ({}) };
    const out = cmds.map(([op, key]) => ({ result: op === "GET" ? (store.kv[key] ?? null) : (store.lists[key] ?? []) }));
    return { ok: true, status: 200, json: async () => out };
  };
  return sent;
}

(async () => {
  const auth = require(api("_auth"));
  const data = require(api("data"));
  const good = "acp_session=" + auth.issue(PW);
  const bundle = require(api("_data"));
  const store = {
    kv: {
      "acp:live:latest": JSON.stringify({ finished_at: "2026-11-05T11:00:00+00:00", sources: {} }),
      "acp:live:snapshot": JSON.stringify({ payments: 8 }),
      "acp:live:audit": "not json {{{",
    },
    lists: { "acp:live:runs": [JSON.stringify({ run_id: "b" }), JSON.stringify({ run_id: "a" }), "garbage"], "acp:live:history": [] },
  };

  // --- login gate comes first: no cookie, no Redis call
  clearEnv();
  process.env.KV_REST_API_URL = "https://x.upstash.io";
  process.env.KV_REST_API_READ_ONLY_TOKEN = "RO-token";
  let sent = fakeRedis(store);
  const anon = await call(data, { query: { file: "live" } });
  assert.strictEqual(anon.code, 401);
  assert.strictEqual(sent.length, 0, "an unauthenticated request must not touch Redis");

  // --- configured: serves parsed values, newest first, using ONLY read commands and the read-only token
  const ok = await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(ok.code, 200);
  assert.strictEqual(ok.body.via, "redis");
  assert.deepStrictEqual(ok.body.runs.map((r) => r.run_id), ["b", "a"], "garbage list items are skipped");
  assert.strictEqual(ok.body.audit, null, "an unparseable value becomes null, not a 500");
  assert.strictEqual(ok.body.snapshot.payments, 8);
  assert.ok(/no-store/.test(ok.headers["cache-control"]));
  assert.strictEqual(sent.length, 1);
  assert.strictEqual(sent[0].auth, "Bearer RO-token");
  for (const [op] of sent[0].cmds) assert.ok(["GET", "LRANGE"].includes(op), "only read commands: " + op);

  // --- a WRITE token alone is never used
  clearEnv();
  process.env.KV_REST_API_URL = "https://x.upstash.io";
  process.env.KV_REST_API_TOKEN = "WRITE-token";
  sent = fakeRedis(store);
  const noRO = await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(sent.length, 0, "the write token must never be used by the dashboard");
  assert.ok(noRO.code === 404 || noRO.body.via === "bundle");

  // --- plain-http URL or missing config -> not configured
  clearEnv();
  process.env.KV_REST_API_URL = "http://x.upstash.io";
  process.env.KV_REST_API_READ_ONLY_TOKEN = "RO-token";
  sent = fakeRedis(store);
  await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(sent.length, 0, "an insecure URL is refused");

  // --- Redis down: 502 with no details, never the token
  clearEnv();
  process.env.KV_REST_API_URL = "https://x.upstash.io";
  process.env.KV_REST_API_READ_ONLY_TOKEN = "RO-token-secret";
  fakeRedis(store, { status: 500 });
  const down = await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(down.code, 502);
  assert.ok(!JSON.stringify(down.body).includes("RO-token-secret"));
  global.fetch = async () => { throw new Error("connect ECONNREFUSED with RO-token-secret"); };
  const boom = await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(boom.code, 502);
  assert.ok(!JSON.stringify(boom.body).includes("RO-token-secret"));

  // --- not configured and nothing bundled -> a clear 404; bundled files keep working
  clearEnv();
  fakeRedis(store);
  const bundled = Object.prototype.hasOwnProperty.call(bundle, "live");
  const none = await call(data, { headers: { cookie: good }, query: { file: "live" } });
  assert.strictEqual(none.code, bundled ? 200 : 404);
  if (bundled) assert.strictEqual(none.body.via, "bundle");
  const still = await call(data, { headers: { cookie: good }, query: { file: "assure" } });
  assert.strictEqual(still.code, 200);
  assert.deepStrictEqual(still.body, bundle.assure);

  console.log("dashboard live data: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
