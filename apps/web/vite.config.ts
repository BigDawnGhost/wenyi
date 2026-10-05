import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { createSandboxState, resolveFixture } from "./tests/fixtures";

// MOCK_API=1 pnpm dev:web serves the same fixture data the Playwright suites
// use, directly from the dev server — click around every page without a
// backend. Without the variable the proxy to the real API is untouched.
const sandbox = createSandboxState();

function mockApi(): Plugin {
  const enabled = process.env.MOCK_API === "1" || process.env.MOCK_API === "true";
  console.log("[mock-api] enabled =", enabled);
  return {
    name: "mock-api",
    configureServer(server) {
      if (!enabled) return;
      server.middlewares.use("/api", (req, res) => {
        const method = (req.method || "GET").toUpperCase();
        const requestPath = (req.url || "/").split("?")[0];
        const chunks: Buffer[] = [];
        req.on("data", (chunk) => chunks.push(chunk));
        req.on("end", () => {
          const rawBody =
            method === "GET" || method === "HEAD"
              ? undefined
              : Buffer.concat(chunks).toString("utf8");
          const { status, json } = resolveFixture(method, requestPath, rawBody, {}, sandbox);
          res.statusCode = status;
          res.setHeader("content-type", "application/json");
          res.end(JSON.stringify(json));
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
      "@": path.resolve(__dirname, "../../packages/ui/src"),
      "@wenyi/ui": path.resolve(__dirname, "../../packages/ui/src"),
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
