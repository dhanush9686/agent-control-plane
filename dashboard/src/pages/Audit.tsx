import { useEffect, useState } from "react";
import { Download, FileText } from "lucide-react";
import { useReady } from "@/lib/data";
import { ageText } from "@/lib/format";
import type { ReportFeed } from "@/lib/types";
import { Badge, Card, Disclosure, SectionTitle, PageHeader, TableWrap } from "@/components/ui";
import { ChainView } from "@/components/domain";

const FORMATS = [["html", "Report (print to PDF)"], ["json", "JSON"], ["csv", "CSV"]] as const;

/* The compliance report the worker last published: what it covers, and links to view or download it. */
function ReportCard() {
  const [feed, setFeed] = useState<ReportFeed | null>(null);
  useEffect(() => {
    fetch("/api/data?file=report", { cache: "no-store", credentials: "same-origin" })
      .then((r) => (r.ok ? (r.json() as Promise<ReportFeed>) : null))
      .then(setFeed)
      .catch(() => setFeed(null));
  }, []);
  const rep = feed?.report;
  return (
    <Card>
      <SectionTitle right={rep && <Badge tone={rep.ok ? "pass" : "fail"} dot>{rep.ok ? "Integrity passed" : `Broken at #${rep.first_bad_seq}`}</Badge>}>Compliance report</SectionTitle>
      {rep ? (
        <>
          <p className="text-sm text-muted">{rep.entries_total} log entries · {rep.blocked} calls blocked · updated {ageText(rep.generated_at)}</p>
          <p className="mt-1 text-xs text-faint">Head <code className="font-mono">{rep.head.slice(0, 16)}…</code></p>
          <div className="mt-4 flex flex-wrap gap-2">
            <a href="/api/report?format=html&inline=1" target="_blank" rel="noreferrer" className="inline-flex h-10 items-center gap-2 rounded-full bg-fg px-5 text-sm font-medium text-bg"><FileText className="size-4" />View</a>
            {FORMATS.map(([f, label]) => <a key={f} href={`/api/report?format=${f}`} className="inline-flex h-10 items-center gap-2 rounded-full border border-line-strong px-4 text-sm font-medium"><Download className="size-4" />{label}</a>)}
          </div>
          <Disclosure summary="How an auditor checks it">
            <p className="text-sm leading-relaxed text-muted">Download the JSON and check it with the verifier script in the repository. It recomputes every hash from the file alone and names the first entry that does not match.</p>
          </Disclosure>
        </>
      ) : <p className="text-sm text-muted">{feed && !feed.configured ? "The report store is not configured." : "No report yet. One is published after the worker's next run."}</p>}
    </Card>
  );
}

export default function Audit() {
  const { assure } = useReady();
  const a = assure.audit_demo;
  return (
    <div className="space-y-6">
      <PageHeader title="Audit trail" lead="Edit one entry and the chain breaks." />
      <ReportCard />
      <Card>
        <ChainView rows={a.rows} editedSeq={a.edited_seq} firstBad={a.after.first_bad_seq} />
        <div className="mt-5 grid gap-4 border-t border-line pt-5 text-sm sm:grid-cols-3">
          <div><div className="text-xs font-medium uppercase tracking-wide text-faint">Recorded</div><div className="mt-1">{a.before.rows} entries, chain intact</div></div>
          <div><div className="text-xs font-medium uppercase tracking-wide text-faint">Edited</div><div className="mt-1">Entry #{a.edited_seq} changed</div></div>
          <div><div className="text-xs font-medium uppercase tracking-wide text-faint">Verified again</div><div className="mt-1 font-medium text-bad">Broken at #{a.after.first_bad_seq}</div></div>
        </div>
      </Card>
      <Card>
        <Disclosure summary="Full table">
          <TableWrap>
            <thead><tr>{["#", "Action", "Hash", "Previous"].map((t) => <th key={t}>{t}</th>)}</tr></thead>
            <tbody>{a.rows.map((r) => <tr key={r.seq}><td>{r.seq}</td><td>{r.action}</td><td className="font-mono text-xs">{r.hash}…</td><td className="font-mono text-xs">{r.prev}…</td></tr>)}</tbody>
          </TableWrap>
        </Disclosure>
      </Card>
    </div>
  );
}
