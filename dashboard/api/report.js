// GET /api/report?format=html|json|csv[&inline=1] -> the compliance report the worker last published. Login required.
// The worker builds the report next to the audit log and stores it; this only reads it back and hands it over as a download.
const users = require("./_users");
const { readReportFile, REPORT_FORMATS } = require("./_redis");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "private, no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    return res.status(405).json({ error: "Use GET." });
  }
  const session = await users.authenticate(req);
  if (!session) return res.status(401).json({ error: "Login required." });
  const q = req.query || {};
  const format = String(q.format || "html");
  if (!Object.prototype.hasOwnProperty.call(REPORT_FORMATS, format)) return res.status(400).json({ error: "Choose html, json or csv." });
  let text;
  try {
    text = await readReportFile(format);
  } catch (_) {
    return res.status(502).json({ error: "The report store could not be reached." });
  }
  if (!text) return res.status(404).json({ error: "No report has been published yet." });
  const inline = format === "html" && q.inline === "1";
  res.setHeader("Content-Type", REPORT_FORMATS[format]);
  if (format === "html") res.setHeader("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'");
  res.setHeader("Content-Disposition", `${inline ? "inline" : "attachment"}; filename="agent-audit-report.${format}"`);
  return res.status(200).send(text);
};
