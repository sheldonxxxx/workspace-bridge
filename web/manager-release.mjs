// M4.1 Manager release identity helper (Node-only).
//
// Pure, testable Manager build-identity logic for `web/vite.config.ts`.
// The Workspace Bridge product version is read from the root canonical
// release metadata (`../pyproject.toml` relative to this web root) and the
// build fails clearly when no valid product version is available. There is
// intentionally no hard-coded literal release fallback.
//
// `build_id` is deterministic over actual production Manager build inputs
// (web source, public assets, dependency lock/package metadata and relevant
// Vite/TS build config plus this helper itself; sorted normalized paths +
// bytes) plus the resolved product version, which is embedded in
// `__MANAGER_RELEASE__` and `release.json`. A product-version-only bump
// therefore changes `build_id`. Test-only Playwright config and shadcn
// tooling metadata (`components.json`) are not production build inputs and
// are excluded so local and Docker builds agree.
//
// This module uses Node APIs and must never be bundled into the browser;
// browser-safe validation/comparison lives in
// `src/lib/manager-identity.ts`.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

export const RELEASE_CONTRACT = 1;
export const RELEASE_PRODUCT = "workspace-bridge";
export const RELEASE_COMPONENT = "manager";
export const MANAGER_HASH_DOMAIN = "workspace-bridge-manager-v1";

export const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/;
export const BUILD_ID_RE = /^sha256:[0-9a-f]{64}$/;

// Production top-level build inputs. `src/` and `public/` are walked
// separately. `playwright.config.ts` (test-only) and `components.json`
// (shadcn tooling metadata, not consumed by `npm run build`) are
// intentionally excluded. Keep in sync with the Dockerfile web-builder
// COPY list and `collectManagerInputFiles`.
export const PRODUCTION_TOP_LEVEL_FILES = [
  "index.html",
  "manager-release.d.mts",
  "manager-release.mjs",
  "package-lock.json",
  "package.json",
  "tsconfig.app.json",
  "tsconfig.json",
  "tsconfig.node.json",
  "vite.config.ts",
];

export function adapterRoot(fromUrl = import.meta.url) {
  return path.dirname(fileURLToPath(fromUrl));
}

export function canonicalPyprojectPath(webRoot = adapterRoot()) {
  return path.resolve(webRoot, "../pyproject.toml");
}

export function parseProductVersionFromPyprojectText(text, sourceLabel) {
  const label = sourceLabel || "pyproject.toml";
  const match = text.match(/version\s*=\s*["']([^"']+)["']/);
  if (!match) {
    throw new Error(
      `Manager build requires a valid product version in ${label}: no version field found`,
    );
  }
  const version = match[1];
  if (typeof version !== "string" || !VERSION_RE.test(version)) {
    throw new Error(
      `Manager build requires a valid product version in ${label}: invalid version ${JSON.stringify(version)}`,
    );
  }
  return version;
}

export function readProductVersion(webRoot = adapterRoot()) {
  const file = canonicalPyprojectPath(webRoot);
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (error) {
    throw new Error(
      `Manager build requires canonical product metadata at ${file}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parseProductVersionFromPyprojectText(text, file);
}

export function validateManagerRelease(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Release identity must be an object");
  }
  const keys = Object.keys(value).sort();
  const expected = [
    "build_id",
    "component",
    "component_version",
    "contract",
    "product",
    "product_version",
  ].sort();
  if (
    keys.length !== expected.length ||
    !keys.every((k, i) => k === expected[i])
  ) {
    throw new Error("Release identity has unexpected fields");
  }
  if (value.contract !== RELEASE_CONTRACT) {
    const error = new Error("Runtime release contract is unsupported");
    error.code = "unsupported";
    throw error;
  }
  if (value.product !== RELEASE_PRODUCT) {
    throw new Error("Release product is invalid");
  }
  if (
    typeof value.product_version !== "string" ||
    !VERSION_RE.test(value.product_version)
  ) {
    throw new Error("Release product version is invalid");
  }
  if (value.component !== RELEASE_COMPONENT) {
    throw new Error("Release component is invalid");
  }
  if (
    typeof value.component_version !== "string" ||
    !VERSION_RE.test(value.component_version)
  ) {
    throw new Error("Release component version is invalid");
  }
  if (typeof value.build_id !== "string" || !BUILD_ID_RE.test(value.build_id)) {
    throw new Error("Release build ID is invalid");
  }
  return { ...value };
}

export function collectManagerInputFiles(webRoot = adapterRoot()) {
  const files = [];
  const pushFile = (file) => {
    try {
      if (fs.statSync(file).isFile()) files.push(file);
    } catch {
      // Missing optional inputs are deployment noise, not identity inputs.
    }
  };
  for (const name of PRODUCTION_TOP_LEVEL_FILES) {
    pushFile(path.join(webRoot, name));
  }
  const walk = (dir) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === "node_modules" || entry.name === "dist") continue;
        walk(full);
      } else if (entry.isFile()) {
        files.push(full);
      }
    }
  };
  walk(path.join(webRoot, "src"));
  walk(path.join(webRoot, "public"));
  return files;
}

function toRelPosix(webRoot, file) {
  return path.relative(webRoot, file).split(path.sep).join("/");
}

export function readManagerInputEntries(webRoot = adapterRoot()) {
  const files = collectManagerInputFiles(webRoot);
  const entries = [];
  for (const file of files) {
    const rel = toRelPosix(webRoot, file);
    if (rel.startsWith("../") || rel === "static/dist/release.json") continue;
    const data = fs.readFileSync(path.join(webRoot, rel));
    entries.push({ rel, data });
  }
  entries.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
  return entries;
}

export function computeManagerBuildId(productVersion, entries) {
  if (typeof productVersion !== "string" || !VERSION_RE.test(productVersion)) {
    throw new Error("Manager build requires a valid product version");
  }
  const sorted = [...entries].sort((a, b) =>
    a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0,
  );
  const hash = createHash("sha256");
  hash.update(`${MANAGER_HASH_DOMAIN}\x00`);
  // The resolved product version is embedded in `__MANAGER_RELEASE__` and
  // `release.json`, so it is a compiled release input: a version-only bump
  // must change `build_id`.
  hash.update("product-version\x00");
  const productBytes = Buffer.from(productVersion, "utf8");
  const productLen = Buffer.alloc(8);
  productLen.writeBigUInt64BE(BigInt(productBytes.length));
  hash.update(productLen);
  hash.update(productBytes);
  hash.update(Buffer.from([0]));
  for (const { rel, data } of sorted) {
    const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data);
    const nameBytes = Buffer.from(rel, "utf8");
    const nameLen = Buffer.alloc(8);
    nameLen.writeBigUInt64BE(BigInt(nameBytes.length));
    const dataLen = Buffer.alloc(8);
    dataLen.writeBigUInt64BE(BigInt(bytes.length));
    hash.update(nameLen);
    hash.update(nameBytes);
    hash.update(Buffer.from([0]));
    hash.update(dataLen);
    hash.update(bytes);
    hash.update(Buffer.from([0]));
  }
  return `sha256:${hash.digest("hex")}`;
}

export function computeManagerRelease(webRoot = adapterRoot()) {
  const productVersion = readProductVersion(webRoot);
  const entries = readManagerInputEntries(webRoot);
  const buildId = computeManagerBuildId(productVersion, entries);
  return validateManagerRelease({
    contract: RELEASE_CONTRACT,
    product: RELEASE_PRODUCT,
    product_version: productVersion,
    component: RELEASE_COMPONENT,
    component_version: productVersion,
    build_id: buildId,
  });
}
