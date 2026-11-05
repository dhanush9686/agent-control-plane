import { useMemo, useState } from "react";
import { useReady } from "@/lib/data";
import { SOURCE_LABEL } from "@/lib/vocab";
import { Card, Disclosure, Empty, GraphKey, PageHeader, Segmented } from "@/components/ui";
import { RunBadges, RunItem } from "@/components/domain";

export default function Runs() {
  const { live } = useReady();
  const [source, setSource] = useState("all");
  const [mode, setMode] = useState("all");
  const [status, setStatus] = useState("notable");
  const [page, setPage] = useState(1);

  const all = live?.runs ?? [];
  const sources = useMemo(() => Array.from(new Set(all.map((r) => r.source))), [all]);
  const rows = all.filter((r) =>
    (source === "all" || r.source === source) && (mode === "all" || r.mode === mode) &&
    (status === "all" || (status === "notable" ? r.attempted.length || !r.passed || r.withheld : status === "failed" ? !r.passed : r.effective.length > 0)));
  const shown = rows.slice(0, page * 25);

  if (!all.length) return <><PageHeader title="Runs" /><Empty>{live?.error ?? "No runs yet."}</Empty></>;

  return (
    <div>
      <PageHeader title="Runs" lead={`${all.length} runs from the latest live check.`} />
      <div className="mb-5 flex flex-wrap gap-3">
        <Segmented label="Source" value={source} onChange={(v) => { setSource(v); setPage(1); }} items={[["all", "All sources"], ...sources.map((s) => [s, SOURCE_LABEL[s] ?? s] as [string, string])]} />
        <Segmented label="Mode" value={mode} onChange={(v) => { setMode(v); setPage(1); }} items={[["all", "Both"], ["guarded", "Guarded"], ["unguarded", "Without"]]} />
        <Segmented label="Outcome" value={status} onChange={(v) => { setStatus(v); setPage(1); }} items={[["notable", "Attempted or failed"], ["failed", "Failed"], ["leaked", "Got through"], ["all", "Everything"]]} />
      </div>
      <Card>
        <div className="mb-1 text-xs text-faint">{rows.length} run{rows.length === 1 ? "" : "s"}</div>
        {shown.length ? shown.map((r, i) => <Disclosure key={i} summary={<RunBadges r={r} />}><RunItem r={r} /></Disclosure>) : <p className="py-6 text-center text-sm text-muted">Nothing matches these filters.</p>}
        {shown.length < rows.length && <button type="button" onClick={() => setPage(page + 1)} className="mt-3 w-full rounded-lg bg-sunken py-2 text-sm font-medium hover:bg-accent-soft">Show more</button>}
        <GraphKey />
      </Card>
    </div>
  );
}
