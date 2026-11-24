import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { frontendManualChunks } from "../../../packages/ui/vite.shared.ts";
import { thirdPartyNotices } from "./scripts/third-party-notices.ts";

export default defineConfig({
  plugins: [react(), thirdPartyNotices()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    // Explicit floor for the Tauri WebView baseline (Safari 15.4 / WebKitGTK 2.36).
    target: "safari15",
    rollupOptions: {
      output: { manualChunks: frontendManualChunks },
    },
  },
  server: { port: 5174, strictPort: true },
  clearScreen: false,
});
