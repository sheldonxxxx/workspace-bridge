import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import path from "node:path";

// https://vite.dev/config/
export default defineConfig({
  base: "/static/dist/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": "http://127.0.0.1:8766" } },
  build: { outDir: "../workspace_bridge/static/dist", emptyOutDir: true },
});
