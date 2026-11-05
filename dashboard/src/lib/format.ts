export const pct = (v: number | null | undefined) => (v == null ? "n/a" : (v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0) + "%");
export const pct1 = (v: number | null | undefined) => (v == null ? "n/a" : (v * 100).toFixed(1) + "%");
export const ms = (v: number | null | undefined) => (v == null ? "n/a" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
export const usd = (v: number | null | undefined) => (v == null ? "n/a" : "$" + v.toFixed(5));
export const rupees = (paise: number) =>
  "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const shortModel = (m: string | undefined) => (m ?? "").replace("apac.", "").replace("amazon.", "").replace("-v1:0", "");
export const clip = (s: unknown, n: number) => {
  const t = String(s);
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
};

export function ageText(iso: string): string {
  const m = Math.max(0, (Date.now() - Date.parse(iso)) / 60000);
  if (!isFinite(m)) return "unknown time";
  if (m < 60) return Math.max(1, Math.round(m)) + " min ago";
  if (m < 2880) return Math.round(m / 60) + " h ago";
  return Math.round(m / 1440) + " days ago";
}

export type Tone = "pass" | "warn" | "fail";
export function freshness(finishedAt: string, expectedHours: number | undefined, via: string | undefined): { tone: Tone; text: string } {
  if (via === "bundle") return { tone: "warn", text: "Saved copy" };
  const hours = (Date.now() - Date.parse(finishedAt)) / 3600000;
  const every = expectedHours || 168;
  if (hours <= every * 1.25) return { tone: "pass", text: "Up to date" };
  if (hours <= every * 2) return { tone: "warn", text: "Getting stale" };
  return { tone: "fail", text: "Overdue" };
}
