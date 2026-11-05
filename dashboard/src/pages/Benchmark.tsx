import { useReady } from "@/lib/data";
import { pct, pct1, shortModel } from "@/lib/format";
import { catName, kindName } from "@/lib/vocab";
import { Card, Disclosure, Legend, PageHeader, PairBars, SectionTitle, Stat, TableWrap } from "@/components/ui";

export default function Benchmark() {
  const { assure } = useReady();
  const R = assure.real_run, g = R.summary.guarded, u = R.summary.unguarded;
  const kinds = Object.keys(g.by_kind);
  const max = Math.max(...kinds.map((k) => Math.max(u.by_kind[k].effective, g.by_kind[k].effective)), 1);
  return (
    <div className="space-y-6">
      <PageHeader title="Benchmark" lead={`${g.n / R.repeats} scenarios, run ${R.repeats} times each, with and without guardrails. ${shortModel(R.model)}, synthetic data.`} />
      <div className="grid gap-4 sm:grid-cols-3">
        <Stat num={`${u.critical_effective.k}/${u.critical_effective.n}`} tone="bad" cap="serious mistakes without guardrails." />
        <Stat num={`${g.critical_effective.k}/${g.critical_effective.n}`} tone="good" cap="with guardrails." fine={`95% interval ${pct1(g.critical_effective.ci95[0])}–${pct1(g.critical_effective.ci95[1])}`} />
        <Stat num={pct(g.pass.rate)} cap="of tests passed with guardrails." fine={`${pct(u.pass.rate)} without`} />
      </div>
      <Card>
        <SectionTitle>What got through</SectionTitle>
        <Legend />
        <PairBars rows={kinds.map((k) => ({ name: kindName(k), g: g.by_kind[k].effective, u: u.by_kind[k].effective }))} max={max} />
      </Card>
      <Card>
        <Disclosure summary="Pass rate by situation" defaultOpen>
          <TableWrap>
            <thead><tr><th>Situation</th><th className="num">Guarded</th><th className="num">Without</th></tr></thead>
            <tbody>{Object.entries(g.per_category).map(([c, v]) => <tr key={c}><td>{catName(c)}</td><td className="num">{v.passed}/{v.n}</td><td className="num">{u.per_category[c].passed}/{u.per_category[c].n}</td></tr>)}</tbody>
          </TableWrap>
        </Disclosure>
        <Disclosure summary="Mistakes: tried / got through">
          <TableWrap>
            <thead><tr><th>Mistake</th><th className="num">Guarded</th><th className="num">Without</th></tr></thead>
            <tbody>{kinds.map((k) => <tr key={k}><td>{kindName(k)}</td><td className="num">{g.by_kind[k].attempted} / {g.by_kind[k].effective}</td><td className="num">{u.by_kind[k].attempted} / {u.by_kind[k].effective}</td></tr>)}</tbody>
          </TableWrap>
        </Disclosure>
        <Disclosure summary={`Guarded failures (${g.failures.length})`}>
          {g.failures.length ? <ul className="list-disc space-y-1 pl-5 text-sm">{g.failures.map((f, i) => <li key={f.id + i}><code className="font-mono text-xs">{f.id}</code>: {f.why.join("; ")}</li>)}</ul> : <p className="text-sm">None.</p>}
          {R.notes && <p className="mt-3 text-sm text-muted">{R.notes}</p>}
        </Disclosure>
        <Disclosure summary="Limits">
          <p className="text-sm leading-relaxed text-muted">Synthetic data, one model, repeats not independent. “Without” is an unprotected baseline.</p>
        </Disclosure>
      </Card>
    </div>
  );
}
