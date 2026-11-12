import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { frontendManualChunks } from "../../../packages/ui/vite.shared";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    rollupOptions: {
      output: { manualChunks: frontendManualChunks },
    },
  },
  server: { port: 5174, strictPort: true },
  clearScreen: false,
});
