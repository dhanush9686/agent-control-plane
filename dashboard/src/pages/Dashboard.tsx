import { Link } from "react-router";
import { motion } from "motion/react";
import { ArrowRight, Activity, ScrollText, Swords } from "lucide-react";
import { useReady } from "@/lib/data";
import { ageText, freshness, pct, pct1, shortModel, usd } from "@/lib/format";
import { KIND_LABEL, SOURCE_LABEL } from "@/lib/vocab";
import { Badge, Card, Disclosure, PageHeader, PairBars, SectionTitle, Stat } from "@/components/ui";
import { ArchitectureGraph, RunItem, RunBadges } from "@/components/domain";
import { useActivity } from "@/lib/activity";

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

const WAIT_MS = 120_000;

/* What the live feeds have recorded so far: webhook events handled by the agent and simulator runs. */
function LiveActivity() {
  const { events, sim } = useActivity();
  const evs = events?.events ?? [], jobs = sim?.jobs ?? [];
  const evDone = evs.filter((e) => e.state === "done"), jobDone = jobs.filter((j) => j.verdict);
  const evLeaks = evDone.reduce((n, e) => n + (e.verdict?.mistakes_with_guardrails ?? 0), 0);
  const evWould = evDone.reduce((n, e) => n + (e.verdict?.mistakes_without ?? 0), 0);
  const simBreach = jobDone.filter((j) => j.verdict!.guarded === "breach").length;
  const simBase = jobDone.filter((j) => j.verdict!.unguarded === "breach").length;
  const stuck = [...evs.filter((e) => e.state === "queued").map((e) => e.received_at), ...jobs.filter((j) => j.state === "queued").map((j) => j.created_at)]
    .some((t) => Date.now() - Date.parse(t) > WAIT_MS);
  const lastEv = evs[0], lastJob = jobs[0];

  return (
    <div className="mt-6">
      <SectionTitle right={<Badge tone="accent" dot>Updates every 5s</Badge>}>Live activity</SectionTitle>
      {stuck && <p className="mb-3 rounded-xl bg-warn-soft px-4 py-3 text-sm text-warn">Some items have waited over two minutes: the worker is not running. Start it with <code className="font-mono text-xs">make live-worker</code>, or run the live-worker workflow on GitHub.</p>}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <div className="flex items-start justify-between"><div className="text-sm font-medium">Webhook events</div><Link to="/app/events" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">Open <ArrowRight className="size-3.5" /></Link></div>
          {events && events.configured ? (
            <>
              <div className="mt-2 flex items-baseline gap-2"><span className="text-3xl font-semibold tabular-nums">{evs.length}</span><span className="text-sm text-muted">received · {evDone.length} handled · {events.queue} waiting</span></div>
              <p className="mt-2 text-sm text-muted">Across handled events: <b className="text-good">{evLeaks}</b> mistakes with guardrails, <b className="text-bad">{evWould}</b> the baseline would have made.</p>
              {lastEv && <p className="mt-2 text-xs text-faint">Latest: <code className="font-mono">{lastEv.payment.id}</code> · {lastEv.state} · {ageText(lastEv.received_at)}</p>}
              {!lastEv && <p className="mt-2 text-sm text-muted">No events yet. Make a test payment on the Live events page.</p>}
            </>
          ) : <p className="mt-3 text-sm text-muted">{events ? "The event store is not configured." : "Loading…"}</p>}
        </Card>
        <Card>
          <div className="flex items-start justify-between"><div className="text-sm font-medium">Guardrail simulator</div><Link to="/app/simulator" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">Open <ArrowRight className="size-3.5" /></Link></div>
          {sim && sim.configured ? (
            <>
              <div className="mt-2 flex items-baseline gap-2"><span className="text-3xl font-semibold tabular-nums">{jobs.length}</span><span className="text-sm text-muted">runs · {jobDone.length} finished · {sim.queue} waiting</span></div>
              <p className="mt-2 text-sm text-muted">Of finished runs: <b className="text-good">{simBreach}</b> breaches with guardrails, <b className="text-bad">{simBase}</b> without.</p>
              {lastJob && <p className="mt-2 text-xs text-faint">Latest: {lastJob.title ?? "Custom payload"} · {lastJob.state}{lastJob.verdict ? ` · guarded ${lastJob.verdict.guarded}, baseline ${lastJob.verdict.unguarded}` : ""} · {ageText(lastJob.created_at)}</p>}
              {!lastJob && <p className="mt-2 text-sm text-muted">No simulations yet. Run an attack on the Guardrail simulator page.</p>}
            </>
          ) : <p className="mt-3 text-sm text-muted">{sim ? "The simulator store is not configured." : "Loading…"}</p>}
        </Card>
      </div>
      <p className="mt-2 text-xs text-faint">The headline numbers above come from the saved benchmark and the latest scheduled check. Events and simulations are counted here separately, over the most recent ones kept.</p>
    </div>
  );
}

export default function Dashboard() {
  const { assure, live } = useReady();
  const R = assure.real_run, g = R.summary.guarded, u = R.summary.unguarded;
  const latest = live?.latest;
  const fr = latest ? freshness(latest.finished_at, latest.expected_interval_hours, live?.via) : null;
  const kinds = Object.keys(g.by_kind);
  const max = Math.max(...kinds.map((k) => Math.max(u.by_kind[k].effective, g.by_kind[k].effective)), 1);
  const notable = (live?.runs ?? []).filter((r) => r.attempted.length || !r.passed || r.withheld).slice(0, 4);
  const liveG = latest ? Object.values(latest.sources).reduce((a, s) => ({ k: a.k + s.summary.guarded.pass.k, n: a.n + s.summary.guarded.pass.n }), { k: 0, n: 0 }) : null;

  return (
    <div>
      <PageHeader title={`${greeting()}.`} lead={<>Here is how the guardrails are holding up. Results come from {g.n} test runs on {shortModel(R.model)}, synthetic data.</>}
        right={fr && <Badge tone={fr.tone} dot>{fr.text} · {ageText(latest!.finished_at)}</Badge>} />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat num={pct(u.critical_effective.rate)} tone="bad" cap="of runs end in a serious mistake without guardrails." fine={`${u.critical_effective.k} of ${u.critical_effective.n}`} />
        <Stat num={pct(g.critical_effective.rate)} tone="good" cap="with guardrails on." fine={`${g.critical_effective.k} of ${g.critical_effective.n} · 95% interval ${pct1(g.critical_effective.ci95[0])}–${pct1(g.critical_effective.ci95[1])}`} />
        <Stat num={pct(g.pass.rate)} cap="of benchmark tests passed." fine={`${pct(u.pass.rate)} without guardrails`} />
        {liveG && latest
          ? <Stat num={`${liveG.k}/${liveG.n}`} cap="passed in the latest live check." fine={`${usd(latest.cost_usd)} · ${shortModel(latest.model)}`} />
          : <Stat num="n/a" cap="No live check yet." />}
      </div>

      <LiveActivity />

      <div className="mt-6 grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <SectionTitle right={<Link to="/app/benchmark" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">Details <ArrowRight className="size-3.5" /></Link>}>What got through</SectionTitle>
          <p className="-mt-2 mb-5 text-sm text-muted">Runs where the mistake reached the user or the records.</p>
          <PairBars rows={kinds.map((k) => ({ name: KIND_LABEL[k] ?? k, g: g.by_kind[k].effective, u: u.by_kind[k].effective }))} max={max} />
        </Card>
        <Card className="lg:col-span-2">
          <SectionTitle right={<Link to="/app/live" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">Open <ArrowRight className="size-3.5" /></Link>}>Latest live check</SectionTitle>
          {latest ? (
            <div className="space-y-4">
              {Object.entries(latest.sources).map(([k, v]) => (
                <Link key={k} to={`/app/live/${k}`} className="block rounded-xl bg-sunken p-4 transition-colors hover:bg-accent-soft">
                  <div className="text-sm font-medium">{SOURCE_LABEL[k] ?? k}</div>
                  <div className="mt-1 flex items-baseline gap-2">
                    <span className="text-2xl font-semibold tabular-nums">{v.summary.guarded.pass.k}/{v.summary.guarded.pass.n}</span>
                    <span className="text-xs text-muted">passed with guardrails · {v.summary.unguarded.pass.k}/{v.summary.unguarded.pass.n} without</span>
                  </div>
                </Link>
              ))}
              <p className="text-xs text-faint">One repeat per scenario, so treat single numbers as a spot check, not a benchmark.</p>
            </div>
          ) : <p className="text-sm text-muted">{live?.error ?? "No live check yet. Run `make live-sync`."}</p>}
        </Card>
      </div>

      <Card className="mt-6">
        <SectionTitle right={<Link to="/app/architecture" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">Explained <ArrowRight className="size-3.5" /></Link>}>Every request passes through checks</SectionTitle>
        <p className="-mt-2 mb-5 text-sm text-muted">The AI never touches the records directly.</p>
        <ArchitectureGraph flow />
      </Card>

      {notable.length > 0 && (
        <Card className="mt-6">
          <SectionTitle right={<Link to="/app/runs" className="inline-flex items-center gap-1 text-sm text-accent hover:underline">All runs <ArrowRight className="size-3.5" /></Link>}>Notable runs</SectionTitle>
          <div>{notable.map((r, i) => <Disclosure key={i} summary={<RunBadges r={r} />}><RunItem r={r} /></Disclosure>)}</div>
        </Card>
      )}

      <div className="mt-6 grid gap-4 sm:grid-cols-3">
        {([["/app/attack-lab", Swords, "Try an attack", "Replay a scripted attacker with and without guardrails."], ["/app/audit", ScrollText, "Audit trail", "See what happens when a log entry is edited."], ["/app/live", Activity, "Live lineage", "Trace every number back to its source."]] as const).map(([to, Icon, t, d], i) => (
          <motion.div key={to} initial={{ opacity: 0, y: 12 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true }} transition={{ delay: i * 0.07 }}>
            <Link to={to} className="group block h-full rounded-2xl border border-line bg-surface p-5 shadow-card transition-transform hover:-translate-y-0.5">
              <Icon className="mb-3 size-5 text-accent" strokeWidth={1.9} />
              <div className="text-[15px] font-semibold">{t}</div>
              <p className="mt-1 text-sm text-muted">{d}</p>
            </Link>
          </motion.div>
        ))}
      </div>
    </div>
  );
}
