// The approvals API and the report download: who may decide, first decision wins, what the browser can see, and which
// Redis commands and tokens are used.
const assert = require("assert");
const path = require("path");
const api = (f) => path.join(__dirname, "..", "dashboard", "api", f);
process.env.ACP_AUTH_CACHE_MS = "0";
process.env.SESSION_SECRET = "a-long-session-secret-for-tests";
process.env.DASHBOARD_PASSWORD = "the shared bootstrap password";

function call(handler, { method = "POST", body, headers = {}, query = {} } = {}) {
  return new Promise((resolve) => {
    const res = { headers: {}, code: 200 };
    res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; };
    res.status = (c) => { res.code = c; return res; };
    res.json = (o) => { res.body = o; resolve(res); return res; };
    res.send = (t) => { res.text = t; resolve(res); return res; };
    Promise.resolve(handler({ method, body, headers, query }, res));
  });
}

const ID_A = "apr_" + "a".repeat(16), ID_S = "apr_" + "5".repeat(16), ID_X = "apr_" + "9".repeat(16);
function redis() {
  const kv = {}, lists = {}, sent = [];
  for (const [email, role] of [["ada@example.com", "admin"], ["vic@example.com", "viewer"]]) kv["acp:user:" + email] = JSON.stringify({ email, name: email.split("@")[0], role, disabled: false });
  kv["acp:live:approval:" + ID_A] = JSON.stringify({ id: ID_A, tenant: "m_acme", payment_id: "pay_A001", amount_paise: 50000, reason: "duplicate", tier: "standard", source: { kind: "sim", id: "sim_1", title: "Legitimate refund" }, created_at: "2026-11-05T10:00:00+00:00", secret_extra: "never shown" });
  kv["acp:live:approval:" + ID_S] = JSON.stringify({ id: ID_S, tenant: "m_acme", payment_id: "pay_A002", amount_paise: 2000000, reason: "", tier: "senior", source: { kind: "event", id: "evt_1", title: "pay_A002" }, created_at: "2026-11-05T10:01:00+00:00" });
  lists["acp:live:approvals"] = [ID_S, ID_A, "not-an-id"];
  kv["acp:live:report:meta"] = JSON.stringify({ generated_at: "2026-11-05T11:00:00+00:00", ok: true, entries_total: 7, head: "ab".repeat(32) });
  kv["acp:live:report:html"] = "<html>report</html>"; kv["acp:live:report:json"] = '{"schema":1}'; kv["acp:live:report:csv"] = "seq,time\n1,x\n";
  global.fetch = async (url, opts) => {
    const cmds = JSON.parse(opts.body);
    sent.push({ auth: opts.headers.Authorization, ops: cmds.map((c) => c[0]) });
    const out = cmds.map((c) => {
      const [op, key] = c;
      if (op === "GET") return { result: kv[key] ?? null };
      if (op === "SET") { if (c.includes("NX") && key in kv) return { result: null }; kv[key] = c[2]; return { result: "OK" }; }
      if (op === "MGET") return { result: c.slice(1).map((k) => kv[k] ?? null) };
      if (op === "LRANGE") return { result: (lists[key] || []).slice(c[2], c[3] + 1) };
      return { error: "unsupported " + op };
    });
    return { ok: true, status: 200, json: async () => out };
  };
  return { kv, sent };
}
const env = () => { process.env.KV_REST_API_URL = "https://x.upstash.io"; process.env.KV_REST_API_READ_ONLY_TOKEN = "RO"; process.env.KV_REST_API_TOKEN = "RW"; };

(async () => {
  const approvals = require(api("approvals")), report = require(api("report")), data = require(api("data")), auth = require(api("_auth"));
  env();
  const cookie = (email, role) => "acp_session=" + auth.issueUser({ email, role, name: email.split("@")[0] });
  const admin = { cookie: cookie("ada@example.com", "admin") }, viewer = { cookie: cookie("vic@example.com", "viewer") };
  const post = (body, headers = admin) => call(approvals, { body, headers });
  const feed = async (file, headers = viewer) => (await call(data, { method: "GET", headers, query: { file } })).body;

  // --- gate
  let r = redis();
  assert.strictEqual((await post({ id: ID_A, decision: "approve" }, {})).code, 401);
  assert.strictEqual(r.sent.length, 0, "no login, no Redis call");
  assert.strictEqual((await call(approvals, { method: "GET", headers: admin })).code, 405);
  assert.strictEqual((await post({ id: ID_A, decision: "approve" }, viewer)).code, 403, "a viewer cannot decide");
  assert(r.sent.every((s) => s.auth === "Bearer RO"), "a refused viewer never uses the write token");
  assert.strictEqual((await post({ id: "apr_nope", decision: "approve" })).code, 400);
  assert.strictEqual((await post({ id: ID_A, decision: "maybe" })).code, 400);
  assert.strictEqual((await post({ id: ID_X, decision: "approve" })).code, 404, "an unknown or expired approval");
  assert.strictEqual((await post({ id: ID_A })).code, 400);

  // --- a decision: recorded once, by name, first one wins
  r = redis();
  let x = await post({ id: ID_A, decision: "approve", note: "duplicate confirmed, card 4111 1111 1111 1111 on file" });
  assert.strictEqual(x.code, 200); assert.deepStrictEqual(x.body, { ok: true, state: "approved" });
  const stored = JSON.parse(r.kv["acp:live:approval:" + ID_A + ":decision"]);
  assert.strictEqual(stored.decision, "approved"); assert.strictEqual(stored.by, "ada");
  assert(!JSON.stringify(stored).includes("4111"), "card numbers in the note are masked"); assert(!JSON.stringify(stored).includes("@"), "no email stored");
  x = await post({ id: ID_A, decision: "reject" });
  assert.strictEqual(x.code, 409, "the second decision is refused");
  assert.strictEqual(JSON.parse(r.kv["acp:live:approval:" + ID_A + ":decision"]).decision, "approved", "and does not overwrite the first");

  // --- a senior approval needs a reason; rejecting does not
  x = await post({ id: ID_S, decision: "approve" });
  assert.strictEqual(x.code, 400); assert(/written reason/.test(x.body.error));
  assert.strictEqual((await post({ id: ID_S, decision: "approve", note: "ok" })).code, 400, "too short");
  assert.strictEqual((await post({ id: ID_S, decision: "reject" })).code, 200);

  // --- only the write token, only GET and SET, for decisions; reads use the read-only token
  const writes = r.sent.filter((s) => s.auth === "Bearer RW");
  assert(writes.length > 0 && writes.every((s) => s.ops.every((o) => o === "GET" || o === "SET")), JSON.stringify(writes));
  r = redis();
  const list = await feed("approvals");
  assert(r.sent.every((s) => s.auth === "Bearer RO" && s.ops.every((o) => ["GET", "LRANGE", "MGET"].includes(o))), "the feed reads with the read-only token, read commands only");

  // --- the feed: valid ids only, merged state, nothing extra
  assert.strictEqual(list.configured, true); assert.strictEqual(list.approvals.length, 2);
  const [first, second] = list.approvals;
  assert.strictEqual(first.id, ID_S); assert.strictEqual(first.tier, "senior"); assert.strictEqual(first.state, "pending");
  assert.strictEqual(second.id, ID_A); assert.strictEqual(second.amount_paise, 50000); assert.strictEqual(second.decision, null);
  assert(!JSON.stringify(list).includes("never shown"), "unlisted fields are not passed on");
  r.kv["acp:live:approval:" + ID_A + ":decision"] = JSON.stringify({ decision: "approved", by: "Priya", note: "ok", at: "2026-11-05T10:05:00+00:00" });
  r.kv["acp:live:approval:" + ID_A + ":audit"] = JSON.stringify({ hash: "f".repeat(64) });
  const after = (await feed("approvals")).approvals[1];
  assert.strictEqual(after.state, "approved"); assert.strictEqual(after.decision.by, "Priya"); assert.strictEqual(after.audit.hash, "f".repeat(64));
  assert.strictEqual((await call(data, { method: "GET", headers: {}, query: { file: "approvals" } })).code, 401);

  // --- not configured: honest empty answer, never an error
  delete process.env.KV_REST_API_URL;
  const rd = require(api("_redis")), ap = require(api("_approvals"));
  assert.deepStrictEqual(await rd.readApprovals(), { via: "none", configured: false, approvals: [] });
  assert.deepStrictEqual(await rd.readReportMeta(), { via: "none", configured: false, report: null });
  assert.strictEqual((await ap.decide({ id: ID_A, decision: "approve" }, { role: "admin", name: "ada" })).code, 503);
  env(); r = redis();

  // --- the report: login, formats, download headers, safe html
  assert.strictEqual((await call(report, { method: "GET", headers: {} })).code, 401);
  assert.strictEqual((await call(report, { method: "POST", headers: viewer })).code, 405);
  assert.strictEqual((await call(report, { method: "GET", headers: viewer, query: { format: "pdf" } })).code, 400);
  const html = await call(report, { method: "GET", headers: viewer, query: { format: "html" } });
  assert.strictEqual(html.code, 200); assert.strictEqual(html.text, "<html>report</html>");
  assert(/^text\/html/.test(html.headers["content-type"])); assert(/^attachment; filename="agent-audit-report\.html"/.test(html.headers["content-disposition"]));
  assert(/default-src 'none'/.test(html.headers["content-security-policy"]), "the report page cannot run scripts or load anything");
  assert.strictEqual(html.headers["x-content-type-options"], "nosniff");
  assert(/^inline/.test((await call(report, { method: "GET", headers: viewer, query: { format: "html", inline: "1" } })).headers["content-disposition"]));
  assert(/^attachment/.test((await call(report, { method: "GET", headers: viewer, query: { format: "json", inline: "1" } })).headers["content-disposition"]), "only html can be shown inline");
  const csv = await call(report, { method: "GET", headers: viewer, query: { format: "csv" } });
  assert.strictEqual(csv.text, "seq,time\n1,x\n"); assert(/^text\/csv/.test(csv.headers["content-type"]));
  assert.strictEqual((await call(report, { method: "GET", headers: viewer })).code, 200, "html is the default");
  const meta = await feed("report");
  assert.strictEqual(meta.report.entries_total, 7); assert.strictEqual(meta.report.ok, true);
  delete r.kv["acp:live:report:json"];
  assert.strictEqual((await call(report, { method: "GET", headers: viewer, query: { format: "json" } })).code, 404, "nothing published yet");

  console.log("approvals and report: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
