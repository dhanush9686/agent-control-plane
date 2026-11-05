import { Link } from "react-router";
import { useReady } from "@/lib/data";
import { shortModel } from "@/lib/format";
import { GUARDRAILS } from "@/lib/vocab";
import { Badge, Card, GraphKey, PageHeader, SectionTitle } from "@/components/ui";
import { ArchitectureGraph, LineageGraph } from "@/components/domain";

export function Architecture() {
  return (
    <div className="space-y-6">
      <PageHeader title="Architecture" lead="The AI model sits behind the checks, not in front of them. Nothing it says is trusted until it passes." />
      <Card><ArchitectureGraph flow /><GraphKey /></Card>
      <div className="grid gap-4 sm:grid-cols-2">
        {[
          ["Why the guards sit outside the model", "A prompt can be talked around. Code that sits between the model and the records cannot, because the model never gets a choice."],
          ["Why refunds are drafts", "Moving money is the one action that cannot be undone, so the agent proposes and a person disposes. Large refunds need a senior approver."],
          ["Why a read-only adapter", "The live check reads a Razorpay test account with GET requests only and test keys only. It cannot create or change anything there."],
          ["Why no judge model", "Scores come from the ledger's ground truth, not another model's opinion, so results are reproducible and cheap to re-run."],
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
      <PageHeader title="Guardrails" lead="Five independent controls. Each one is measured against the mistake it exists to stop." />
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
      <p className="mt-4 text-xs text-faint">Counts are runs where the mistake reached the user or the records, out of {g.n} per mode. The audit log is checked separately on the Audit page.</p>
    </div>
  );
}

export function DataLineage() {
  const { live, assure } = useReady();
  const latest = live?.latest;
  return (
    <div className="space-y-6">
      <PageHeader title="Data and lineage" lead="Where every number on this site comes from, and what it is not." />
      <Card>
        <SectionTitle>Saved benchmark</SectionTitle>
        <p className="text-sm leading-relaxed text-muted">{assure.real_run.summary.guarded.n} runs per mode on {shortModel(assure.real_run.model)} against a synthetic ledger of fake merchants. It is bundled with the site and changes only when the project is rebuilt.</p>
      </Card>
      {latest && live ? (
        <>
          {Object.keys(latest.sources).map((src) => (
            <Card key={src}><SectionTitle>{src === "razorpay-test" ? "Razorpay test mode" : "Sample data"}</SectionTitle><LineageGraph live={live} latest={latest} src={src} /></Card>
          ))}
          <Card>
            <SectionTitle>Provenance</SectionTitle>
            <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted">{(latest.provenance ?? []).map((t, i) => <li key={i}>{t}</li>)}</ul>
          </Card>
        </>
      ) : <Card><p className="text-sm text-muted">No live sync yet. See <Link className="text-accent" to="/app/live">Live check</Link>.</p></Card>}
    </div>
  );
}

export function About() {
  const limits = [
    "Test-mode and synthetic data only. No production traffic, no real customers.",
    "One AI model so far, and small samples. Repeats of a scenario are not independent, so confidence intervals are optimistic.",
    "Cross-tenant coverage on real payments is thin: the Razorpay test account has very few payments for the second merchant, and tenancy is emulated.",
    "The test account returns no refunds or settlements, so those paths are exercised on sample data.",
    "“Without guardrails” is a deliberately unprotected baseline, not a claim about any real system.",
    "The audit chain is tamper-evident, not tamper-proof; the latest fingerprint is not yet anchored outside the database.",
    "The dashboard uses one shared password. A real deployment would use single sign-on and per-user roles.",
    "Live checks are periodic captures. Live events are real Razorpay test-mode webhooks, but they come from test payments made through Checkout, the demo sets their note and merchant, and they are handled by a worker that has to be running; the page polls rather than streams.",
  ];
  return (
    <div className="space-y-6">
      <PageHeader title="About and limits" lead="An assurance layer for AI agents that touch money, built as a portfolio project. Not affiliated with Razorpay or any payment company." />
      <Card>
        <SectionTitle>What this is</SectionTitle>
        <p className="text-sm leading-relaxed text-muted">A policy engine, masking, a numeric-fidelity guard and a hash-chained audit log that sit between an AI agent and a payments ledger, plus a way to measure whether they work: the same scenarios run with and without the guards, scored from ledger ground truth.</p>
      </Card>
      <Card>
        <SectionTitle>What it does not claim</SectionTitle>
        <ul className="list-disc space-y-2 pl-5 text-sm leading-relaxed text-muted">{limits.map((l) => <li key={l}>{l}</li>)}</ul>
      </Card>
    </div>
  );
}
