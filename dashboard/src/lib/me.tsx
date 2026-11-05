import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

export interface Me { email: string; name: string; role: "admin" | "viewer"; mode: "user" | "shared" }
const Ctx = createContext<Me | null>(null);

/* Who is signed in. Loaded once by the app shell; null until it answers (or if the API is not there). */
export function MeProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin", cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => setMe(j as Me | null))
      .catch(() => setMe(null));
  }, []);
  return <Ctx.Provider value={me}>{children}</Ctx.Provider>;
}
export const useMe = () => useContext(Ctx);
