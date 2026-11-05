import { Suspense, useEffect, useState } from "react";
import { NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router";
import { AnimatePresence, motion } from "motion/react";
import clsx from "clsx";
import { Activity, BookOpenCheck, FlaskRound, Radio, UserCog, Boxes, FlaskConical, Gauge, LayoutDashboard, ListChecks, LogOut, Menu, Moon, ScrollText, ShieldCheck, Sun, Swords, Workflow, X } from "lucide-react";
import { Logo } from "@/components/Logo";
import { useData } from "@/lib/data";
import { MeProvider, useMe } from "@/lib/me";
import { useTheme } from "@/lib/theme";
import { freshness } from "@/lib/format";

const NAV = [
  { group: "Workspace", items: [
    { to: "/app", label: "Dashboard", icon: LayoutDashboard, end: true },
    { to: "/app/live", label: "Live check", icon: Activity, live: true },
    { to: "/app/events", label: "Live events", icon: Radio },
    { to: "/app/runs", label: "Runs", icon: ListChecks },
  ] },
  { group: "Evidence", items: [
    { to: "/app/benchmark", label: "Benchmark", icon: Gauge },
    { to: "/app/attack-lab", label: "Attack lab", icon: Swords },
    { to: "/app/simulator", label: "Guardrail simulator", icon: FlaskRound },
    { to: "/app/audit", label: "Audit trail", icon: ScrollText },
  ] },
  { group: "Reference", items: [
    { to: "/app/architecture", label: "Architecture", icon: Workflow },
    { to: "/app/guardrails", label: "Guardrails", icon: ShieldCheck },
    { to: "/app/data", label: "Data and lineage", icon: Boxes },
    { to: "/app/about", label: "About and limits", icon: BookOpenCheck },
  ] },
] as const;

const ADMIN = { group: "Admin", items: [{ to: "/app/users", label: "Users and access", icon: UserCog }] } as const;

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { state } = useData();
  const me = useMe();
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();
  const dot = state.status === "ready" && state.live?.latest ? freshness(state.live.latest.finished_at, state.live.latest.expected_interval_hours, state.live.via) : null;

  async function signOut() {
    try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); } catch { /* leaving anyway */ }
    navigate("/", { replace: true });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 shrink-0 items-center gap-2.5 px-5">
        <Logo size={22} />
        <span className="text-[15px] font-semibold tracking-tight">Control Plane</span>
      </div>
      <nav className="flex-1 overflow-y-auto px-3 pb-4" aria-label="Sections">
        {[...NAV, ...(me?.role === "admin" && me.mode === "user" ? [ADMIN] : [])].map((g) => (
          <div key={g.group} className="mt-5 first:mt-2">
            <div className="px-3 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">{g.group}</div>
            {g.items.map((it) => (
              <NavLink key={it.to} to={it.to} end={"end" in it ? it.end : false} onClick={onNavigate}
                className={({ isActive }) => clsx("group relative mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-[14px] font-medium transition-colors", isActive ? "text-fg" : "text-muted hover:bg-sunken hover:text-fg")}>
                {({ isActive }) => (
                  <>
                    {isActive && <motion.span layoutId="nav-pill" className="absolute inset-0 rounded-lg bg-sunken ring-1 ring-line" transition={{ type: "spring", stiffness: 420, damping: 36 }} />}
                    <it.icon className={clsx("relative size-[17px]", isActive && "text-accent")} strokeWidth={1.9} />
                    <span className="relative">{it.label}</span>
                    {"live" in it && it.live && dot && (
                      <span title={dot.text} className={clsx("relative ml-auto size-2 rounded-full", dot.tone === "pass" ? "bg-good" : dot.tone === "warn" ? "bg-warn" : "bg-bad")} />
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
      <div className="shrink-0 space-y-1 border-t border-line p-3">
        {me && (
          <div className="mb-1 flex items-center gap-3 rounded-lg px-3 py-2">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-sm font-semibold text-accent">{(me.name || me.email).slice(0, 1).toUpperCase()}</span>
            <span className="min-w-0 leading-tight">
              <span className="block truncate text-[13px] font-medium">{me.mode === "shared" ? "Shared password" : me.name}</span>
              <span className="block truncate text-[11px] text-faint">{me.mode === "shared" ? "Admin, no personal account" : `${me.role} · ${me.email}`}</span>
            </span>
          </div>
        )}
        <button type="button" onClick={toggle} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[14px] font-medium text-muted transition-colors hover:bg-sunken hover:text-fg">
          {theme === "dark" ? <Sun className="size-[17px]" strokeWidth={1.9} /> : <Moon className="size-[17px]" strokeWidth={1.9} />}
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
        <button type="button" onClick={signOut} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[14px] font-medium text-muted transition-colors hover:bg-sunken hover:text-fg">
          <LogOut className="size-[17px]" strokeWidth={1.9} />Sign out
        </button>
        <p className="px-3 pt-2 text-[11px] leading-snug text-faint">Test-mode and synthetic data only.</p>
      </div>
    </div>
  );
}

export default function AppShell() {
  const { state, reload } = useData();
  const loc = useLocation();
  const [open, setOpen] = useState(false);
  useEffect(() => { setOpen(false); window.scrollTo({ top: 0 }); }, [loc.pathname]);

  if (state.status === "unauthorized") return <Navigate to="/login" replace />;

  return (
    <MeProvider>
    <div className="min-h-screen bg-bg lg:pl-64">
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-line bg-surface lg:block"><Sidebar /></aside>

      <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-line bg-bg/80 px-4 backdrop-blur-xl lg:hidden">
        <div className="flex items-center gap-2.5"><Logo size={22} /><span className="text-[15px] font-semibold tracking-tight">Control Plane</span></div>
        <button type="button" onClick={() => setOpen(true)} aria-label="Open menu" className="rounded-lg p-2 text-muted hover:bg-sunken"><Menu className="size-5" /></button>
      </header>

      <AnimatePresence>
        {open && (
          <>
            <motion.div key="scrim" className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm lg:hidden" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={() => setOpen(false)} />
            <motion.aside key="drawer" className="fixed inset-y-0 left-0 z-50 w-72 border-r border-line bg-surface lg:hidden" initial={{ x: -300 }} animate={{ x: 0 }} exit={{ x: -300 }} transition={{ type: "spring", stiffness: 380, damping: 36 }}>
              <button type="button" onClick={() => setOpen(false)} aria-label="Close menu" className="absolute right-3 top-3 z-10 rounded-lg p-2 text-muted hover:bg-sunken"><X className="size-5" /></button>
              <Sidebar onNavigate={() => setOpen(false)} />
            </motion.aside>
          </>
        )}
      </AnimatePresence>

      <main className="mx-auto max-w-[1080px] px-4 py-8 sm:px-8 sm:py-12">
        {state.status === "loading" && <Skeleton />}
        {state.status === "error" && (
          <div className="rounded-2xl border border-line bg-surface p-8 text-center">
            <FlaskConical className="mx-auto mb-3 size-7 text-faint" />
            <p className="text-sm text-muted">Could not load the dashboard data. {state.message}</p>
            <button type="button" onClick={reload} className="mt-4 rounded-lg bg-fg px-4 py-2 text-sm font-medium text-bg">Try again</button>
          </div>
        )}
        {state.status === "ready" && (
          <motion.div key={loc.pathname.split("/").slice(0, 3).join("/")} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}>
            <Suspense fallback={<Skeleton />}>
              <Outlet />
            </Suspense>
          </motion.div>
        )}
      </main>
    </div>
    </MeProvider>
  );
}

function Skeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading">
      <div className="h-9 w-64 animate-pulse rounded-lg bg-sunken" />
      <div className="grid gap-4 sm:grid-cols-3">{[0, 1, 2].map((i) => <div key={i} className="h-36 animate-pulse rounded-2xl bg-sunken" />)}</div>
      <div className="h-72 animate-pulse rounded-2xl bg-sunken" />
    </div>
  );
}
