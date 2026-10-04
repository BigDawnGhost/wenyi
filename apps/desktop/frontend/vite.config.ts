import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "../../../packages/ui/src"),
      "@wenyi/ui": path.resolve(__dirname, "../../../packages/ui/src"),
    },
  },
  server: { port: 5174, strictPort: true },
  clearScreen: false,
});
