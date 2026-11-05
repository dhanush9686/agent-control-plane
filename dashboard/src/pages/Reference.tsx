import { Link } from "react-router";
import { useReady } from "@/lib/data";
import { GUARDRAILS } from "@/lib/vocab";
import { Badge, Card, GraphKey, PageHeader, SectionTitle } from "@/components/ui";
import { ArchitectureGraph, LineageGraph } from "@/components/domain";

export function Architecture() {
  return (
    <div className="space-y-6">
      <PageHeader title="Architecture" lead="The model sits behind the checks, not in front of them." />
      <Card><ArchitectureGraph flow /><GraphKey /></Card>
      <div className="grid gap-4 sm:grid-cols-2">
        {[
          ["Why the guards sit outside the model", "A prompt can be talked around. Code between the model and the records cannot."],
          ["Why refunds are drafts", "Money moves only after a person approves. Large refunds need a senior approver."],
          ["Why a read-only adapter", "The live check uses GET requests and test keys only. It cannot change anything."],
          ["Why no judge model", "Scores come from the ledger, not another model's opinion."],
        ].map(([t, d]) => <Card key={t}><h3 className="text-[15px] font-semibold">{t}</h3><p className="mt-1.5 text-sm leading-relaxed text-muted">{d}</p></Card>)}
      </div>
    </div>
  );
}

export function Guardrails() {
  const { assure } = useReady();
  const g = assure.real_run.summary.guarded, u = assure.real_run.summary.unguarded;
  return (
    <div>
      <PageHeader title="Guardrails" lead="Five independent controls, each measured against the mistake it stops." />
      <div className="space-y-4">
        {GUARDRAILS.map((x, i) => {
          const gk = g.by_kind[x.kind], uk = u.by_kind[x.kind];
          return (
            <Card key={x.key}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex gap-4">
                  <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-sm font-semibold text-accent">{i + 1}</div>
                  <div>
                    <h3 className="text-base font-semibold">{x.title}</h3>
                    <p className="mt-1 max-w-xl text-sm leading-relaxed text-muted">{x.long}</p>
                  </div>
                </div>
                {gk && uk && <div className="flex gap-2"><Badge tone="pass">Guarded: {gk.effective} got through</Badge><Badge tone={uk.effective ? "fail" : "neutral"}>Without: {uk.effective}</Badge></div>}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

export function DataLineage() {
  const { live, assure } = useReady();
  const latest = live?.latest;
  return (
    <div className="space-y-6">
      <PageHeader title="Data and lineage" lead="Where every number on this site comes from." />
      <Card>
        <SectionTitle>Saved benchmark</SectionTitle>
        <p className="text-sm leading-relaxed text-muted">{assure.real_run.summary.guarded.n} runs per mode, synthetic ledger.</p>
      </Card>
      {latest && live ? (
        <>
          {Object.keys(latest.sources).map((src) => (
            <Card key={src}><SectionTitle>{src === "razorpay-test" ? "Razorpay test mode" : "Sample data"}</SectionTitle><LineageGraph live={live} latest={latest} src={src} /></Card>
          ))}
          <Card>
            <SectionTitle>Provenance</SectionTitle>
            <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted">{(latest.provenance ?? []).map((t) => t.replace(/\s*Not affiliated with Razorpay\.?/i, "").replace("a real model on Amazon Bedrock", "a live AI model")).filter(Boolean).map((t, i) => <li key={i}>{t}</li>)}</ul>
          </Card>
        </>
      ) : <Card><p className="text-sm text-muted">No live sync yet. <Link className="text-accent" to="/app/live">Live check</Link></p></Card>}
    </div>
  );
}

export function About() {
  const limits = [
    "Test-mode and synthetic data only. No production traffic, no real customers.",
    "One AI model and small samples, so confidence intervals are optimistic.",
    "Tenancy is emulated, and the test account has no refunds or settlements, so those paths use sample data.",
    "“Without guardrails” is a deliberately unprotected baseline, not a claim about any real system.",
    "The audit chain is tamper-evident, not tamper-proof.",
    "Live events come from test-mode payments and refresh every few seconds, not instantly.",
  ];
  return (
    <div className="space-y-6">
      <PageHeader title="About and limits" lead="An assurance layer for AI agents that touch money." />
      <Card>
        <SectionTitle>What this is</SectionTitle>
        <p className="text-sm leading-relaxed text-muted">Guardrails between an AI agent and a payments ledger, plus a benchmark that runs the same scenarios with and without them.</p>
      </Card>
      <Card>
        <SectionTitle>What it does not claim</SectionTitle>
        <ul className="list-disc space-y-2 pl-5 text-sm leading-relaxed text-muted">{limits.map((l) => <li key={l}>{l}</li>)}</ul>
      </Card>
    </div>
  );
}
