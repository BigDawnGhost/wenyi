import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [
    react(),
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
