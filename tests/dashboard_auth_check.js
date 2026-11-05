// Exercises the dashboard's serverless login functions with fake request/response objects. Run by pytest.
const assert = require("assert");
const path = require("path");
const api = (f) => path.join(__dirname, "..", "dashboard", "api", f);

process.env.ACP_LOGIN_DELAY_MS = "1"; // do not slow the test down
const PW = "correct horse battery";

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

(async () => {
  const auth = require(api("_auth"));
  const login = require(api("login"));
  const logout = require(api("logout"));
  const data = require(api("data"));
  const bundle = require(api("_data"));

  // --- not configured: fail closed
  delete process.env.DASHBOARD_PASSWORD;
  assert.strictEqual((await call(login, { method: "POST", body: { password: "anything12345" } })).code, 503);
  assert.strictEqual((await call(data, { query: { file: "assure" } })).code, 401);
  process.env.DASHBOARD_PASSWORD = "short";
  assert.strictEqual((await call(login, { method: "POST", body: { password: "short" } })).code, 503, "short password is rejected");

  process.env.DASHBOARD_PASSWORD = PW;

  // --- login
  assert.strictEqual((await call(login, { method: "GET" })).code, 405);
  for (const bad of [{ password: "wrong" }, {}, { password: 12345 }, { password: "" }, null, "not json{"]) {
    const r = await call(login, { method: "POST", body: bad });
    assert.strictEqual(r.code, 401, "bad credentials must be refused: " + JSON.stringify(bad));
    assert.ok(!r.headers["set-cookie"], "no cookie on failure");
  }
  const ok = await call(login, { method: "POST", body: { password: PW } });
  assert.strictEqual(ok.code, 200);
  const raw = ok.headers["set-cookie"];
  for (const flag of ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=28800"]) assert.ok(raw.includes(flag), "cookie flag " + flag);
  assert.ok(!raw.includes(PW), "the password is never in the cookie");
  const good = cookieOf(ok);

  // --- data is behind the cookie
  const unauth = await call(data, { query: { file: "assure" } });
  assert.strictEqual(unauth.code, 401);
  assert.ok(!JSON.stringify(unauth.body).includes("real_run"), "no data leaks in the 401");
  const authed = await call(data, { headers: { cookie: good }, query: { file: "assure" } });
  assert.strictEqual(authed.code, 200);
  assert.deepStrictEqual(authed.body, bundle.assure);
  assert.ok(/no-store/.test(authed.headers["cache-control"]));

  // --- forged, tampered, expired, other-password and malformed sessions
  const [exp, sig] = good.split("=")[1].split(".");
  const forged = [
    "acp_session=" + exp + ".deadbeef",
    "acp_session=" + (Number(exp) + 99999) + "." + sig, // extended expiry, old signature
    "acp_session=" + auth.issue("another password entirely"),
    "acp_session=" + (Math.floor(Date.now() / 1000) - 10) + "." + "0".repeat(64),
    "acp_session=garbage", "acp_session=", "other=" + good.split("=")[1],
  ];
  for (const c of forged) assert.strictEqual((await call(data, { headers: { cookie: c }, query: { file: "assure" } })).code, 401, c);
  // a genuinely expired but correctly signed token
  assert.strictEqual(auth.verify(auth.issue(PW, Date.now() - 9 * 3600 * 1000), PW), false, "expired token refused");
  assert.strictEqual(auth.verify(auth.issue(PW), PW), true);

  // --- changing the password kills every existing session
  process.env.DASHBOARD_PASSWORD = "a brand new password";
  assert.strictEqual((await call(data, { headers: { cookie: good }, query: { file: "assure" } })).code, 401);
  process.env.DASHBOARD_PASSWORD = PW;

  // --- unknown names and path tricks
  for (const f of ["nope", "../_auth", "..%2f_auth", "__proto__", "constructor", "toString", "", "assure/../traces"]) {
    assert.strictEqual((await call(data, { headers: { cookie: good }, query: { file: f } })).code, 404, "refused: " + f);
  }
  // every file in the bundle is reachable when logged in
  for (const name of Object.keys(bundle)) assert.strictEqual((await call(data, { headers: { cookie: good }, query: { file: name } })).code, 200);

  // --- logout clears the cookie
  const out = await call(logout, { method: "POST" });
  assert.ok(out.headers["set-cookie"].includes("Max-Age=0"));
  assert.strictEqual((await call(logout, { method: "GET" })).code, 405);

  console.log("dashboard auth: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
