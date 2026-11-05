import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import clsx from "clsx";
import { Check, ShieldCheck, X } from "lucide-react";
import { Badge, Card, Empty, PageHeader, Segmented } from "@/components/ui";
import { ageText, rupees } from "@/lib/format";
import { useMe } from "@/lib/me";
import type { Approval, ApprovalsFeed } from "@/lib/types";

const POLL_MS = 5000;
const STATE_TONE = { pending: "warn", approved: "pass", rejected: "fail" } as const;

function useApprovals() {
  const navigate = useNavigate();
  const [feed, setFeed] = useState<ApprovalsFeed | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/data?file=approvals", { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) return navigate("/login", { replace: true });
      if (!r.ok) throw new Error("HTTP " + r.status);
      setFeed((await r.json()) as ApprovalsFeed);
      setError("");
    } catch { setError("Could not reach the approvals feed. Retrying."); }
  }, [navigate]);
  useEffect(() => {
    void load();
    const t = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);
  return { feed, error, reload: load };
}

function Item({ a, canDecide, onDone }: { a: Approval; canDecide: boolean; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"" | "approve" | "reject">("");
  const [msg, setMsg] = useState("");
  const senior = a.tier === "senior";

  async function decide(decision: "approve" | "reject") {
    setMsg(""); setBusy(decision);
    try {
      const r = await fetch("/api/approvals", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id, decision, note }) });
      const j = (await r.json().catch(() => ({}))) as { error?: string };
      if (!r.ok) setMsg(j.error ?? `Could not record the decision (HTTP ${r.status}).`);
      onDone();
    } catch { setMsg("Could not reach the server."); }
    setBusy("");
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-2xl font-semibold tabular-nums">{rupees(a.amount_paise)}</span>
            <Badge tone={STATE_TONE[a.state]} dot>{a.state}</Badge>
            {senior && <Badge tone="warn">Senior approval</Badge>}
            <Badge>{a.tenant}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted">Refund on <code className="font-mono text-xs">{a.payment_id}</code> · {a.source.kind === "sim" ? "Simulator" : "Payment event"}{a.source.title ? ` · ${a.source.title}` : ""}</p>
          {a.reason && <p className="mt-1 text-sm text-muted">“{a.reason}”</p>}
        </div>
        <span className="text-xs text-faint">{ageText(a.created_at)}</span>
      </div>

      {a.state === "pending" && canDecide && (
        <div className="mt-4 space-y-3">
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} aria-label="Reason"
            placeholder={senior ? "Reason (required to approve)" : "Reason (optional)"}
            className="h-10 w-full rounded-lg border border-line-strong bg-surface px-3 text-sm outline-none focus:border-accent" />
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" disabled={!!busy} onClick={() => decide("approve")}
              className="inline-flex h-10 items-center gap-2 rounded-full bg-fg px-5 text-sm font-medium text-bg disabled:opacity-50"><Check className="size-4" />{busy === "approve" ? "Saving…" : "Approve"}</button>
            <button type="button" disabled={!!busy} onClick={() => decide("reject")}
              className="inline-flex h-10 items-center gap-2 rounded-full border border-line-strong px-5 text-sm font-medium disabled:opacity-50"><X className="size-4" />{busy === "reject" ? "Saving…" : "Reject"}</button>
          </div>
          {msg && <p className="text-sm text-bad" role="alert">{msg}</p>}
        </div>
      )}

      {a.decision && (
        <div className="mt-4 rounded-xl bg-sunken p-3.5 text-sm">
          <p><b>{a.decision.by}</b> {a.state} this · {ageText(a.decision.at)}{a.decision.note ? ` · “${a.decision.note}”` : ""}</p>
          <p className={clsx("mt-1 flex items-center gap-1.5 text-xs", a.audit ? "text-good" : "text-faint")}>
            <ShieldCheck className="size-3.5" />{a.audit ? <>In the audit log · <code className="font-mono">{a.audit.hash.slice(0, 12)}…</code></> : "Waiting to be written to the audit log"}
          </p>
        </div>
      )}
    </Card>
  );
}

export default function Approvals() {
  const me = useMe();
  const { feed, error, reload } = useApprovals();
  const [view, setView] = useState<"pending" | "decided" | "all">("pending");
  const all = feed?.approvals ?? [];
  const pending = all.filter((a) => a.state === "pending").length;
  const rows = all.filter((a) => view === "all" || (view === "pending") === (a.state === "pending"));

  return (
    <div className="space-y-6">
      <PageHeader title="Approvals" lead="Refunds the agent drafted. Nothing happens until a person decides; the ledger is a sample, so no money moves."
        right={<Badge tone={pending ? "warn" : "neutral"} dot>{pending} pending</Badge>} />
      <Segmented label="Show" value={view} onChange={setView} items={[["pending", "Pending"], ["decided", "Decided"], ["all", "All"]]} />
      {me?.role !== "admin" && <p className="text-sm text-muted">Only admins can approve or reject. You can follow the queue here.</p>}
      {error && <p className="text-sm text-warn" role="status">{error}</p>}
      {feed && !feed.configured && <Empty>The approvals store is not configured.</Empty>}
      {feed?.configured && rows.length === 0 && <Empty>{view === "pending" ? "Nothing is waiting for approval." : "Nothing here yet."}</Empty>}
      <div className="space-y-4">{rows.map((a) => <Item key={a.id} a={a} canDecide={me?.role === "admin"} onDone={() => void reload()} />)}</div>
    </div>
  );
}
