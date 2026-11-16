import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { frontendManualChunks } from "../../packages/ui/vite.shared";
import { createSandboxState, resolveFixture } from "../../packages/ui/sandbox/fixtures";
import { attachProgressServer } from "../../packages/ui/sandbox/progress-server";
import { matchFault, pickScenario } from "../../packages/ui/sandbox/scenarios";

// MOCK_API=1 pnpm dev:web serves the same fixture data the Playwright suites
// use, directly from the dev server — click around every page without a
// backend. The progress WebSocket on /ws is simulated with the same state
// machine, and MOCK_SCENARIO=<id> selects a scenario from
// packages/ui/sandbox/scenarios.ts. Without the variables the proxy to the
// real API is untouched.
const sandbox = createSandboxState();

function mockApi(): Plugin {
  const enabled = process.env.MOCK_API === "1" || process.env.MOCK_API === "true";
  const scenario = pickScenario(process.env.MOCK_SCENARIO);
  console.log("[mock-api] enabled =", enabled, "scenario =", scenario.id);
  return {
    name: "mock-api",
    configureServer(server) {
      if (!enabled) return;
      scenario.seed?.(sandbox);
      if (server.httpServer) attachProgressServer(server.httpServer, sandbox);
      server.middlewares.use("/api", (req, res) => {
        const method = (req.method || "GET").toUpperCase();
        const requestPath = (req.url || "/").split("?")[0];
        const chunks: Buffer[] = [];
        req.on("data", (chunk) => chunks.push(chunk));
        req.on("end", async () => {
          const rawBody =
            method === "GET" || method === "HEAD"
              ? undefined
              : Buffer.concat(chunks).toString("utf8");
          try {
            const fault = matchFault(scenario, method, requestPath);
            if (fault?.kind === "delay")
              await new Promise((resolve) => setTimeout(resolve, fault.delayMs ?? 1_000));
            if (fault?.kind === "network") {
              res.destroy();
              return;
            }
            const { status, json } =
              fault && fault.kind === "http"
                ? {
                    status: fault.status ?? 500,
                    json: fault.body ?? { detail: fault.detail ?? "Injected fault" },
                  }
                : resolveFixture(method, requestPath, rawBody, scenario.overrides ?? {}, sandbox, {
                    unknownWrite: scenario.unknownWrite,
                  });
            res.statusCode = status;
            res.setHeader("content-type", "application/json");
            res.end(JSON.stringify(json));
          } catch (error) {
            res.statusCode = 500;
            res.setHeader("content-type", "application/json");
            res.end(JSON.stringify({ detail: String(error) }));
          }
        });
      });
    },
  };
}

export default defineConfig({
  plugins: [
    react(),
    mockApi(),
    {
      name: "browser-platform-boundary",
      generateBundle(_options, bundle) {
        for (const item of Object.values(bundle)) {
          if (item.type !== "chunk") continue;
          for (const id of Object.keys(item.modules)) {
            if (/\/apps\/desktop\/|\/native(?:Drop|Export)\./.test(id.replace(/\\/g, "/")))
              this.error(`Desktop module entered the Web bundle: ${id}`);
          }
          if (/__TAURI|__WENYI_DESKTOP|native_drop_|native_export_|\/desktop\/credentials|System credential store|系统凭据库/.test(item.code))
            this.error(`Desktop code or credential UI entered Web chunk: ${item.fileName}`);
        }
      },
    },
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // Explicit floor for the Tauri WebView baseline (Safari 15.4 / WebKitGTK 2.36).
    target: "safari15",
    rollupOptions: {
      output: { manualChunks: frontendManualChunks },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
