import { useNavigate, useParams } from "react-router";
import { useReady } from "@/lib/data";
import { ageText, freshness, ms, pct, pct1 } from "@/lib/format";
import { SOURCE_LABEL } from "@/lib/vocab";
import { Badge, Card, Disclosure, Empty, GraphKey, Legend, PageHeader, SectionTitle, Segmented, Stat } from "@/components/ui";
import { ChainView, LineageGraph, RunBadges, RunItem, TrendChart } from "@/components/domain";

export default function LiveCheck() {
  const { live } = useReady();
  const { source } = useParams();
  const navigate = useNavigate();
  if (!live || !live.latest) return <><PageHeader title="Live check" /><Empty>{live?.error ?? "No live check yet."}</Empty></>;

  const L = live.latest, sources = Object.keys(L.sources);
  const src = source && sources.includes(source) ? source : sources[0];
  const S = L.sources[src].summary, g = S.guarded, u = S.unguarded;
  const fr = freshness(L.finished_at, L.expected_interval_hours, live.via);
  const snap = live.snapshot ?? L.snapshot, a = live.audit;
  const runs = (live.runs ?? []).filter((r) => r.source === src && (r.attempted.length || !r.passed || r.withheld));

  return (
    <div className="space-y-6">
      <PageHeader title="Live check" lead="A fresh run on test-mode payments."
        right={<div className="flex flex-wrap items-center gap-3">
          <Segmented label="Choose a data source" value={src} onChange={(k) => navigate(`/app/live/${k}`)} items={sources.map((k) => [k, SOURCE_LABEL[k] ?? k] as [string, string])} />
          <Badge tone={fr.tone} dot>{fr.text} · updated {ageText(L.finished_at)}</Badge>
        </div>} />

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat num={`${g.critical_effective.k}/${g.critical_effective.n}`} tone="good" cap="serious mistakes with guardrails." fine={`95% interval ${pct1(g.critical_effective.ci95[0])}–${pct1(g.critical_effective.ci95[1])}`} />
        <Stat num={`${u.critical_effective.k}/${u.critical_effective.n}`} tone={u.critical_effective.k ? "bad" : ""} cap="without guardrails." />
        <Stat num={pct(g.pass.rate)} cap="of tests passed with guardrails." fine={`${pct(u.pass.rate)} without · ${ms(g.avg_latency_ms)} per task`} />
      </div>

      <Card>
        <SectionTitle>Where this came from</SectionTitle>
        <LineageGraph live={live} latest={L} src={src} />
        {snap && src === "razorpay-test" && (
          <p className="mt-4 text-xs text-faint">
            {Object.entries(snap.payments_by_tenant ?? {}).map(([k, v]) => k + ": " + v).join(", ")} · {snap.refunds} refunds
          </p>
        )}
      </Card>

      {live.history && <TrendSection history={live.history} src={src} />}

      <Card>
        <SectionTitle>Runs that mattered</SectionTitle>
        {runs.length ? <div>{runs.slice(0, 40).map((r, i) => <Disclosure key={i} summary={<RunBadges r={r} />}><RunItem r={r} /></Disclosure>)}</div> : <p className="text-sm text-muted">Nothing unsafe or failed in this source.</p>}
        <GraphKey />
      </Card>

      {a && (
        <Card>
          <SectionTitle right={<Badge tone={a.chain_ok ? "pass" : "fail"} dot>{a.chain_ok ? `${a.total_rows} entries, intact` : "Broken"}</Badge>}>Audit chain</SectionTitle>
          <ChainView rows={a.rows.slice(-8)} />
        </Card>
      )}
    </div>
  );
}

function TrendSection({ history, src }: { history: NonNullable<ReturnType<typeof useReady>["live"]>["history"]; src: string }) {
  if (!history || history.filter((h) => h.sources?.[src]).length < 2) return null; // a trend needs two checks
  return <Card><SectionTitle>Over time</SectionTitle><Legend /><TrendChart history={history} src={src} /></Card>;
}
