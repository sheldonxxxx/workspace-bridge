import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";
import path from "node:path";

function scrollLockWhitespacePlugin(): Plugin {
  return {
    name: "workspace-bridge-scroll-lock-whitespace",
    apply: "build",
    transform(code, id) {
      if (
        !/\/react-remove-scroll-bar\/dist\/es(?:5|2015|2019)\/component\.js$/.test(
          id,
        )
      ) {
        return null;
      }
      // This dependency embeds CSS with indented blank lines. Normalize only
      // this module's CSS blanks, including escaped newlines in its ES5 build,
      // before minification turns them into multiline template literals.
      return {
        code: code.replace(/^[ \t]+$/gm, "").replace(/\\n[ \t]+\\n/g, "\\n\\n"),
        map: null,
      };
    },
  };
}

// https://vite.dev/config/
export default defineConfig({
  base: "/static/dist/",
  plugins: [react(), tailwindcss(), scrollLockWhitespacePlugin()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": "http://127.0.0.1:8766" } },
  build: { outDir: "../workspace_bridge/static/dist", emptyOutDir: true },
});
