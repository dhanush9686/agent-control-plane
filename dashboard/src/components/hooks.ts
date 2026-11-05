import { useEffect, useState } from "react";

export function useNarrow(breakpoint = 760) {
  const [narrow, setNarrow] = useState(() => window.innerWidth < breakpoint);
  useEffect(() => {
    const on = () => setNarrow(window.innerWidth < breakpoint);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [breakpoint]);
  return narrow;
}
