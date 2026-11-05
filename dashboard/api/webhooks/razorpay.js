// POST /api/webhooks/razorpay: called by Razorpay's servers, not by a browser. The signature is the only credential;
// there is no login. Needs the raw request body, so Vercel's JSON parsing is switched off for this route.
const { ingest } = require("../_events");

const MAX_BYTES = 100_000;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let n = 0;
    req.on("data", (c) => {
      n += c.length;
      if (n > MAX_BYTES) { reject(new Error("too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Use POST." });
  }
  let raw;
  try { raw = await readBody(req); } catch (_) { return res.status(413).json({ error: "Body too large." }); }
  try {
    const { code, body } = await ingest(raw, req.headers);
    return res.status(code).json(body);
  } catch (_) {
    return res.status(502).json({ error: "Could not store the event." }); // never echo details; Razorpay will retry
  }
};
module.exports.config = { api: { bodyParser: false } };
