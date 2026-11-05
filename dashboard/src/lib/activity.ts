import { useCallback, useEffect, useState } from "react";
import type { EventsFeed, SimFeed } from "./types";

/* The two live feeds (webhook events and simulator jobs), polled every few seconds while the tab is visible. */
export function useActivity(pollMs = 5000) {
  const [events, setEvents] = useState<EventsFeed | null>(null);
  const [sim, setSim] = useState<SimFeed | null>(null);

  const load = useCallback(async () => {
    const get = async <T,>(file: string): Promise<T | null> => {
      try {
        const r = await fetch("/api/data?file=" + file, { cache: "no-store", credentials: "same-origin" });
        return r.ok ? ((await r.json()) as T) : null;
      } catch { return null; }
    };
    const [e, s] = await Promise.all([get<EventsFeed>("events"), get<SimFeed>("sim")]);
    if (e) setEvents(e);
    if (s) setSim(s);
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, pollMs);
    return () => window.clearInterval(t);
  }, [load, pollMs]);

  return { events, sim };
}
