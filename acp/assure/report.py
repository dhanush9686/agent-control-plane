"""The compliance report: what the agent did, what was blocked, who approved what, and proof the log was not edited.

Analogy: the year-end statement. The audit log is the ledger book; this turns it into a document a compliance person
can read, file and check without trusting anyone's dashboard. Every row carries the hash that links it to the one
before it, and `scripts/verify_audit_export.py` recomputes the whole chain from the exported file alone.

Formats: HTML (self-contained and print-ready, so "print to PDF" gives a PDF), JSON (the full record, the format to
verify) and CSV (one row per log entry, safe to open in a spreadsheet).

Honest limits, printed in every report: tamper-evident, not tamper-proof; the newest N rows are listed while the
whole chain is verified; the data is test-mode and sample data.
"""

# ruff: noqa: E501  (the HTML template has long lines on purpose)
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from acp import store
from acp.assure import approvals as approvals_queue
from acp.assure.audit import GENESIS, AuditLog
from acp.assure.publish import PREFIX, UpstashClient
from acp.config import ROOT

SCHEMA = 1
BLOCKED_SHOWN = 100
LIMITS = [
    "The log is tamper-evident, not tamper-proof: someone who can rewrite the whole table could rebuild a consistent "
    "chain. Record the head hash below somewhere the database administrator cannot write, and compare it later.",
    "Only the newest entries are listed; the whole chain is checked and the result is stated above.",
    "Data is test-mode or sample data, handled by a model whose output is checked by rules, not by a person.",
    "Card and identity numbers are masked before anything is written to the log.",
]
HOW_TO_VERIFY = [
    "Save the JSON export (not this page).",
    "Run: python scripts/verify_audit_export.py report.json",
    "It recomputes every hash from the entries alone and prints OK, or the first entry that does not match.",
    "Rule: hash = SHA-256 of  previous_hash + '|' + canonical JSON of {ts, actor, action, run_id, detail}  "
    "(keys sorted, no spaces).",
]


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    try:
        d = json.loads(row["detail"])
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    actions: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    runs = withheld = masked = 0
    for e in entries:
        actions[e["action"]] += 1
        d = _detail(e)
        if e["action"] == "tool_call":
            decisions["allowed" if d.get("allowed") else "blocked"] += 1
            if not d.get("allowed"):
                codes[str(d.get("code"))] += 1
        elif e["action"] == "run_start":
            runs += 1
        elif e["action"] == "run_end":
            withheld += 1 if d.get("withheld") else 0
            masked += int(d.get("pii_masked") or 0)
        elif e["action"] == "approval_decided":
            decisions["approval_" + str(d.get("decision"))] += 1
    return {
        "runs": runs,
        "tool_calls": decisions["allowed"] + decisions["blocked"],
        "allowed": decisions["allowed"],
        "blocked": decisions["blocked"],
        "blocked_by_reason": dict(codes.most_common()),
        "answers_withheld": withheld,
        "items_masked": masked,
        "actions": dict(sorted(actions.items())),
    }


def build_report(
    audit: AuditLog,
    *,
    approvals: list[dict[str, Any]] | None = None,
    title: str = "Agent audit report",
    limit: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    entries = audit.entries()
    verdict = audit.verify()
    shown = entries[-limit:] if limit else entries
    blocked: list[dict[str, Any]] = []
    for e in reversed(shown):
        d = _detail(e)
        if e["action"] == "tool_call" and not d.get("allowed", True):
            blocked.append({"seq": e["seq"], "ts": e["ts"], "run_id": e["run_id"], "tool": d.get("tool"),
                            "code": d.get("code"), "args": d.get("args")})  # fmt: skip
    blocked = blocked[:BLOCKED_SHOWN]
    ap = approvals or []
    state = Counter(a.get("state", "pending") for a in ap)
    return {
        "schema": SCHEMA,
        "title": title,
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "scope": {
            "entries_total": len(entries),
            "entries_listed": len(shown),
            "first_seq": shown[0]["seq"] if shown else None,
            "last_seq": shown[-1]["seq"] if shown else None,
        },
        "integrity": {
            "ok": verdict.ok,
            "entries_checked": verdict.rows,
            "first_bad_seq": verdict.first_bad_seq,
            "reason": verdict.reason,
            "head": audit.head(),
            "anchor_prev_hash": shown[0]["prev_hash"] if shown else GENESIS,
        },
        "summary": {**summarise(entries), "approvals": dict(state)},
        "blocked": blocked,
        "approvals": ap,
        "entries": [
            {k: e[k] for k in ("seq", "ts", "actor", "action", "run_id", "detail", "prev_hash", "hash")} for e in shown
        ],
        "limits": LIMITS,
        "how_to_verify": HOW_TO_VERIFY,
    }


# ------------------------------------------------------------------ renderers
def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)


def _cell(v: Any) -> str:
    """A spreadsheet runs text that starts with = + - or @ as a formula, so neutralise those."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(report: dict[str, Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        ["seq", "time_utc", "actor", "action", "run_id", "tool", "allowed", "code", "detail", "prev_hash", "hash"]
    )
    for e in report["entries"]:
        d = _detail(e)
        row = [e["seq"], e["ts"], e["actor"], e["action"], e["run_id"], d.get("tool"), d.get("allowed"), d.get("code"),
               e["detail"], e["prev_hash"], e["hash"]]  # fmt: skip
        w.writerow([_cell(x) for x in row])
    return out.getvalue()


def _inr(paise: Any) -> str:
    try:
        return f"₹{int(paise) / 100:,.2f}"
    except (TypeError, ValueError):
        return "-"


CSS = """
:root{color-scheme:light}*{box-sizing:border-box}
body{font:14px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1d1d1f;margin:0;padding:40px 24px;background:#fff}
main{max-width:980px;margin:0 auto}h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 10px;
border-bottom:1px solid #d2d2d7;padding-bottom:6px}p{margin:6px 0}.muted{color:#6e6e73}
.banner{border-radius:10px;padding:14px 18px;margin:20px 0;font-weight:600}.ok{background:#e8f6ec;color:#17692f}
.bad{background:#fdebec;color:#b3261e}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat{border:1px solid #d2d2d7;border-radius:10px;padding:12px}.stat b{display:block;font-size:22px}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{border-bottom:1px solid #e5e5ea;padding:6px 8px;
text-align:left;vertical-align:top}th{background:#f5f5f7}code,.mono{font:11px/1.4 ui-monospace,Menlo,monospace;word-break:break-all}
ol,ul{padding-left:20px}@media print{body{padding:0}h2{break-after:avoid}tr{break-inside:avoid}}
@media(max-width:700px){.grid{grid-template-columns:repeat(2,1fr)}}
"""


def to_html(report: dict[str, Any]) -> str:
    e = html.escape
    r, s, i, sc = report, report["summary"], report["integrity"], report["scope"]

    def table(head: list[str], rows: list[list[Any]], empty: str) -> str:
        if not rows:
            return f'<p class="muted">{e(empty)}</p>'
        th = "".join(f"<th>{e(h)}</th>" for h in head)
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
        return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"

    stat = lambda n, label: f'<div class="stat"><b>{e(str(n))}</b><span class="muted">{e(label)}</span></div>'  # noqa: E731
    ok = i["ok"]
    banner = (
        f'<div class="banner ok">Integrity check passed: all {i["entries_checked"]} entries link correctly and match '
        "their hashes.</div>"
        if ok
        else f'<div class="banner bad">Integrity check FAILED at entry {e(str(i["first_bad_seq"]))}: {e(i["reason"])}.</div>'
    )
    ap = r["approvals"]
    ap_rows = []
    for a in ap:
        dec = a.get("decision") or {}
        cols = [a.get("id"), a.get("state"), a.get("tier"), a.get("payment_id"), _inr(a.get("amount_paise")),
                a.get("created_at"), dec.get("by", "-"), dec.get("note", "")]  # fmt: skip
        ap_rows.append([e(str(c if c is not None else "")) for c in cols])
    blocked_rows = [
        [e(str(b["seq"])), e(b["ts"]), e(str(b["tool"])), e(str(b["code"])), f'<span class="mono">{e(json.dumps(b["args"], ensure_ascii=False))}</span>']
        for b in r["blocked"]
    ]  # fmt: skip
    reasons = [[e(k), e(str(v))] for k, v in s["blocked_by_reason"].items()]
    log_rows = [
        [
            e(str(x["seq"])), e(x["ts"]), e(x["actor"]), e(x["action"]), f'<span class="mono">{e(x["detail"])}</span>',
            f'<span class="mono">{e(x["hash"])}</span>',
        ]
        for x in r["entries"]
    ]  # fmt: skip
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(r["title"])}</title><style>{CSS}</style></head><body><main>
<h1>{e(r["title"])}</h1>
<p class="muted">Generated {e(r["generated_at"])} (UTC). Entries {e(str(sc["first_seq"]))} to {e(str(sc["last_seq"]))} of {e(str(sc["entries_total"]))}.</p>
{banner}
<p>Head hash (record this to detect later changes): <code>{e(i["head"])}</code></p>

<h2>Summary</h2>
<div class="grid">{stat(s["runs"], "agent runs")}{stat(s["tool_calls"], "tool calls")}{stat(s["blocked"], "calls blocked")}{stat(s["allowed"], "calls allowed")}</div>
<div class="grid" style="margin-top:10px">{stat(s["answers_withheld"], "answers withheld")}{stat(s["items_masked"], "items masked")}{stat(s["approvals"].get("pending", 0), "approvals pending")}{stat(s["approvals"].get("approved", 0) + s["approvals"].get("rejected", 0), "approvals decided")}</div>

<h2>Why calls were blocked</h2>
{table(["Reason", "Count"], reasons, "Nothing was blocked in this period.")}

<h2>Approvals</h2>
<p class="muted">Refunds the agent drafted. A person approves or rejects each one; no money moves on this site.</p>
{table(["Id", "State", "Tier", "Payment", "Amount", "Requested", "Decided by", "Note"], ap_rows, "No approvals in this period.")}

<h2>Blocked calls (newest first)</h2>
{table(["#", "Time (UTC)", "Tool", "Reason", "Arguments (masked)"], blocked_rows, "No blocked calls among the listed entries.")}

<h2>Full log</h2>
<p class="muted">Each hash covers the entry and the hash before it. The first listed entry follows <code>{e(i["anchor_prev_hash"])}</code>.</p>
{table(["#", "Time (UTC)", "Actor", "Action", "Detail", "Hash"], log_rows, "The log is empty.")}

<h2>How to verify this report</h2>
<ol>{"".join(f"<li>{e(t)}</li>" for t in r["how_to_verify"])}</ol>

<h2>Limits</h2>
<ul>{"".join(f"<li>{e(t)}</li>" for t in r["limits"])}</ul>
</main></body></html>
"""


RENDERERS = {"html": to_html, "json": to_json, "csv": to_csv}
MAX_BYTES = 380_000  # each stored copy stays well inside the Redis request limit


def render_all(
    audit: AuditLog,
    approvals: list[dict[str, Any]] | None = None,
    *,
    title: str = "Agent audit report",
    limit: int = 300,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the report and all three renderings, listing fewer entries until each fits the size limit."""
    while True:
        report = build_report(audit, approvals=approvals, title=title, limit=limit)
        files = {fmt: fn(report) for fmt, fn in RENDERERS.items()}
        if limit <= 20 or all(len(v.encode()) <= MAX_BYTES for v in files.values()):
            return report, files
        limit = max(20, limit // 2)


KEY_REPORT = PREFIX + "report:"
REPORT_TTL_S = 30 * 24 * 3600


def publish_report(client: UpstashClient, audit: AuditLog, *, force: bool = False) -> bool:
    """Store the three renderings where the dashboard can serve them. Returns False when nothing changed since last time.

    The dashboard only ever reads these; it never builds a report itself, so the chain check always runs here, next to
    the log, and the HTML, JSON and CSV a person downloads are byte-for-byte what the worker produced.
    """
    aps = approvals_queue.read_all(client)
    state = [audit.head(), [(a["id"], a["state"], bool(a.get("audit"))) for a in aps]]
    fingerprint = hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]
    meta = client.get_json(KEY_REPORT + "meta")
    if not force and isinstance(meta, dict) and meta.get("fingerprint") == fingerprint:
        return False
    report, files = render_all(audit, aps)
    new_meta = {
        "generated_at": report["generated_at"],
        "fingerprint": fingerprint,
        "head": report["integrity"]["head"],
        "ok": report["integrity"]["ok"],
        "first_bad_seq": report["integrity"]["first_bad_seq"],
        "entries_total": report["scope"]["entries_total"],
        "entries_listed": report["scope"]["entries_listed"],
        "approvals": report["summary"]["approvals"],
        "blocked": report["summary"]["blocked"],
        "bytes": {fmt: len(text.encode()) for fmt, text in files.items()},
    }
    client.pipeline(
        [["SET", KEY_REPORT + fmt, text, "EX", REPORT_TTL_S] for fmt, text in files.items()]
        + [["SET", KEY_REPORT + "meta", json.dumps(new_meta), "EX", REPORT_TTL_S]]
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m acp.assure.report", description="Export the audit log as a report.")
    ap.add_argument("--db", default=str(ROOT / "results" / "ingest-audit.db"))
    ap.add_argument("--format", choices=sorted(RENDERERS), default="html")
    ap.add_argument("--out", default=None, help="file to write (default: print to the terminal)")
    ap.add_argument("--limit", type=int, default=300, help="list the newest N entries (0 lists all)")
    ap.add_argument("--title", default="Agent audit report")
    args = ap.parse_args(argv)
    report = build_report(AuditLog(store.connect(args.db)), title=args.title, limit=args.limit)
    text = RENDERERS[args.format](report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out} ({report['scope']['entries_listed']} of {report['scope']['entries_total']} entries; "
              f"chain {'OK' if report['integrity']['ok'] else 'BROKEN'})")  # fmt: skip
    else:
        sys.stdout.write(text)
    return 0 if report["integrity"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
