import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";
import path from "node:path";
import { computeManagerRelease } from "./manager-release.mjs";

// M4.1 content-addressed Manager identity.
//
// build_id is deterministic over actual production Manager build inputs
// (web source, public assets, dependency lock/package metadata, relevant
// Vite/TS build config and the release helper itself; sorted normalized
// paths + bytes) plus the resolved Workspace Bridge product version, which
// is embedded in `__MANAGER_RELEASE__` and `release.json`. It hashes build
// inputs, not final bundle bytes, avoiding an output/self-hashing loop. The
// product version is read from the root canonical release metadata
// (`../pyproject.toml`); the build fails clearly when no valid product
// version is available and never falls back to a literal release version.
// Test-only Playwright config and shadcn tooling metadata are excluded so
// local and Docker builds agree. The identity is injected as a compile-time
// constant AND emitted as workspace_bridge/static/dist/release.json via the
// normal Vite output. This is source/package identity, not a container image
// digest or code-signing provenance.
const managerRelease = computeManagerRelease(import.meta.dirname);

function managerReleasePlugin(release: typeof managerRelease) {
  return {
    name: "workspace-bridge-manager-release",
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    generateBundle(this: any) {
      this.emitFile({
        type: "asset",
        fileName: "release.json",
        source: JSON.stringify(release, null, 2) + "\n",
      });
    },
  };
}

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
  plugins: [
    react(),
    tailwindcss(),
    scrollLockWhitespacePlugin(),
    managerReleasePlugin(managerRelease),
  ],
  define: {
    __MANAGER_RELEASE__: JSON.stringify(managerRelease),
  },
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": "http://127.0.0.1:8766" } },
  build: { outDir: "../workspace_bridge/static/dist", emptyOutDir: true },
});
