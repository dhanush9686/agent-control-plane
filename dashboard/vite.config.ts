import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

/* Local development only: serves data/*.json where the deployed site uses the login-protected /api functions.
   There is no login locally; any password works. Nothing here ships to production. */
function devApi(): Plugin {
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
        if (url.pathname === "/data") {
          const file = url.searchParams.get("file") ?? "";
          if (!/^[a-z]+$/.test(file)) return send(404, { error: "No such data file." });
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
