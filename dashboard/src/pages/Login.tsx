import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { motion } from "motion/react";
import { ArrowLeft, Eye, EyeOff, Loader2, ShieldCheck } from "lucide-react";
import { Logo } from "@/components/Logo";
import { ArchitectureGraph } from "@/components/domain";

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [mode, setMode] = useState<"user" | "shared">("shared");
  const [pw, setPw] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  /* Already signed in? Go straight in. Otherwise learn whether this site uses personal accounts. */
  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin", cache: "no-store" })
      .then(async (r) => {
        if (r.ok) return navigate("/app", { replace: true });
        const j = (await r.json().catch(() => ({}))) as { mode?: string };
        if (j.mode === "user") setMode("user");
      })
      .catch(() => { /* no API here (static preview): keep the simple form */ });
  }, [navigate]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(mode === "user" ? { email, password: pw } : { password: pw }), credentials: "same-origin" });
      if (r.ok) { navigate("/app", { replace: true }); return; }
      const j = (await r.json().catch(() => ({}))) as { error?: string };
      setErr(j.error ?? `Sign-in failed (HTTP ${r.status}).`);
    } catch {
      setErr("Could not reach the sign-in service.");
    }
    setBusy(false);
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[minmax(420px,1fr)_1.15fr]">
      <div className="flex flex-col px-6 py-8 sm:px-12">
        <Link to="/" className="inline-flex w-fit items-center gap-2 text-sm text-muted transition-colors hover:text-fg"><ArrowLeft className="size-4" />Back to home</Link>
        <div className="m-auto w-full max-w-[360px] py-12">
          <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}>
            <Logo size={56} />
            <h1 className="mt-6 text-[28px] font-semibold tracking-tight">Welcome back</h1>
            <p className="mt-1.5 text-[15px] text-muted">Sign in to open the Control Plane dashboard.</p>
            <form onSubmit={submit} className="mt-8 space-y-4">
              {mode === "user" && (
                <div>
                  <label htmlFor="email" className="mb-1.5 block text-sm font-medium">Email</label>
                  <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus autoComplete="username"
                    className="h-11 w-full rounded-xl border border-line-strong bg-surface px-3.5 text-[15px] outline-none transition-shadow placeholder:text-faint focus:border-accent focus:ring-4 focus:ring-accent-soft" placeholder="you@example.com" />
                </div>
              )}
              <div>
                <label htmlFor="pw" className="mb-1.5 block text-sm font-medium">Password</label>
                <div className="relative">
                  <input id="pw" type={show ? "text" : "password"} value={pw} onChange={(e) => setPw(e.target.value)} required autoFocus={mode === "shared"} autoComplete="current-password"
                    className="h-11 w-full rounded-xl border border-line-strong bg-surface px-3.5 pr-11 text-[15px] outline-none transition-shadow placeholder:text-faint focus:border-accent focus:ring-4 focus:ring-accent-soft" placeholder="Enter password" />
                  <button type="button" onClick={() => setShow(!show)} aria-label={show ? "Hide password" : "Show password"} aria-pressed={show}
                    className="absolute right-1.5 top-1.5 rounded-lg p-2 text-faint transition-colors hover:text-fg">
                    {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>
              <p role="alert" aria-live="assertive" className="min-h-5 text-sm text-bad">{err}</p>
              <motion.button whileTap={{ scale: 0.98 }} type="submit" disabled={busy || !pw || (mode === "user" && !email)}
                className="flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-fg text-[15px] font-medium text-bg transition-opacity disabled:opacity-50">
                {busy && <Loader2 className="size-4 animate-spin" />}{busy ? "Signing in…" : "Sign in"}
              </motion.button>
            </form>
            <p className="mt-6 text-xs leading-relaxed text-faint">Test-mode data only. Ask the project owner for an account.</p>
          </motion.div>
        </div>
      </div>

      <div className="relative hidden overflow-hidden border-l border-line bg-sunken lg:block">
        <div className="grid-bg absolute inset-0 opacity-70" />
        <motion.div className="absolute -right-24 -top-24 size-[420px] rounded-full bg-accent/25 blur-3xl" animate={{ x: [0, -30, 0], y: [0, 30, 0] }} transition={{ duration: 14, repeat: Infinity, ease: "easeInOut" }} />
        <div className="relative flex h-full flex-col justify-center px-14">
          <motion.div initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2, duration: 0.6 }}>
            <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-line bg-surface/80 px-3 py-1 text-xs font-medium text-muted backdrop-blur"><ShieldCheck className="size-3.5 text-accent" />Every request is checked</div>
            <h2 className="max-w-md text-[30px] font-semibold leading-tight tracking-tight">The AI never touches the records directly.</h2>
            <div className="mt-8 rounded-2xl border border-line bg-surface/90 p-5 shadow-card backdrop-blur"><ArchitectureGraph flow /></div>
          </motion.div>
        </div>
      </div>
    </div>
  );
}
