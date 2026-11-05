import { useNavigate, useParams } from "react-router";
import { useReady } from "@/lib/data";
import { rupees } from "@/lib/format";
import { Card, Disclosure, GraphKey, PageHeader, Segmented } from "@/components/ui";
import { Answer, ReplayGraph, StepList } from "@/components/domain";
import type { ReplaySide } from "@/lib/types";
import clsx from "clsx";

function Column({ title, r, guarded, prompt }: { title: string; r: ReplaySide; guarded: boolean; prompt: string }) {
  const changed = r.user_sees !== r.model_said;
  return (
    <Card>
      <h3 className="mb-3 text-[15px] font-semibold">{title}</h3>
      <ReplayGraph r={r} guarded={guarded} prompt={prompt} />
      <div className={clsx("mt-4 rounded-xl px-4 py-3 text-sm font-medium", r.ok ? "bg-good-soft text-good" : "bg-bad-soft text-bad")}>{r.ok ? "No serious mistake" : r.bad_outcomes.join(" ")}</div>
      <Disclosure summary="Details">
        <StepList steps={r.steps} />
        <Answer label="The AI said" text={r.model_said} />
        {changed && <Answer label={r.withheld ? "The user sees (withheld)" : "The user sees (masked)"} text={r.user_sees} />}
        {r.refunds_created.map((x) => <p key={x.id} className="mt-3 text-xs text-muted">{x.id}: {rupees(x.amount_paise)} on {x.payment_id}, {x.status}</p>)}
      </Disclosure>
    </Card>
  );
}

export default function AttackLab() {
  const { assure } = useReady();
  const { id } = useParams();
  const navigate = useNavigate();
  const cases = assure.replays, c = cases.find((x) => x.id === id) ?? cases[0];
  return (
    <div>
      <PageHeader title="Attack lab" lead="A scripted attacker, run through the real checks. Not a live AI." />
      <Segmented label="Choose an attack" value={c.id} onChange={(v) => navigate(`/app/attack-lab/${v}`)} items={cases.map((x) => [x.id, x.title] as [string, string])} />
      <p className="mt-6 max-w-2xl text-[15px] leading-relaxed">{c.story}</p>
      <p className="mt-1.5 text-sm text-faint">Asked: “{c.prompt}”</p>
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Column title="Without guardrails" r={c.unguarded} guarded={false} prompt={c.prompt} />
        <Column title="With guardrails" r={c.guarded} guarded prompt={c.prompt} />
      </div>
      <GraphKey />
    </div>
  );
}
