// Deterministic enforcement fingerprint (milestone 3C1).
//
// Computes a SHA-256 fingerprint at process startup over the
// package-owned enforcement modules (adapter/rpc/policy/trusted
// extension/config/server). A changed fingerprint is audit evidence,
// not an automatic refusal. No full package paths are exposed: only
// the hex digest plus module basenames.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const FINGERPRINT_MODULES = [
  "adapter.mjs",
  "rpc.mjs",
  "policy.mjs",
  "trusted-permission-extension.mjs",
  "config.mjs",
  "server.mjs",
  "executions.mjs",
  "extensions.mjs",
];

function packageDir() {
  return path.dirname(fileURLToPath(import.meta.url));
}

export function computeEnforcementFingerprint() {
  const dir = packageDir();
  const hash = createHash("sha256");
  const seen = [];
  for (const base of [...FINGERPRINT_MODULES].sort()) {
    const full = path.join(dir, base);
    let bytes;
    try {
      bytes = fs.readFileSync(full);
    } catch {
      // Missing module fails the fingerprint deterministically (hash of
      // the basename marker), never silently skipped.
      hash.update(`missing:${base}\n`, "utf8");
      seen.push(base);
      continue;
    }
    hash.update(`file:${base}:${bytes.length}\n`, "utf8");
    hash.update(bytes);
    hash.update("\n", "utf8");
    seen.push(base);
  }
  return { fingerprint: hash.digest("hex"), modules: seen };
}

let cached = null;
export function enforcementFingerprint() {
  if (!cached) cached = computeEnforcementFingerprint();
  return cached;
}
