import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

type Theme = "light" | "dark";
const Ctx = createContext<{ theme: Theme; toggle: () => void }>({ theme: "light", toggle: () => {} });

function initial(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark" || attr === "light") return attr;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(initial);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      try { localStorage.setItem("acp-theme", next); } catch { /* storage blocked: fine */ }
      return next;
    });
  }, []);
  const value = useMemo(() => ({ theme, toggle }), [theme, toggle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
export const useTheme = () => useContext(Ctx);
