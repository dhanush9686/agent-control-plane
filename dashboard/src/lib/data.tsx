import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Assure, Live } from "./types";

type State =
  | { status: "loading" }
  | { status: "unauthorized" }
  | { status: "error"; message: string }
  | { status: "ready"; assure: Assure; live: Live | null };

interface Value { state: State; reload: () => void }
const Ctx = createContext<Value>({ state: { status: "loading" }, reload: () => {} });

/* Data comes through the login-protected /api/data function. A 401 means "not signed in". */
async function getJson<T>(file: string): Promise<{ code: number; body?: T }> {
  const r = await fetch("/api/data?file=" + file, { cache: "no-store", credentials: "same-origin" });
  if (!(r.headers.get("content-type") ?? "").includes("json")) return { code: r.status === 200 ? 502 : r.status };
  if (r.status === 401) return { code: 401 };
  return r.ok ? { code: 200, body: (await r.json()) as T } : { code: r.status, body: (await r.json().catch(() => undefined)) as T | undefined };
}

export function DataProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const a = await getJson<Assure>("assure");
        if (cancelled) return;
        if (a.code === 401) return setState({ status: "unauthorized" });
        if (a.code !== 200 || !a.body) return setState({ status: "error", message: "The data service answered with HTTP " + a.code + "." });
        const l = await getJson<Live>("live");
        if (cancelled) return;
        if (l.code === 401) return setState({ status: "unauthorized" });
        let live: Live | null = null;
        if (l.code === 200 && l.body) live = l.body;
        else if (l.code === 502) live = { error: "The live data store could not be reached. Showing saved results." };
        setState({ status: "ready", assure: a.body, live });
      } catch (e) {
        if (!cancelled) setState({ status: "error", message: e instanceof Error ? e.message : "Network error." });
      }
    })();
    return () => { cancelled = true; };
  }, [tick]);

  const value = useMemo(() => ({ state, reload }), [state, reload]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
export const useData = () => useContext(Ctx);

/* Pages inside the shell only render once data is ready. */
export function useReady() {
  const { state, reload } = useData();
  if (state.status !== "ready") throw new Error("Data not ready");
  return { assure: state.assure, live: state.live, reload };
}
