import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

/* Local development only: serves data/*.json where the deployed site uses the login-protected /api functions.
   There is no login locally; any password works. Nothing here ships to production. */
function devApi(): Plugin {
  let approvals: { approvals: Array<{ id: string; state: string; tier: string; decision: unknown }> } | null = null;
  const devApprovals = () => {
    const fx = resolve(__dirname, "data", "approvals.dev.json"); // optional, git-ignored; decisions made in dev live in memory
    approvals ??= existsSync(fx) ? JSON.parse(readFileSync(fx, "utf8")) : { via: "none", configured: false, approvals: [] };
    return approvals!;
  };
  return {
    name: "acp-dev-api",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/api", (req, res) => {
        const url = new URL(req.url ?? "/", "http://localhost");
        const send = (code: number, body: unknown) => {
          res.statusCode = code;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify(body));
        };
        if (url.pathname === "/login" || url.pathname === "/logout") return send(200, { ok: true });
        if (url.pathname === "/me") return send(200, { email: "dev@local", name: "Dev", role: "admin", mode: "user" });
        if (url.pathname === "/simulate") return send(200, { id: "sim_0000000000000000" });
        if (url.pathname === "/report") {
          const fmt = url.searchParams.get("format") ?? "html";
          const fx = resolve(__dirname, "data", `report.dev.${fmt}`); // optional, git-ignored
          if (!/^(html|json|csv)$/.test(fmt) || !existsSync(fx)) return send(404, { error: "No report has been published yet." });
          res.setHeader("Content-Type", { html: "text/html", json: "application/json", csv: "text/csv" }[fmt] as string);
          return res.end(readFileSync(fx, "utf8"));
        }
        if (url.pathname === "/approvals" && req.method === "POST") {
          let raw = "";
          req.on("data", (c) => (raw += c));
          req.on("end", () => {
            const b = JSON.parse(raw || "{}") as { id?: string; decision?: string; note?: string };
            const a = devApprovals().approvals.find((x: { id: string }) => x.id === b.id);
            if (!a) return send(404, { error: "That approval no longer exists." });
            if (a.state !== "pending") return send(409, { error: "Someone already decided this one." });
            if (a.tier === "senior" && b.decision === "approve" && (b.note ?? "").trim().length < 5) return send(400, { error: "A senior-tier approval needs a written reason." });
            a.state = b.decision === "approve" ? "approved" : "rejected";
            a.decision = { by: "Dev", note: b.note ?? "", at: new Date().toISOString() };
            return send(200, { ok: true, state: a.state });
          });
          return;
        }
        if (url.pathname === "/data") {
          const file = url.searchParams.get("file") ?? "";
          if (!/^[a-z]+$/.test(file)) return send(404, { error: "No such data file." });
          if (file === "sim") {
            const fx = resolve(__dirname, "data", "sim.dev.json"); // optional, git-ignored
            return send(200, existsSync(fx) ? JSON.parse(readFileSync(fx, "utf8")) : { via: "none", configured: false, queue: 0, jobs: [] });
          }
          if (file === "approvals") return send(200, devApprovals());
          if (file === "report") {
            const fx = resolve(__dirname, "data", "reportmeta.dev.json");
            const meta = existsSync(fx) ? JSON.parse(readFileSync(fx, "utf8")) : null;
            return send(200, { via: "none", configured: !!meta, report: meta });
          }
          if (file === "events") {
            // Optional local fixture (data/events.dev.json, git-ignored, or the path in ACP_DEV_EVENTS) so the page can be developed without Redis.
            const fx = process.env.ACP_DEV_EVENTS ?? resolve(__dirname, "data", "events.dev.json");
            return send(200, fx && existsSync(fx) ? JSON.parse(readFileSync(fx, "utf8")) : { via: "none", configured: false, queue: 0, events: [] });
          }
          const path = resolve(__dirname, "data", file + ".json");
          if (!existsSync(path)) return send(404, { error: "No such data file." });
          const json = JSON.parse(readFileSync(path, "utf8"));
          return send(200, file === "live" ? { via: "bundle", ...json } : json);
        }
        return send(404, { error: "Not found." });
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), devApi()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  build: { sourcemap: false, chunkSizeWarningLimit: 700 },
});
