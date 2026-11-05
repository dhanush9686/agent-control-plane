import { useReady } from "@/lib/data";
import { Card, Disclosure, PageHeader, TableWrap } from "@/components/ui";
import { ChainView } from "@/components/domain";

export default function Audit() {
  const { assure } = useReady();
  const a = assure.audit_demo;
  return (
    <div className="space-y-6">
      <PageHeader title="Audit trail" lead="Edit one entry and the chain breaks." />
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
