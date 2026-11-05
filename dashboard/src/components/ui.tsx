import { useEffect, useRef, useState, type ReactNode } from "react";
import { animate, motion, useInView, useReducedMotion } from "motion/react";
import { ChevronDown } from "lucide-react";
import clsx from "clsx";
import type { Tone } from "@/lib/format";

export function Card({ children, className, hover }: { children: ReactNode; className?: string; hover?: boolean }) {
  return (
    <motion.div whileHover={hover ? { y: -3 } : undefined} transition={{ type: "spring", stiffness: 300, damping: 24 }}
      className={clsx("rounded-2xl border border-line bg-surface p-5 shadow-card sm:p-6", className)}>
      {children}
    </motion.div>
  );
}

export function PageHeader({ title, lead, right }: { title: string; lead?: ReactNode; right?: ReactNode }) {
  return (
    <motion.header initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45 }}
      className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div className="max-w-2xl">
        <h1 className="text-[28px] font-semibold leading-tight tracking-tight sm:text-[32px]">{title}</h1>
        {lead && <p className="mt-2 text-[15px] leading-relaxed text-muted">{lead}</p>}
      </div>
      {right}
    </motion.header>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="text-lg font-semibold tracking-tight">{children}</h2>
      {right}
    </div>
  );
}

const TONE = { pass: "bg-good-soft text-good", warn: "bg-warn-soft text-warn", fail: "bg-bad-soft text-bad", neutral: "bg-sunken text-muted", accent: "bg-accent-soft text-accent" };
export function Badge({ tone = "neutral", children, dot }: { tone?: Tone | "neutral" | "accent"; children: ReactNode; dot?: boolean }) {
  return (
    <span className={clsx("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium", TONE[tone])}>
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/* Number that counts up once when it scrolls into view. Accepts "12%", "0/93", "₹1,200" shapes. */
export function CountUp({ value, className }: { value: string; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-20px" });
  const reduce = useReducedMotion();
  const m = value.match(/^([^\d]*)(\d+(?:\.\d+)?)(.*)$/);
  const [text, setText] = useState(reduce || !m ? value : (m[1] ?? "") + "0" + (m[3] ?? ""));
  useEffect(() => {
    if (!inView || reduce || !m) { setText(value); return; }
    const target = parseFloat(m[2]), decimals = (m[2].split(".")[1] ?? "").length;
    const c = animate(0, target, { duration: 1.1, ease: [0.16, 1, 0.3, 1], onUpdate: (v) => setText(m[1] + v.toFixed(decimals) + m[3]) });
    return () => c.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inView, value, reduce]);
  return <span ref={ref} className={className}>{text}</span>;
}

export function Stat({ num, cap, tone, fine }: { num: string; cap: string; tone?: "good" | "bad" | ""; fine?: string }) {
  return (
    <Card hover>
      <CountUp value={num} className={clsx("block text-[44px] font-semibold leading-none tracking-tight tabular-nums", tone === "bad" && "text-bad", tone === "good" && "text-good")} />
      <p className="mt-3 text-sm leading-snug text-fg">{cap}</p>
      {fine && <p className="mt-1.5 text-xs leading-snug text-faint">{fine}</p>}
    </Card>
  );
}

export function Segmented<T extends string>({ items, value, onChange, label }: { items: [T, string][]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex flex-wrap gap-1 rounded-[14px] bg-sunken p-1">
      {items.map(([k, text]) => (
        <button key={k} type="button" onClick={() => onChange(k)} aria-pressed={k === value}
          className={clsx("relative rounded-[10px] px-3.5 py-1.5 text-[13px] font-medium transition-colors", k === value ? "text-fg" : "text-muted hover:text-fg")}>
          {k === value && <motion.span layoutId={"seg-" + label} className="absolute inset-0 rounded-[10px] bg-surface shadow-sm ring-1 ring-line" transition={{ type: "spring", stiffness: 420, damping: 34 }} />}
          <span className="relative">{text}</span>
        </button>
      ))}
    </div>
  );
}

export function Disclosure({ summary, children, defaultOpen }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <div className="border-t border-line first:border-t-0">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 py-3.5 text-left text-sm font-medium">
        <span className="min-w-0 flex-1">{summary}</span>
        <ChevronDown className={clsx("size-4 shrink-0 text-faint transition-transform", open && "rotate-180")} />
      </button>
      <motion.div initial={false} animate={{ height: open ? "auto" : 0, opacity: open ? 1 : 0 }} transition={{ duration: 0.25 }} className="overflow-hidden" aria-hidden={!open}>
        <div className="pb-4">{children}</div>
      </motion.div>
    </div>
  );
}

export function PairBars({ rows, max }: { rows: { name: string; g: number; u: number }[]; max: number }) {
  return (
    <div className="space-y-5">
      {rows.map((r) => (
        <div key={r.name}>
          <div className="mb-1.5 text-sm font-medium">{r.name}</div>
          {([["With guardrails", r.g, "bg-good"], ["Without", r.u, "bg-bad"]] as const).map(([lab, v, color]) => (
            <div key={lab} className="mb-1 grid grid-cols-[96px_1fr_32px] items-center gap-3 text-xs text-muted">
              <span>{lab}</span>
              <span className="h-2 overflow-hidden rounded-full bg-sunken">
                <motion.span className={clsx("block h-full rounded-full", color)} initial={{ width: 0 }}
                  whileInView={{ width: max ? `${Math.max(v ? 2 : 0, (v / max) * 100)}%` : 0 }} viewport={{ once: true }} transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }} />
              </span>
              <span className="text-right tabular-nums text-fg">{v}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function Legend() {
  return (
    <div className="mb-4 flex gap-4 text-xs text-muted">
      <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-full bg-good" />With guardrails</span>
      <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-full bg-bad" />Without</span>
    </div>
  );
}

export function GraphKey() {
  return (
    <div className="mt-4 flex flex-wrap justify-center gap-4 text-xs text-muted">
      {([["border-accent bg-accent-soft", "Guardrail"], ["border-good bg-good-soft", "Blocked or safe"], ["border-bad bg-bad-soft", "Bad outcome"]] as const).map(([c, t]) => (
        <span key={t} className="inline-flex items-center gap-1.5"><i className={clsx("size-3 rounded border", c)} />{t}</span>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-2xl border border-dashed border-line-strong p-10 text-center text-sm text-muted">{children}</div>;
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="overflow-x-auto rounded-xl border border-line"><table className="w-full border-collapse text-sm [&_td]:border-t [&_td]:border-line [&_td]:px-3.5 [&_td]:py-2.5 [&_th]:px-3.5 [&_th]:py-2.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_th]:text-muted [&_.num]:text-right [&_.num]:tabular-nums">{children}</table></div>;
}
