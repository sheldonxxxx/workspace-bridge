// M4.1 content-addressed release identity for the Pi host adapter.
//
// Every deployed component exposes a bounded identity:
//   { contract: 1, product: "workspace-bridge",
//     product_version: <WB release>, component: "pi-host-adapter",
//     component_version: <adapter package version>,
//     build_id: "sha256:<64 hex>" }
//
// build_id is deterministic over production adapter inputs present both in
// the source checkout and the installed rsync copy: production .mjs files
// plus package metadata/lock. test/, launchd/, README, node_modules,
// logs/state and generated files are excluded. No paths, hostnames, tokens,
// or mutable instance IDs enter the public result. This is source/package
// identity, not a container image digest or code-signing provenance.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

export const RELEASE_CONTRACT = 1;
export const RELEASE_PRODUCT = "workspace-bridge";
export const PI_COMPONENT = "pi-host-adapter";

const BUILD_ID_RE = /^sha256:[0-9a-f]{64}$/;
const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/;

let cachedBuildId = null;

export function adapterRoot(fromUrl = import.meta.url) {
  return path.dirname(fileURLToPath(fromUrl));
}

export function readPackageMeta(root = adapterRoot()) {
  const raw = fs.readFileSync(path.join(root, "package.json"), "utf8");
  const data = JSON.parse(raw);
  return {
    version: typeof data.version === "string" ? data.version : "",
    workspaceBridgeRelease: typeof data.workspaceBridgeRelease === "string"
      ? data.workspaceBridgeRelease
      : "",
  };
}

// Production inputs present in both the source checkout and the rsync
// install copy (sync_adapters_local.sh excludes test/, launchd/, README).
// Only top-level production .mjs plus tracked package metadata/lock.
export function listProductionInputs(root = adapterRoot()) {
  const entries = fs.readdirSync(root, { withFileTypes: true });
  const names = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const name = entry.name;
    if (name.endsWith(".mjs")) {
      names.push(name);
    } else if (name === "package.json" || name === "package-lock.json") {
      names.push(name);
    }
  }
  names.sort();
  return names;
}

export function computeBuildId(root = adapterRoot()) {
  const names = listProductionInputs(root);
  const hash = createHash("sha256");
  hash.update("workspace-bridge-pi-adapter-v1\x00");
  for (const name of names) {
    const data = fs.readFileSync(path.join(root, name));
    const nameBytes = Buffer.from(name, "utf8");
    const nameLen = Buffer.alloc(8);
    nameLen.writeBigUInt64BE(BigInt(nameBytes.length));
    const dataLen = Buffer.alloc(8);
    dataLen.writeBigUInt64BE(BigInt(data.length));
    hash.update(nameLen);
    hash.update(nameBytes);
    hash.update(Buffer.from([0]));
    hash.update(dataLen);
    hash.update(data);
    hash.update(Buffer.from([0]));
  }
  return `sha256:${hash.digest("hex")}`;
}

export function piBuildId(root = adapterRoot()) {
  if (cachedBuildId && root === adapterRoot()) return cachedBuildId;
  const value = computeBuildId(root);
  if (root === adapterRoot()) cachedBuildId = value;
  return value;
}

export function clearCachedBuildId() {
  cachedBuildId = null;
}

export function validateRelease(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Release identity must be an object");
  }
  const keys = Object.keys(value).sort();
  const expected = ["build_id", "component", "component_version",
    "contract", "product", "product_version"].sort();
  if (keys.length !== expected.length || !keys.every((k, i) => k === expected[i])) {
    throw new Error("Release identity has unexpected fields");
  }
  if (value.contract !== RELEASE_CONTRACT) {
    const error = new Error("Runtime release contract is unsupported");
    error.code = "unsupported";
    throw error;
  }
  if (value.product !== RELEASE_PRODUCT) throw new Error("Release product is invalid");
  if (typeof value.product_version !== "string" || !VERSION_RE.test(value.product_version)) {
    throw new Error("Release product version is invalid");
  }
  if (value.component !== PI_COMPONENT) throw new Error("Release component is invalid");
  if (typeof value.component_version !== "string" || !VERSION_RE.test(value.component_version)) {
    throw new Error("Release component version is invalid");
  }
  if (typeof value.build_id !== "string" || !BUILD_ID_RE.test(value.build_id)) {
    throw new Error("Release build ID is invalid");
  }
  return { ...value };
}

export function piRelease(root = adapterRoot()) {
  const meta = readPackageMeta(root);
  if (!meta.version || !VERSION_RE.test(meta.version)) {
    throw new Error("Pi adapter package version is invalid");
  }
  if (!meta.workspaceBridgeRelease || !VERSION_RE.test(meta.workspaceBridgeRelease)) {
    throw new Error("Workspace Bridge product version is invalid");
  }
  const value = {
    contract: RELEASE_CONTRACT,
    product: RELEASE_PRODUCT,
    product_version: meta.workspaceBridgeRelease,
    component: PI_COMPONENT,
    component_version: meta.version,
    build_id: piBuildId(root),
  };
  return validateRelease(value);
}
