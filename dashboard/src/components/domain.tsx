import { Fragment } from "react";
import { motion } from "motion/react";
import clsx from "clsx";
import { Graph, type GNode, type GEdge } from "./Graph";
import { Badge, GraphKey } from "./ui";
import { useNarrow } from "./hooks";
import { ageText, clip, ms, usd } from "@/lib/format";
import { catName, kindName } from "@/lib/vocab";
import type { ChainRow, Live, LiveLatest, ReplaySide, RunRecord, Step } from "@/lib/types";

/* How a request is protected: the one diagram that explains the product. */
export function ArchitectureGraph({ flow }: { flow?: boolean }) {
  const vertical = useNarrow();
  return (
    <Graph vertical={vertical} flow={flow}
      label="How a request is protected: user, AI agent, policy checks, payments records or human approval, safety filters, then the answer and the audit log."
      layers={[["user"], ["agent"], ["policy"], ["ledger", "approval"], ["filters"], ["answer", "audit"]]}
      nodes={{
        user: { title: "Request", sub: "“Refund ₹500 on payment X”", cls: "info" },
        agent: { title: "AI agent", sub: "Chooses which tools to call" },
        policy: { title: "Policy checks", sub: "Whose data? What amount?", cls: "accent" },
        ledger: { title: "Payments records", sub: "Read-only", cls: "info" },
        approval: { title: "Human approval", sub: "Refunds are drafts", cls: "info" },
        filters: { title: "Safety filters", sub: "Mask cards. Verify figures.", cls: "accent" },
        answer: { title: "Answer", sub: "Only what passed" },
        audit: { title: "Audit log", sub: "Tamper-evident", cls: "info" },
      }}
      edges={[["user", "agent"], ["agent", "policy"], ["policy", "ledger"], ["policy", "approval"], ["ledger", "filters"], ["approval", "filters"], ["filters", "answer"], ["filters", "audit", { dashed: true }]]} />
  );
}

function eventNode(e: Step): Pick<GNode, "cls" | "sub"> {
  const arg = e.args ? Object.values(e.args)[0] : "";
  if (e.allowed === false) return { cls: "ok", sub: ["Blocked by policy", e.code ?? ""] };
  if (e.effect === "executed") return { cls: "bad", sub: "Money moved, no approval" };
  if (e.effect === "draft") return { cls: "warn", sub: "Draft, awaits a human" };
  if (e.foreign) return { cls: "bad", sub: "Other merchant's data" };
  return { sub: arg ? String(arg) : "Allowed" };
}

export function RunGraph({ prompt, events, guarded, filterNote, filterCls, outcomeTitle, outcomeSub, outcomeCls, vertical, label }: {
  prompt: string; events: Step[]; guarded: boolean; filterNote: string; filterCls: GNode["cls"]; outcomeTitle: string; outcomeSub: string; outcomeCls: GNode["cls"]; vertical: boolean; label: string;
}) {
  const nodes: Record<string, GNode> = { u: { title: "Request", sub: prompt, cls: "info" } };
  const ids = ["u"];
  const shown = events.slice(0, 5);
  shown.forEach((e, i) => { nodes["e" + i] = { title: clip(e.tool, 26), ...eventNode(e) }; ids.push("e" + i); });
  if (events.length > shown.length) { nodes.more = { title: `+${events.length - shown.length} more calls` }; ids.push("more"); }
  nodes.f = { title: "Safety filters", sub: guarded ? filterNote : "Off in this mode", cls: guarded ? filterCls : "warn" };
  nodes.o = { title: outcomeTitle, sub: outcomeSub, cls: outcomeCls };
  ids.push("f", "o");
  const edges: GEdge[] = ids.slice(1).map((id, i) => [ids[i], id]);
  return <Graph layers={ids.map((i) => [i])} nodes={nodes} edges={edges} vertical={vertical} label={label} />;
}

export function ReplayGraph({ r, guarded, prompt }: { r: ReplaySide; guarded: boolean; prompt: string }) {
  const sees = r.user_sees !== r.model_said;
  return (
    <RunGraph prompt={prompt} events={r.steps} guarded={guarded} vertical
      label={(guarded ? "With guardrails: " : "Without guardrails: ") + (r.ok ? "no serious mistake." : "a serious mistake occurred.")}
      filterNote={r.withheld ? "Answer withheld" : sees ? "Card data masked" : "Passed"} filterCls={r.withheld || sees ? "ok" : ""}
      outcomeTitle={r.ok ? "Safe" : "Serious mistake"} outcomeSub={r.ok ? "Nothing bad got through" : clip(r.bad_outcomes.join(" "), 60)} outcomeCls={r.ok ? "ok" : "bad"} />
  );
}

export function StepList({ steps }: { steps: Step[] }) {
  if (!steps.length) return <p className="text-sm text-muted">No tool calls.</p>;
  return (
    <ol className="space-y-1.5">
      {steps.map((s, i) => {
        const args = Object.entries(s.args ?? {}).map(([k, v]) => k + "=" + v).join(", ");
        return (
          <li key={i} className={clsx("flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-3 py-2 text-[13px]", s.allowed ? "bg-sunken" : "bg-good-soft")}>
            <code className="font-mono text-xs">{s.tool}({args})</code>
            <span className={s.allowed ? "text-muted" : "font-medium text-good"}>{s.allowed ? "ran" : "blocked: " + s.code}</span>
            {typeof s.ms === "number" && <span className="text-xs tabular-nums text-faint">{s.ms.toFixed(2)} ms</span>}
            {s.effect === "executed" && <span className="font-medium text-bad">money moved</span>}
            {s.effect === "draft" && <span className="text-muted">draft for approval</span>}
            {!s.allowed && s.reason && <span className="basis-full text-xs text-muted">{s.reason}</span>}
          </li>
        );
      })}
    </ol>
  );
}

export function Answer({ label, text }: { label: string; text: string }) {
  return (
    <div className="mt-4">
      <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-faint">{label}</div>
      <div className="whitespace-pre-wrap break-words rounded-xl bg-sunken p-3.5 text-[13px] leading-relaxed">{text || "(none)"}</div>
    </div>
  );
}

export function ChainView({ rows, editedSeq, firstBad }: { rows: ChainRow[]; editedSeq?: number; firstBad?: number }) {
  return (
    <div role="img" aria-label="A chain of audit entries; each holds the fingerprint of the one before" className="noscrollbar flex items-stretch overflow-x-auto py-2">
      {rows.map((r, i) => {
        const edited = r.seq === editedSeq, after = firstBad != null && r.seq > firstBad;
        return (
          <Fragment key={r.seq}>
            {i > 0 && <div className={clsx("my-auto h-0.5 w-6 shrink-0", after ? "bg-bad/40" : "bg-line-strong")} />}
            <motion.div initial={{ opacity: 0, scale: 0.92 }} whileInView={{ opacity: after ? 0.55 : 1, scale: 1 }} viewport={{ once: true }} transition={{ delay: i * 0.06 }}
              title={r.action}
              className={clsx("w-[148px] shrink-0 rounded-xl border p-3 font-mono text-[11px]", edited ? "border-bad bg-bad-soft" : after ? "border-dashed border-bad/50 bg-surface" : "border-line-strong bg-surface")}>
              <div className="text-xs font-semibold text-fg">#{r.seq}</div>
              <div className="mt-0.5 truncate font-sans text-xs text-fg">{clip(r.action, 34)}</div>
              <div className="mt-2 text-faint">hash <span className="text-muted">{r.hash.slice(0, 8)}…</span></div>
              <div className="text-faint">prev <span className="text-muted">{(r.prev || "").slice(0, 8)}…</span></div>
            </motion.div>
          </Fragment>
        );
      })}
    </div>
  );
}

export function TrendChart({ history, src }: { history: NonNullable<Live["history"]>; src: string }) {
  const pts = history.slice().reverse().map((h) => ({ t: h.finished_at, s: h.sources?.[src] })).filter((p) => p.s);
  if (pts.length < 2) return null; // a trend needs two checks
  const W = 640, H = 190, L = 40, T = 12, B = 28, iw = W - L - 12, ih = H - T - B;
  const x = (i: number) => L + (i * iw) / (pts.length - 1), y = (r: number) => T + ih - r * ih;
  const line = (key: "guarded_pass" | "unguarded_pass", color: string) => {
    const ok = pts.map((p, i) => [i, p.s?.[key]] as const).filter((e): e is readonly [number, number] => e[1] != null);
    return (
      <g key={key}>
        <polyline fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" points={ok.map(([i, v]) => x(i) + "," + y(v)).join(" ")} />
        {ok.map(([i, v]) => <circle key={i} cx={x(i)} cy={y(v)} r={4} fill={color}><title>{`${pts[i].t.slice(0, 16).replace("T", " ")}: ${Math.round(v * 100)}%`}</title></circle>)}
      </g>
    );
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Share of tests passed at each check, with and without guardrails">
      {[0, 0.5, 1].map((r) => (
        <g key={r}>
          <line x1={L} x2={W - 12} y1={y(r)} y2={y(r)} stroke="var(--line)" />
          <text x={L - 6} y={y(r) + 4} textAnchor="end" fontSize="11" fill="var(--faint)">{Math.round(r * 100)}%</text>
        </g>
      ))}
      {line("unguarded_pass", "var(--bad)")}{line("guarded_pass", "var(--good)")}
      <text x={L} y={H - 8} fontSize="11" fill="var(--faint)">{pts[0].t.slice(0, 10)}</text>
      <text x={W - 12} y={H - 8} textAnchor="end" fontSize="11" fill="var(--faint)">{pts[pts.length - 1].t.slice(0, 10)}</text>
    </svg>
  );
}

export function LineageGraph({ live, latest, src }: { live: Live; latest: LiveLatest; src: string }) {
  const vertical = useNarrow();
  const g = latest.sources[src].summary.guarded, a = live.audit, razor = src === "razorpay-test";
  const snap = live.snapshot ?? latest.snapshot;
  const nodes: Record<string, GNode> = {
    src: { title: razor ? "Razorpay test account" : "Sample records", sub: razor ? "Read-only" : "Fake merchants", cls: "info" },
    snap: razor && snap ? { title: `${snap.payments} payments`, sub: "No personal data" } : { title: "Fixed sample set", sub: "Same every time" },
    scn: { title: `${g.n} scenarios`, sub: razor ? "Built from those payments" : "Hand-written" },
    run: { title: "AI runs each twice", sub: "With and without guardrails", cls: "accent" },
    aud: { title: "Audit chain", sub: a ? `${a.total_rows} entries, ${a.chain_ok ? "intact" : "broken"}` : "Unavailable", cls: a ? (a.chain_ok ? "ok" : "bad") : "warn" },
    store: { title: "Published results", sub: live.via === "redis" ? "Live" : "Saved copy", cls: "info" },
    page: { title: "This page", sub: "Behind the login" },
  };
  return (
    <Graph vertical={vertical} nodes={nodes} flow
      label="Data lineage: source records, snapshot, scenarios, AI runs, then the audit chain and published results, then this page."
      layers={[["src"], ["snap"], ["scn"], ["run"], ["aud", "store"], ["page"]]}
      edges={[["src", "snap"], ["snap", "scn"], ["scn", "run"], ["run", "aud"], ["run", "store"], ["store", "page"]]} />
  );
}

export function RunItem({ r }: { r: RunRecord }) {
  const vertical = useNarrow();
  const blocked = r.attempted.filter((k) => !r.effective.includes(k));
  const happened = r.effective, guarded = r.mode === "guarded";
  const sees = r.withheld ? "Answer withheld" : happened.length ? "Mistake got through" : r.passed ? "Correct" : "Check failed";
  return (
    <div className="space-y-4">
      <RunGraph prompt={catName(r.category)} events={r.events} guarded={guarded} vertical={vertical}
        label={"Execution of one test run: " + sees}
        filterNote={r.withheld ? "Answer withheld" : r.pii_found.length ? "Card data masked" : "Passed"} filterCls={r.withheld || r.pii_found.length ? "ok" : ""}
        outcomeTitle="User sees" outcomeSub={sees} outcomeCls={happened.length || !r.passed ? "bad" : "ok"} />
      <Answer label="Answer shown (masked)" text={r.answer} />
      {r.failed_checks.length > 0 && <p className="text-sm"><b>Why it failed: </b>{r.failed_checks.join("; ")}</p>}
      <div><div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-faint">Tool calls</div><StepList steps={r.events} /></div>
      <p className="text-xs text-faint">{r.tool_calls} calls, {r.denied_calls} denied · {ms(r.latency_ms)} · {usd(r.cost_usd)}</p>
      <GraphKey />
      {blocked.length > 0 && <div className="flex flex-wrap gap-1.5">{blocked.map((k) => <Badge key={k} tone="pass">Stopped: {kindName(k)}</Badge>)}</div>}
    </div>
  );
}

export function RunBadges({ r }: { r: RunRecord }) {
  const blocked = r.attempted.filter((k) => !r.effective.includes(k));
  return (
    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
      <Badge tone={r.mode === "guarded" ? "accent" : "neutral"}>{r.mode === "guarded" ? "Guarded" : "Without"}</Badge>
      <span className="mr-1 text-sm font-medium">{catName(r.category)}</span>
      <Badge tone={r.passed ? "pass" : "fail"}>{r.passed ? "Passed" : "Failed"}</Badge>
      {blocked.map((k) => <Badge key={k} tone="pass">Stopped: {kindName(k)}</Badge>)}
      {r.effective.map((k) => <Badge key={k} tone="fail">Got through: {kindName(k)}</Badge>)}
      {r.withheld && <Badge>Answer withheld</Badge>}
      <span className="text-xs text-faint">{ageText(r.started_at)}</span>
    </span>
  );
}
