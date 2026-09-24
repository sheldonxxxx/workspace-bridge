// 3C2 extension inventory/policy/loadout/audit: fixture agentDir +
// managed npm package fixtures, identity parsing, object/string settings
// forms, bounds, manifest/conventional extensions, missing/symlink/
// malformed cases, no path leaks, and the absence of a public inventory route,
// explicit extension-root loading with auto-discovery off against
// installed Pi 0.87.0, excludeTools loadout, permission pass-through, and
// generic audit bounds.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { sdkExcludeTools } from "../config.mjs";
import {
  canonicalizeEnabledOrder,
  defaultExtensionPolicy,
  extensionRevision,
  isValidExtensionId,
  normalizeExtensionId,
  parseNpmSource,
  parseNpmSpec,
  readExtensionInventory,
  resolveEnabledExtensionRoots,
  resolvePackageExtensionResources,
  validateExtensionPolicy,
} from "../extensions.mjs";
import {
  redactExtensionText,
  summarizeExtensionInput,
  summarizeExtensionResult,
} from "../executions.mjs";
import { MANAGED_TOOLS } from "../trusted-permission-extension.mjs";
import { createPiAdapterServer } from "../server.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";

// ------------------------------------------------------------ fixtures
function writePackage(root, name, { version = "1.0.0", manifest = null, conventional = false, index = false } = {}) {
  const dir = path.join(root, "npm", "node_modules", name);
  fs.mkdirSync(dir, { recursive: true });
  const pkg = { name, version };
  if (manifest !== null) pkg.pi = { extensions: manifest };
  fs.writeFileSync(path.join(dir, "package.json"), JSON.stringify(pkg));
  if (manifest) {
    for (const entry of manifest) {
      const full = path.join(dir, entry);
      fs.mkdirSync(path.dirname(full), { recursive: true });
      fs.writeFileSync(full, "export default function (pi) {}\n");
    }
  }
  if (conventional) {
    const ext = path.join(dir, "extensions");
    fs.mkdirSync(ext, { recursive: true });
    fs.writeFileSync(path.join(ext, "extra.js"), "export default function (pi) {}\n");
  }
  if (index) {
    fs.writeFileSync(path.join(dir, "index.js"), "export default function (pi) {}\n");
  }
  return dir;
}

function makeAgentDir(packages) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-"));
  const agentDir = fs.realpathSync(tmp);
  fs.writeFileSync(path.join(agentDir, "settings.json"),
    JSON.stringify({ packages }));
  return agentDir;
}

// Write package.json WITHOUT materializing manifest entries (for
// fail-closed bound tests where entries must be validated, not assumed).
function writePackageJsonOnly(root, name, manifest) {
  const dir = path.join(root, "npm", "node_modules", name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "package.json"),
    JSON.stringify({ name, version: "1.0.0", pi: { extensions: manifest } }));
  return dir;
}

function materializeFile(dir, rel) {
  const full = path.join(dir, rel);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, "export default function (pi) {}\n");
  return full;
}

// ------------------------------------------------------------ identity
test("npm identity parsing covers scoped/unscoped/pinned specs", () => {
  assert.deepEqual(parseNpmSpec("pi-web-access"), { name: "pi-web-access", version: undefined });
  assert.deepEqual(parseNpmSpec("@scope/pkg"), { name: "@scope/pkg", version: undefined });
  assert.deepEqual(parseNpmSpec("@scope/pkg@1.2.3"), { name: "@scope/pkg", version: "1.2.3" });
  assert.deepEqual(parseNpmSpec("pkg@^2.0.0"), { name: "pkg", version: "^2.0.0" });
  assert.equal(normalizeExtensionId("npm:pi-web-access"), "npm:pi-web-access");
  assert.equal(normalizeExtensionId("npm:pi-web-access@1.0.0"), "npm:pi-web-access");
  assert.equal(normalizeExtensionId("npm:@scope/pkg@^2.0.0"), "npm:@scope/pkg");
  assert.equal(normalizeExtensionId("npm:@scope/pkg"), "npm:@scope/pkg");
  assert.equal(normalizeExtensionId("git:example.com/org/repo"), "");
  assert.equal(normalizeExtensionId("./local/path"), "");
  assert.equal(normalizeExtensionId("npm:"), "");
  assert.equal(normalizeExtensionId(""), "");
  assert.equal(normalizeExtensionId(null), "");
  assert.ok(isValidExtensionId("npm:pi-web-access"));
  assert.ok(isValidExtensionId("npm:@scope/pkg"));
  assert.ok(!isValidExtensionId("npm:pi-web-access@1.0.0"));
  assert.ok(!isValidExtensionId("/abs/path"));
});

test("extension policy validates strictly with stable revision", () => {
  const policy = validateExtensionPolicy({ version: 1, enabled: ["npm:a", "npm:@s/b"] });
  assert.deepEqual(policy, { version: 1, enabled: ["npm:a", "npm:@s/b"] });
  assert.deepEqual(defaultExtensionPolicy(), { version: 1, enabled: [] });
  assert.equal(extensionRevision(policy).length, 64);
  assert.equal(extensionRevision({ version: 1, enabled: ["npm:a", "npm:@s/b"] }),
    extensionRevision(policy));
  assert.throws(() => validateExtensionPolicy({ version: 1, enabled: ["npm:a", "npm:a"] }));
  assert.throws(() => validateExtensionPolicy({ version: 1, enabled: ["npm:a@1.0.0"] }));
  assert.throws(() => validateExtensionPolicy({ version: 1, enabled: ["/x"] }));
  assert.throws(() => validateExtensionPolicy({ version: 2, enabled: [] }));
  assert.throws(() => validateExtensionPolicy({ version: 1 }));
  assert.throws(() => validateExtensionPolicy({ version: 1, enabled: [], extra: 1 }));
});

// ------------------------------------------------------------ inventory
test("inventory reads string/object settings forms with manifest + index rows", () => {
  const agentDir = makeAgentDir(["npm:alpha", { source: "npm:beta@2.0.0" }, "npm:plain"]);
  writePackage(agentDir, "alpha", { manifest: ["./ext.js"] });
  writePackage(agentDir, "beta", { manifest: ["./lib/e.js", "./lib/f.js"] });
  writePackage(agentDir, "plain", { index: true });
  const { packages, error } = readExtensionInventory(agentDir);
  assert.equal(error, undefined);
  assert.equal(packages.length, 3);
  const alpha = packages.find((p) => p.id === "npm:alpha");
  assert.equal(alpha.name, "alpha");
  assert.equal(alpha.version, "1.0.0");
  assert.equal(alpha.supported, true);
  assert.equal(alpha.has_extensions, true);
  assert.deepEqual(alpha.extensions, ["ext.js"]);
  assert.equal(alpha.extension_count, 1);
  assert.equal(alpha.package_json_sha256.length, 64);
  const beta = packages.find((p) => p.id === "npm:beta");
  assert.equal(beta.version, "1.0.0");
  assert.equal(beta.extension_count, 2);
  assert.deepEqual(beta.extensions, ["lib/e.js", "lib/f.js"]);
  const plain = packages.find((p) => p.id === "npm:plain");
  assert.equal(plain.supported, true);
  assert.equal(plain.has_extensions, true);
  assert.deepEqual(plain.extensions, ["index.js"]);
  const serialized = JSON.stringify(packages);
  assert.ok(!serialized.includes(agentDir));
  assert.ok(!serialized.includes("node_modules"));
});

test("inventory marks missing/malformed/non-extension packages unsupported", () => {
  const agentDir = makeAgentDir(["npm:gone", "npm:noext", "git:example.com/o/r"]);
  writePackage(agentDir, "noext", {});
  const { packages } = readExtensionInventory(agentDir);
  const gone = packages.find((p) => p.id === "npm:gone");
  assert.equal(gone.supported, false);
  assert.equal(gone.reason, "not_installed");
  const noext = packages.find((p) => p.id === "npm:noext");
  assert.equal(noext.supported, false);
  assert.equal(noext.reason, "no_extension_resources");
  const git = packages.find((p) => p.supported === false && p.reason === "unsupported_source");
  assert.ok(git);
});

test("inventory rejects symlink escapes and malformed JSON without path leaks", () => {
  const agentDir = makeAgentDir(["npm:evil"]);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-outside-"));
  fs.writeFileSync(path.join(outside, "package.json"),
    JSON.stringify({ name: "evil", version: "9.9.9", pi: { extensions: ["./e.js"] } }));
  const linkPath = path.join(agentDir, "npm", "node_modules", "evil");
  fs.mkdirSync(path.dirname(linkPath), { recursive: true });
  fs.symlinkSync(outside, linkPath);
  const { packages } = readExtensionInventory(agentDir);
  const evil = packages.find((p) => p.id === "npm:evil");
  assert.equal(evil.supported, false);
  assert.equal(evil.reason, "path_escape");
  assert.ok(!JSON.stringify(packages).includes(outside));

  const badSettings = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-bad-"));
  fs.writeFileSync(path.join(badSettings, "settings.json"), "{not json");
  const malformed = readExtensionInventory(badSettings);
  assert.equal(malformed.packages.length, 0);
  assert.ok(malformed.error);

  const badPkg = makeAgentDir(["npm:broken"]);
  const dir = path.join(badPkg, "npm", "node_modules", "broken");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "package.json"), "{broken");
  const broken = readExtensionInventory(badPkg).packages.find((p) => p.id === "npm:broken");
  assert.equal(broken.supported, false);
});

test("resolveEnabledExtensionRoots keeps inventory order and fails closed", () => {
  const agentDir = makeAgentDir(["npm:one", "npm:two"]);
  writePackage(agentDir, "one", { manifest: ["./a.js"] });
  writePackage(agentDir, "two", { manifest: ["./b.js"] });
  const { roots, snapshot } = resolveEnabledExtensionRoots(agentDir, ["npm:two", "npm:one"]);
  // Deterministic inventory/settings order, not caller order.
  assert.ok(roots[0].endsWith(path.join("node_modules", "one")));
  assert.ok(roots[1].endsWith(path.join("node_modules", "two")));
  assert.deepEqual(snapshot.map((s) => s.id), ["npm:one", "npm:two"]);
  assert.ok(snapshot.every((s) => s.name && s.version && s.fingerprint.length === 64));
  assert.ok(!JSON.stringify(snapshot).includes(agentDir));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:missing"]));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:one", "npm:one"]));
  const empty = resolveEnabledExtensionRoots(agentDir, []);
  assert.deepEqual(empty, { roots: [], snapshot: [] });
});

// ------------------------------------------- Pi-parity resolver
test("resolver mirrors Pi manifest/index/discovery precedence exactly", () => {
  const agentDir = makeAgentDir(["npm:m", "npm:i", "npm:d", "npm:bare", "npm:mjs"]);
  // Manifest wins over a root index file.
  const mDir = writePackage(agentDir, "m", { manifest: ["./ext.js"], index: true });
  assert.deepEqual(resolvePackageExtensionResources(fs.realpathSync(mDir)),
    { kind: "manifest", resources: ["ext.js"] });
  // Root index.js fallback when no manifest applies.
  const iDir = writePackage(agentDir, "i", { index: true });
  assert.deepEqual(resolvePackageExtensionResources(fs.realpathSync(iDir)),
    { kind: "index", resources: ["index.js"] });
  // Empty or non-array pi.extensions falls through to index (Pi parity).
  const eDir = path.join(agentDir, "npm", "node_modules", "e");
  fs.mkdirSync(eDir, { recursive: true });
  fs.writeFileSync(path.join(eDir, "package.json"),
    JSON.stringify({ name: "e", version: "1.0.0", pi: { extensions: [] } }));
  fs.writeFileSync(path.join(eDir, "index.ts"), "export default function (pi) {}\n");
  // (settings entry added below via fresh agentDir to keep ordering simple)
  // Discovery: top-level .js + subdir index + subdir manifest; .mjs ignored.
  const dDir = writePackage(agentDir, "d", {});
  fs.writeFileSync(path.join(dDir, "top.js"), "export default function (pi) {}\n");
  fs.writeFileSync(path.join(dDir, "ignored.mjs"), "export default function (pi) {}\n");
  fs.writeFileSync(path.join(dDir, "notes.txt"), "not an extension\n");
  const sub = path.join(dDir, "sub");
  fs.mkdirSync(sub, { recursive: true });
  fs.writeFileSync(path.join(sub, "index.js"), "export default function (pi) {}\n");
  const sub2 = path.join(dDir, "sub2");
  fs.mkdirSync(sub2, { recursive: true });
  fs.writeFileSync(path.join(sub2, "package.json"),
    JSON.stringify({ name: "x", pi: { extensions: ["./w.js"] } }));
  fs.writeFileSync(path.join(sub2, "w.js"), "export default function (pi) {}\n");
  const found = resolvePackageExtensionResources(fs.realpathSync(dDir));
  assert.equal(found.kind, "discovery");
  assert.deepEqual([...found.resources].sort(), ["sub/index.js", "sub2/w.js", "top.js"]);
  // Bare extensions/ directory with no manifest/index loads nothing in Pi.
  const bDir = writePackage(agentDir, "bare", { conventional: true });
  assert.deepEqual(resolvePackageExtensionResources(fs.realpathSync(bDir)),
    { kind: "none", resources: [] });
  const inv = readExtensionInventory(agentDir);
  assert.equal(inv.packages.find((p) => p.id === "npm:bare").reason, "no_extension_resources");
});

test("resolver rejects manifest traversal/absolute/missing/non-file escapes", () => {
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-res-out-"));
  fs.writeFileSync(path.join(outside, "evil.js"), "x\n");
  for (const [name, manifest, expected] of [
    ["esc-dotdot", ["../../outside.js"], "resource_escape"],
    ["esc-abs", ["/tmp/evil.js"], "resource_escape"],
    ["esc-abs2", [outside + "/evil.js"], "resource_escape"],
    ["gone", ["./missing.js"], "resource_missing"],
    ["isdir", ["./subdir"], "resource_invalid"],
  ]) {
    const agentDir = makeAgentDir([`npm:${name}`]);
    const dir = writePackage(agentDir, name, { manifest });
    if (name === "gone") fs.unlinkSync(path.join(dir, "missing.js"));
    if (name === "isdir") {
      fs.unlinkSync(path.join(dir, "subdir"));
      fs.mkdirSync(path.join(dir, "subdir"), { recursive: true });
    }
    const resolved = resolvePackageExtensionResources(fs.realpathSync(dir));
    assert.equal(resolved.error, expected, name);
    const row = readExtensionInventory(agentDir).packages.find((p) => p.id === `npm:${name}`);
    assert.equal(row.supported, false);
    assert.equal(row.reason, expected);
  }
  // Symlinked manifest file escaping the root is rejected; internal
  // symlinks (Pi loads them) are allowed.
  const agentDir = makeAgentDir(["npm:linkout", "npm:linkin"]);
  const outDir = writePackage(agentDir, "linkout", { manifest: ["./e.js"] });
  fs.unlinkSync(path.join(outDir, "e.js"));
  fs.symlinkSync(path.join(outside, "evil.js"), path.join(outDir, "e.js"));
  assert.equal(resolvePackageExtensionResources(fs.realpathSync(outDir)).error, "resource_escape");
  const inDir = writePackage(agentDir, "linkin", { manifest: ["./e.js"] });
  fs.unlinkSync(path.join(inDir, "e.js"));
  fs.writeFileSync(path.join(inDir, "real.js"), "export default function (pi) {}\n");
  fs.symlinkSync(path.join(inDir, "real.js"), path.join(inDir, "e.js"));
  const inward = resolvePackageExtensionResources(fs.realpathSync(inDir));
  assert.equal(inward.error, undefined);
  assert.deepEqual(inward.resources, ["e.js"]);
  // Symlinked discovery subdirectory escaping the root is rejected.
  const agentDir2 = makeAgentDir(["npm:dirlink"]);
  const dlDir = writePackage(agentDir2, "dirlink", {});
  fs.symlinkSync(outside, path.join(dlDir, "sub"));
  assert.equal(resolvePackageExtensionResources(fs.realpathSync(dlDir)).error, "resource_escape");
});

// ------------------------------------------------- identity
test("package identity mismatches and npm aliases fail explicitly", () => {
  assert.deepEqual(parseNpmSource("npm:alias@npm:real@1.0.0"),
    { name: "alias", version: "npm:real@1.0.0", isAlias: true });
  assert.deepEqual(parseNpmSource("npm:@s/pkg@^2.0.0"),
    { name: "@s/pkg", version: "^2.0.0", isAlias: false });
  const agentDir = makeAgentDir(["npm:alias@npm:real@1.0.0", "npm:renamed"]);
  // Alias install layout: directory `alias` holding package `real`.
  const aDir = path.join(agentDir, "npm", "node_modules", "alias");
  fs.mkdirSync(aDir, { recursive: true });
  fs.writeFileSync(path.join(aDir, "package.json"),
    JSON.stringify({ name: "real", version: "1.0.0", pi: { extensions: ["./e.js"] } }));
  fs.writeFileSync(path.join(aDir, "e.js"), "export default function (pi) {}\n");
  // Renamed directory holding an unrelated package.
  const rDir = path.join(agentDir, "npm", "node_modules", "renamed");
  fs.mkdirSync(rDir, { recursive: true });
  fs.writeFileSync(path.join(rDir, "package.json"),
    JSON.stringify({ name: "other", version: "1.0.0", pi: { extensions: ["./e.js"] } }));
  fs.writeFileSync(path.join(rDir, "e.js"), "export default function (pi) {}\n");
  const { packages } = readExtensionInventory(agentDir);
  assert.equal(packages.find((p) => p.id === "npm:alias").reason, "npm_alias_unsupported");
  assert.equal(packages.find((p) => p.id === "npm:renamed").reason, "identity_mismatch");
  assert.ok(packages.every((p) => p.supported === false));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:alias"]));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:renamed"]));
});

// ------------------------------------------------- canonical order
test("canonical order makes reversed input share one stored revision", () => {
  const agentDir = makeAgentDir(["npm:one", "npm:two"]);
  writePackage(agentDir, "one", { manifest: ["./a.js"] });
  writePackage(agentDir, "two", { manifest: ["./b.js"] });
  const { packages } = readExtensionInventory(agentDir);
  assert.deepEqual(canonicalizeEnabledOrder(packages, ["npm:two", "npm:one"]),
    ["npm:one", "npm:two"]);
  assert.deepEqual(canonicalizeEnabledOrder(packages, ["npm:one", "npm:two"]),
    ["npm:one", "npm:two"]);
  const revA = extensionRevision({ version: 1, enabled: canonicalizeEnabledOrder(packages, ["npm:two", "npm:one"]) });
  const revB = extensionRevision({ version: 1, enabled: canonicalizeEnabledOrder(packages, ["npm:one", "npm:two"]) });
  assert.equal(revA, revB);
});

// --------------------------------- fail-closed bounds (deployment blocker)
test("manifest with exactly 32 safe entries is accepted; 33 is rejected", () => {
  const ok32 = Array.from({ length: 32 }, (_, i) => `./e${i}.js`);
  const agentDir = makeAgentDir(["npm:exact32"]);
  const dir = writePackageJsonOnly(agentDir, "exact32", ok32);
  for (const entry of ok32) materializeFile(dir, entry);
  const accepted = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:exact32");
  assert.equal(accepted.supported, true);
  assert.equal(accepted.extension_count, 32);

  const over33 = [...ok32, "./e32.js"];
  const agentDir2 = makeAgentDir(["npm:over33"]);
  const dir2 = writePackageJsonOnly(agentDir2, "over33", over33);
  for (const entry of over33) materializeFile(dir2, entry);
  const rejected = readExtensionInventory(agentDir2).packages
    .find((p) => p.id === "npm:over33");
  assert.equal(rejected.supported, false);
  assert.equal(rejected.reason, "manifest_too_large");
  assert.equal(rejected.extension_count, 0);
  assert.deepEqual(rejected.extensions, []);
});

test("32 safe entries plus an escaping 33rd entry is rejected", () => {
  const manifest = [...Array.from({ length: 32 }, (_, i) => `./e${i}.js`), "../outside.js"];
  const agentDir = makeAgentDir(["npm:trap33"]);
  const dir = writePackageJsonOnly(agentDir, "trap33", manifest);
  for (const entry of manifest.slice(0, 32)) materializeFile(dir, entry);
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:trap33");
  assert.equal(row.supported, false);
  // Count limit trips first on 33 declarations; the escape is rejected
  // either way (proved within-limit below). Never a first-32 success.
  assert.ok(["manifest_too_large", "resource_escape"].includes(row.reason));
});

test("escaping entry within the count limit is rejected as escape", () => {
  const manifest = [...Array.from({ length: 31 }, (_, i) => `./e${i}.js`), "../outside.js"];
  const agentDir = makeAgentDir(["npm:trap31"]);
  const dir = writePackageJsonOnly(agentDir, "trap31", manifest);
  for (const entry of manifest.slice(0, 31)) materializeFile(dir, entry);
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:trap31");
  assert.equal(row.supported, false);
  assert.equal(row.reason, "resource_escape");
});

test("manifest entry length exactly at max is valid; beyond max is rejected", () => {
  // Exactly 400 chars: 190 + 1 + 190 + 1 + 15 + 3 (declared verbatim).
  const exact = `${"a".repeat(190)}/${"b".repeat(190)}/${"c".repeat(15)}.js`;
  assert.equal(exact.length, 400);
  const agentDir = makeAgentDir(["npm:exactlen"]);
  const dir = writePackageJsonOnly(agentDir, "exactlen", [exact]);
  materializeFile(dir, exact);
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:exactlen");
  assert.equal(row.supported, true);
  assert.deepEqual(row.extensions, [exact]);

  const over = `${"a".repeat(397)}.js`;
  assert.equal(over.length, 400);
  const agentDir2 = makeAgentDir(["npm:overlen"]);
  writePackageJsonOnly(agentDir2, "overlen", [`./${over}`]);
  const row2 = readExtensionInventory(agentDir2).packages
    .find((p) => p.id === "npm:overlen");
  assert.equal(row2.supported, false);
  assert.equal(row2.reason, "resource_path_too_long");
});

test("long entry is judged by its full string, never a truncated prefix", () => {
  // A 400-char nested safe file exists, and the declared string starts
  // with exactly that path but continues into an escape. Prefix
  // validation would approve the safe file while Pi resolves the full
  // string elsewhere; full-string validation must reject.
  const safe400 = `${"a".repeat(190)}/${"b".repeat(190)}/${"c".repeat(15)}.js`;
  assert.equal(safe400.length, 400);
  const trap = `${safe400}/../outside.js`;
  assert.ok(trap.length > 400);
  assert.equal(trap.slice(0, 400), safe400);
  const agentDir = makeAgentDir(["npm:prefixtrap"]);
  const dir = writePackageJsonOnly(agentDir, "prefixtrap", [trap]);
  materializeFile(dir, safe400);
  const resolved = resolvePackageExtensionResources(fs.realpathSync(dir));
  assert.equal(resolved.error, "resource_path_too_long");
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:prefixtrap");
  assert.equal(row.supported, false);
  assert.equal(row.reason, "resource_path_too_long");
});

test("discovery with exactly 32 resources is accepted; 33 is rejected", () => {
  const agentDir = makeAgentDir(["npm:d32"]);
  const dir = writePackageJsonOnly(agentDir, "d32", []);
  // Empty manifest array falls through to discovery (Pi parity).
  fs.writeFileSync(path.join(dir, "package.json"),
    JSON.stringify({ name: "d32", version: "1.0.0", pi: { extensions: [] } }));
  for (let i = 0; i < 32; i++) materializeFile(dir, `f${String(i).padStart(2, "0")}.js`);
  const accepted = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:d32");
  assert.equal(accepted.supported, true);
  assert.equal(accepted.extension_count, 32);

  const agentDir2 = makeAgentDir(["npm:d33"]);
  const dir2 = path.join(agentDir2, "npm", "node_modules", "d33");
  fs.mkdirSync(dir2, { recursive: true });
  fs.writeFileSync(path.join(dir2, "package.json"),
    JSON.stringify({ name: "d33", version: "1.0.0" }));
  for (let i = 0; i < 33; i++) materializeFile(dir2, `f${String(i).padStart(2, "0")}.js`);
  const rejected = readExtensionInventory(agentDir2).packages
    .find((p) => p.id === "npm:d33");
  assert.equal(rejected.supported, false);
  assert.equal(rejected.reason, "discovery_too_large");
  assert.equal(rejected.extension_count, 0);
});

test("discovery escape after 32 harmless resources is still rejected", () => {
  // 32 harmless files created first, then an escaping symlink. Resolver
  // order follows the OS readdir, so names cannot force the escape past
  // a (removed) early break; full traversal validates every candidate
  // regardless of order, making rejection deterministic.
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-disc-out-"));
  fs.writeFileSync(path.join(outside, "evil.js"), "x\n");
  const agentDir = makeAgentDir(["npm:dtrap"]);
  const dir = path.join(agentDir, "npm", "node_modules", "dtrap");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "package.json"),
    JSON.stringify({ name: "dtrap", version: "1.0.0" }));
  for (let i = 0; i < 32; i++) materializeFile(dir, `r${String(i).padStart(2, "0")}.js`);
  fs.symlinkSync(path.join(outside, "evil.js"), path.join(dir, "r32_evil_link.js"));
  const resolved = resolvePackageExtensionResources(fs.realpathSync(dir));
  assert.equal(resolved.error, "resource_escape");
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:dtrap");
  assert.equal(row.supported, false);
  assert.equal(row.reason, "resource_escape");
});

test("spawn validation fails when a package exceeds limits after inventory", () => {
  const ok16 = Array.from({ length: 16 }, (_, i) => `./e${i}.js`);
  const agentDir = makeAgentDir(["npm:mutant"]);
  const dir = writePackageJsonOnly(agentDir, "mutant", ok16);
  for (const entry of ok16) materializeFile(dir, entry);
  const before = resolveEnabledExtensionRoots(agentDir, ["npm:mutant"]);
  assert.equal(before.snapshot.length, 1);
  // Mutate after inventory: grow past the acceptance maximum. Spawn-time
  // revalidation must fail instead of trimming to a safe subset, because
  // Pi receives the whole package root.
  const grown = [...ok16, ...Array.from({ length: 17 }, (_, i) => `./x${i}.js`)];
  assert.equal(grown.length, 33);
  fs.writeFileSync(path.join(dir, "package.json"),
    JSON.stringify({ name: "mutant", version: "1.0.0", pi: { extensions: grown } }));
  for (const entry of grown.slice(16)) materializeFile(dir, entry);
  assert.equal(resolvePackageExtensionResources(fs.realpathSync(dir)).error, "manifest_too_large");
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:mutant"]));
});

// --------------------------------- directory resources (pi-web-access shape)
test("sanitized pi-web-access structural shape is supported", () => {
  // Structural shape of installed pi-web-access@0.30.0 only (manifest +
  // resource layout; no package source code is copied): manifest entry
  // "./dist" resolving to a directory holding only index.js.
  const agentDir = makeAgentDir(["npm:webshape"]);
  const dir = writePackageJsonOnly(agentDir, "webshape", ["./dist"]);
  fs.writeFileSync(path.join(dir, "package.json"),
    JSON.stringify({ name: "webshape", version: "0.30.0", pi: { extensions: ["./dist"] } }));
  const dist = path.join(dir, "dist");
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(path.join(dist, "index.js"), "export default function (pi) {}\n");
  const resolved = resolvePackageExtensionResources(fs.realpathSync(dir));
  assert.deepEqual(resolved, { kind: "manifest", resources: ["dist/index.js"] });
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:webshape");
  assert.equal(row.supported, true);
  assert.equal(row.has_extensions, true);
  assert.equal(row.extension_count, 1);
  assert.deepEqual(row.extensions, ["dist/index.js"]);
  const roots = resolveEnabledExtensionRoots(agentDir, ["npm:webshape"]);
  assert.equal(roots.snapshot.length, 1);
});

test("directory index.ts wins; same-dir mains load; others fail closed", () => {
  function dirCase(name, setup) {
    const agentDir = makeAgentDir([`npm:${name}`]);
    const dir = writePackageJsonOnly(agentDir, name, ["./sub"]);
    const sub = path.join(dir, "sub");
    fs.mkdirSync(sub, { recursive: true });
    setup(dir, sub);
    return { agentDir, dir, sub, name };
  }
  // index.ts beats index.js (verified against Pi 0.86.1).
  {
    const { agentDir, sub } = dirCase("dirts", () => {});
    void agentDir;
    fs.writeFileSync(path.join(sub, "index.ts"), "export default function (pi) {}\n");
    fs.writeFileSync(path.join(sub, "index.js"), "export default function (pi) {}\n");
    assert.deepEqual(resolvePackageExtensionResources(fs.realpathSync(
      path.join(sub, ".."))), { kind: "manifest", resources: ["sub/index.ts"] });
  }
  // Same-dir mains: bare, ./-prefixed, extension-guessed.
  for (const [caseName, main, file] of [
    ["mainbare", "main.js", "main.js"],
    ["maindot", "./main.js", "main.js"],
    ["mainguess", "main", "main.js"],
  ]) {
    const { dir } = dirCase(caseName, () => {});
    fs.writeFileSync(path.join(dir, "sub", "package.json"), JSON.stringify({ main }));
    fs.writeFileSync(path.join(dir, "sub", file), "export default function (pi) {}\n");
    const resolved = resolvePackageExtensionResources(fs.realpathSync(dir));
    assert.deepEqual(resolved, { kind: "manifest", resources: [`sub/${file}`] }, caseName);
  }
  // Subpath mains, index.mjs-only, and main-less/index-less dirs load
  // nothing in Pi and fail closed here.
  for (const [caseName, setup] of [
    ["mainsub", (dir, sub) => {
      fs.writeFileSync(path.join(sub, "package.json"), JSON.stringify({ main: "lib/main.js" }));
      materializeFile(dir, "sub/lib/main.js");
    }],
    ["mainsubdot", (dir, sub) => {
      fs.writeFileSync(path.join(sub, "package.json"), JSON.stringify({ main: "./lib/main.js" }));
      materializeFile(dir, "sub/lib/main.js");
    }],
    ["mjsonly", (dir, sub) => {
      void dir;
      fs.writeFileSync(path.join(sub, "index.mjs"), "export default function (pi) {}\n");
    }],
    ["dirbare", () => {}],
  ]) {
    const { agentDir, name } = dirCase(caseName, setup);
    const row = readExtensionInventory(agentDir).packages.find((p) => p.id === `npm:${name}`);
    assert.equal(row.supported, false, caseName);
    assert.equal(row.reason, "resource_invalid", caseName);
  }
});

test("nested manifests are not consulted for directory entries", () => {
  // Pi passes a manifest directory entry straight to the module loader;
  // a nested pi.extensions manifest inside it is never read. Bridge must
  // not invent deep loading either.
  const agentDir = makeAgentDir(["npm:nest"]);
  const dir = writePackageJsonOnly(agentDir, "nest", ["./sub"]);
  const sub = path.join(dir, "sub");
  fs.mkdirSync(sub, { recursive: true });
  fs.writeFileSync(path.join(sub, "package.json"),
    JSON.stringify({ name: "inner", pi: { extensions: ["./deep.js"] } }));
  fs.writeFileSync(path.join(sub, "deep.js"), "export default function (pi) {}\n");
  const row = readExtensionInventory(agentDir).packages.find((p) => p.id === "npm:nest");
  assert.equal(row.supported, false);
  assert.equal(row.reason, "resource_invalid");
});

test("directory symlink escape rejected; internal dir symlink accepted", () => {
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-dirout-"));
  fs.writeFileSync(path.join(outside, "index.js"), "export default function (pi) {}\n");
  const agentDir = makeAgentDir(["npm:dirout", "npm:dirin"]);
  const outDir = writePackageJsonOnly(agentDir, "dirout", ["./link"]);
  fs.symlinkSync(outside, path.join(outDir, "link"));
  assert.equal(resolvePackageExtensionResources(fs.realpathSync(outDir)).error, "resource_escape");
  const inDir = writePackageJsonOnly(agentDir, "dirin", ["./link"]);
  const real = path.join(inDir, "real");
  fs.mkdirSync(real, { recursive: true });
  fs.writeFileSync(path.join(real, "index.js"), "export default function (pi) {}\n");
  fs.symlinkSync(real, path.join(inDir, "link"));
  const inward = resolvePackageExtensionResources(fs.realpathSync(inDir));
  assert.equal(inward.error, undefined);
  assert.deepEqual(inward.resources, ["link/index.js"]);
});

test("directory declarations count once each toward the fail-closed limit", () => {
  const manifest = Array.from({ length: 33 }, (_, i) => `./d${i}`);
  const agentDir = makeAgentDir(["npm:manydirs"]);
  const dir = writePackageJsonOnly(agentDir, "manydirs", manifest);
  for (const entry of manifest) {
    const sub = path.join(dir, entry);
    fs.mkdirSync(sub, { recursive: true });
    fs.writeFileSync(path.join(sub, "index.js"), "export default function (pi) {}\n");
  }
  const row = readExtensionInventory(agentDir).packages
    .find((p) => p.id === "npm:manydirs");
  assert.equal(row.supported, false);
  assert.equal(row.reason, "manifest_too_large");
});

test("removing a directory index after inventory fails spawn validation", () => {
  const agentDir = makeAgentDir(["npm:dirswap"]);
  const dir = writePackageJsonOnly(agentDir, "dirswap", ["./dist"]);
  const dist = path.join(dir, "dist");
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(path.join(dist, "index.js"), "export default function (pi) {}\n");
  assert.equal(resolveEnabledExtensionRoots(agentDir, ["npm:dirswap"]).snapshot.length, 1);
  // package.json untouched (fingerprint identical): only the resource vanished.
  fs.unlinkSync(path.join(dist, "index.js"));
  assert.equal(resolvePackageExtensionResources(fs.realpathSync(dir)).error, "resource_invalid");
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:dirswap"]));
});

// ------------------------------------------------- redaction
test("extension audit redaction is best-effort but ordinary text survives", () => {
  assert.equal(redactExtensionText("https://user:s3cret@example.com/x?a=1"),
    "https://[redacted]@example.com/x?a=1");
  assert.equal(redactExtensionText("https://h/p?token=abc123&next=x"),
    "https://h/p?token=[redacted]&next=x");
  assert.equal(redactExtensionText("call with api_key=XYZ and mode=rw"),
    "call with api_key=[redacted] and mode=rw");
  assert.equal(redactExtensionText("Authorization: Bearer abcDEF123"),
    "Authorization: Bearer [redacted]");
  assert.equal(redactExtensionText("pong"), "pong");
  assert.equal(redactExtensionText("review the code in src/"), "review the code in src/");
  assert.equal(redactExtensionText("https://example.com/?q=hello&page=2"),
    "https://example.com/?q=hello&page=2");
  const { summary } = summarizeExtensionInput({
    url: "https://user:pw@example.com/hook?token=T",
    query: "password=hunter2", count: 1,
  });
  assert.ok(!JSON.stringify(summary).includes("pw"));
  assert.ok(!JSON.stringify(summary).includes("hunter2"));
  assert.ok(summary.url.includes("[redacted]"));
  assert.ok(summary.args_bytes > 0 && summary.args_sha256.length === 64);
  const result = summarizeExtensionResult(
    { content: [{ type: "text", text: "ok\napi_key = SECRET-XYZ" }] }, false);
  assert.ok(result.preview.includes("[redacted]"));
  assert.ok(!result.preview.includes("SECRET-XYZ"));
});

test("resolveEnabledExtensionRoots keeps inventory order and fails closed", () => {
  const agentDir = makeAgentDir(["npm:one", "npm:two"]);
  writePackage(agentDir, "one", { manifest: ["./a.js"] });
  writePackage(agentDir, "two", { manifest: ["./b.js"] });
  const { roots, snapshot } = resolveEnabledExtensionRoots(agentDir, ["npm:two", "npm:one"]);
  // Deterministic inventory/settings order, not caller order.
  assert.ok(roots[0].endsWith(path.join("node_modules", "one")));
  assert.ok(roots[1].endsWith(path.join("node_modules", "two")));
  assert.deepEqual(snapshot.map((s) => s.id), ["npm:one", "npm:two"]);
  assert.ok(snapshot.every((s) => s.name && s.version && s.fingerprint.length === 64));
  assert.ok(!JSON.stringify(snapshot).includes(agentDir));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:missing"]));
  assert.throws(() => resolveEnabledExtensionRoots(agentDir, ["npm:one", "npm:one"]));
  const empty = resolveEnabledExtensionRoots(agentDir, []);
  assert.deepEqual(empty, { roots: [], snapshot: [] });
});

// ------------------------------------------------------------ loadout
test("managed loadout uses excludeTools and no allowlist once extensions are enabled", () => {
  const excludes = sdkExcludeTools({ writable: false, shellMode: "deny" });
  assert.ok(excludes.includes("edit") && excludes.includes("bash") && excludes.includes("powershell"));
  const writable = sdkExcludeTools({ writable: true, shellMode: "allow" });
  assert.ok(!writable.split(",").includes("edit"));
  assert.ok(!writable.split(",").includes("bash"));
  assert.ok(writable.split(",").includes("powershell"));
});

test("SDK loadout passes explicit extension roots with no tools allowlist", async () => {
  const { sdkToolLoadout } = await import("../config.mjs");
  const loadout = sdkToolLoadout({
    writable: false, shellMode: "deny", extensionRoots: ["/pkg/alpha"],
  });
  assert.equal(loadout.tools, null);
  assert.ok(loadout.excludeTools.includes("edit"));
  assert.deepEqual(loadout.extensionPaths, ["/pkg/alpha"]);
  const plain = sdkToolLoadout({ writable: false });
  assert.deepEqual(plain.tools, ["read", "grep", "find", "ls"]);
  assert.equal(plain.excludeTools, null);
  assert.deepEqual(plain.extensionPaths, []);
})

// ------------------------------------------------------------ adapter session
function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-adapter-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  return { tmp, root: canonicalizeProjectsDir(path.join(tmp, "Projects")), app: fs.realpathSync(app) };
}

async function managedCreate(agentDir, extensionPolicy) {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, agentDir,
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true, piVersion: "0.87.0",
  });
  const { validatePolicy } = await import("../policy.mjs");
  const { policyRevision, canonicalJson } = await import("../policy.mjs");
  const permission = {
    version: 3, write_tools_enabled: false,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [], protected_template_exceptions: [],
    allow_session_always: true, external_access: { default_mode: "deny", roots: [] },
    shell_mode: "deny",
  };
  validatePolicy(permission);
  const session = await adapter.createSession(projects.app, "t", {
    permission_policy: permission, policy_revision: policyRevision(permission),
    extension_policy: extensionPolicy, extension_revision: extensionRevision(extensionPolicy),
  });
  const created = transport.lastCreated();
  await adapter.shutdown();
  return { session, created };
}

test("managed session with enabled extension uses excludeTools and snapshots", async () => {
  const agentDir = makeAgentDir(["npm:alpha"]);
  writePackage(agentDir, "alpha", { manifest: ["./ext.js"] });
  const { session, created } = await managedCreate(agentDir, { version: 1, enabled: ["npm:alpha"] });
  assert.equal(created.tools, null);
  assert.ok(created.excludeTools.includes("edit") && created.excludeTools.includes("bash"));
  // Exactly the resolved package root loads explicitly; the trusted
  // permission extension loads as an inline factory, never a path.
  assert.equal(created.extensionPaths.length, 1);
  assert.ok(created.extensionPaths[0].includes("alpha"));
  assert.ok(created.uiContext);
  assert.equal(session.extension_revision.length, 64);
  assert.equal(session.extensions.length, 1);
  assert.equal(session.extensions[0].id, "npm:alpha");
  assert.ok(!JSON.stringify(session).includes(agentDir));
});

test("managed session with empty extension policy keeps the 3C1 tools allowlist", async () => {
  const agentDir = makeAgentDir([]);
  const { created } = await managedCreate(agentDir, { version: 1, enabled: [] });
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls"]);
  assert.equal(created.excludeTools, null);
  assert.deepEqual(created.extensionPaths, []);
});

test("managed session fails clearly when an enabled package disappeared", async () => {
  const agentDir = makeAgentDir(["npm:alpha"]);
  await assert.rejects(
    managedCreate(agentDir, { version: 1, enabled: ["npm:alpha"] }),
    /unavailable|not installed/i);
});

// ------------------------------------------------------------ preflight
test("extension tools pass preflight without permission ask when enabled", async () => {
  const agentDir = makeAgentDir(["npm:alpha"]);
  writePackage(agentDir, "alpha", { manifest: ["./ext.js"] });
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, agentDir,
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true, piVersion: "0.87.0",
  });
  const { validatePolicy, policyRevision } = await import("../policy.mjs");
  const permission = {
    version: 3, write_tools_enabled: false,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [], protected_template_exceptions: [],
    allow_session_always: true, external_access: { default_mode: "deny", roots: [] },
    shell_mode: "deny",
  };
  const session = await adapter.createSession(projects.app, "t", {
    permission_policy: permission, policy_revision: policyRevision(permission),
    extension_policy: { version: 1, enabled: ["npm:alpha"] },
    extension_revision: extensionRevision({ version: 1, enabled: ["npm:alpha"] }),
  });
  const entry = adapter._entry(session.id);
  const verdict = adapter._evaluate(entry, "alpha-tool", { query: "x" });
  assert.equal(verdict.effect, "allow");
  assert.equal(verdict.code, "extension_tool");
  // Managed built-ins keep existing semantics.
  const read = adapter._evaluate(entry, "read", { path: "notes.txt" });
  assert.equal(read.effect, "allow");
  await adapter.shutdown();
});

test("unknown tools still deny when no extension is enabled", async () => {
  const agentDir = makeAgentDir([]);
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, agentDir,
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true, piVersion: "0.87.0",
  });
  const { policyRevision } = await import("../policy.mjs");
  const { safeDefaultPolicy } = await import("../policy.mjs");
  const permission = safeDefaultPolicy();
  const session = await adapter.createSession(projects.app, "t", {
    permission_policy: permission, policy_revision: policyRevision(permission),
    extension_policy: { version: 1, enabled: [] },
    extension_revision: extensionRevision({ version: 1, enabled: [] }),
  });
  const verdict = adapter._evaluate(adapter._entry(session.id), "mystery-tool", {});
  assert.equal(verdict.effect, "deny");
  await adapter.shutdown();
});

// ------------------------------------------------------------ trusted extension
test("trusted permission hook passes extension tools, blocks malformed calls", async () => {
  const { safeDefaultPolicy, canonicalJson } = await import("../policy.mjs");
  process.env.WB_PI_POLICY_JSON = canonicalJson(safeDefaultPolicy());
  try {
    const mod = await import("../trusted-permission-extension.mjs");
    assert.ok(!MANAGED_TOOLS.includes("alpha-tool"));
    let handler = null;
    const fakePi = { on: (event, fn) => { if (event === "tool_call") handler = fn; } };
    mod.default(fakePi);
    assert.ok(handler);
    // Extension tool with credible toolCallId passes through.
    const pass = await handler(
      { toolName: "alpha-tool", toolCallId: "call-1", input: { query: "x" } }, {});
    assert.equal(pass, undefined);
    // Missing toolCallId fails closed even for extension tools.
    const blocked = await handler({ toolName: "alpha-tool", input: {} }, {});
    assert.equal(blocked.block, true);
    // Managed read still allowed by policy.
    const read = await handler(
      { toolName: "read", toolCallId: "call-2", input: { path: "notes.txt" } }, {});
    assert.equal(read, undefined);
  } finally {
    delete process.env.WB_PI_POLICY_JSON;
  }
});

test("parameterized trusted factory closes over per-session policy and cwd", async () => {
  const { safeDefaultPolicy } = await import("../policy.mjs");
  const { createTrustedPermissionExtension } = await import("../trusted-permission-extension.mjs");
  const factory = createTrustedPermissionExtension({
    policy: safeDefaultPolicy(), cwd: "/sessions/app",
  });
  let handler = null;
  factory({ on: (event, fn) => { if (event === "tool_call") handler = fn; } });
  assert.ok(handler);
  // Ask-path needs a UI context; deny/allow resolve without one.
  const read = await handler(
    { toolName: "read", toolCallId: "call-1", input: { path: "notes.txt" } }, {});
  assert.equal(read, undefined);
  // A null policy fails every call closed without a UI context.
  const broken = createTrustedPermissionExtension({ policy: null, cwd: "/sessions/app" });
  let brokenHandler = null;
  broken({ on: (event, fn) => { if (event === "tool_call") brokenHandler = fn; } });
  const denied = await brokenHandler(
    { toolName: "read", toolCallId: "call-1", input: { path: "notes.txt" } }, {});
  assert.equal(denied.block, true);
});

// ------------------------------------------------------------ generic audit
test("generic extension audit bounds input/result without secrets", () => {
  const { ok, summary } = summarizeExtensionInput({
    query: "hello", url: "https://example.com", count: 3,
    nested: { deep: "x" }, token: "SECRET-TOKEN",
  });
  assert.equal(ok, true);
  assert.equal(summary.args_sha256.length, 64);
  assert.ok(summary.args_bytes > 0);
  assert.ok(summary.top_keys.includes("query"));
  assert.equal(summary.query, "hello");
  assert.equal(summary.url, "https://example.com");
  const serialized = JSON.stringify(summary);
  assert.ok(!serialized.includes("SECRET-TOKEN"));
  assert.ok(!serialized.includes("deep"));
  const result = summarizeExtensionResult(
    { content: [{ type: "text", text: "answer body" }],
      details: { fullOutputPath: "/tmp/secret", truncation: { truncated: false } } }, false);
  assert.equal(result.preview, "answer body");
  assert.ok(!JSON.stringify(result).includes("/tmp/secret"));
  const big = summarizeExtensionResult("z".repeat(20000), false);
  assert.equal(big.preview.length, 8192);
  assert.equal(big.truncated, true);
});

// ------------------------------------------------------------ server
test("third-party extension inventory is not exposed through the runtime API", async () => {
  const agentDir = makeAgentDir(["npm:alpha"]);
  writePackage(agentDir, "alpha", { manifest: ["./ext.js"] });
  const fake = {
    projectsRoot: "/tmp", sessionCount: 0,
    listExtensions: () => readExtensionInventory(agentDir),
  };
  const server = createPiAdapterServer({
    adapter: fake, token: "tok", adapterVersion: "0.3.0", instance: "i",
    piUsable: true, piVersion: "0.87.0",
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const res = await fetch(`${base}/extensions`, { headers: { "X-Runtime-Token": "tok" } });
    assert.equal(res.status, 404);
    const health = await (await fetch(`${base}/health`)).json();
    assert.equal(health.capabilities, undefined);
    assert.ok(!JSON.stringify(health).includes("alpha"));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

// ------------------------------------------------------------ SDK fixture
test("installed Pi SDK loads a fixture package root and exposes its registrations", async () => {
  // Provider-free: creates a real in-process AgentSession against
  // throwaway dirs, never sends an LLM prompt, consumes no quota.
  // The factory proves native loading with a marker side effect first,
  // then registers a tool. Unlike the old RPC surface (which exposed no
  // tool enumeration route), the SDK enumerates tools directly via
  // getAllTools(), so tool availability itself is the proof; real tool
  // execution stays a post-deploy smoke requirement.
  const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-live-agent-"));
  const pkgRoot = path.join(agentDir, "npm", "node_modules", "fixture-ext");
  fs.mkdirSync(pkgRoot, { recursive: true });
  fs.writeFileSync(path.join(pkgRoot, "package.json"), JSON.stringify({
    name: "fixture-ext", version: "0.0.1", pi: { extensions: ["./ext.js"] },
  }));
  // The factory proves native loading with a marker side effect first,
  // then registers a tool and a command (both guarded so a future
  // registration-API change cannot mask the load proof). Tools have no
  // provider-free enumeration route in older Pi RPC (only commands,
  // skills, and prompts are enumerable via get_commands), so command
  // visibility is the strongest provider-free availability proof; real
  // tool execution stays a post-deploy smoke requirement.
  const marker = path.join(agentDir, "ext-loaded.marker");
  fs.writeFileSync(path.join(pkgRoot, "ext.js"),
    `import fs from "node:fs";\n` +
    `const marker = ${JSON.stringify(marker)};\n` +
    `export default function (pi) {\n` +
    `  try { fs.writeFileSync(marker, "loaded\\n"); } catch {}\n` +
    `  try {\n` +
    `    pi.registerTool({ name: "fixture_ping", description: "ping",\n` +
    `      parameters: { type: "object", properties: {} },\n` +
    `      async execute() { return { content: [{ type: "text", text: "pong" }] }; } });\n` +
    `  } catch {}\n` +
    `  try {\n` +
    `    pi.registerCommand("fixture_ping_cmd", { description: "ping command",\n` +
    `      handler: async () => {} });\n` +
    `  } catch {}\n` +
    `}\n`);
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "pi-live-cwd-"));
  const scratchAgentDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-live-sdk-agent-"));
  // Managed-session boundary: auto-discovery off, explicit package
  // root, built-ins hidden via denylist (no tools allowlist).
  const { createSdkSession } = await import("../sdk-transport.mjs");
  const { safeDefaultPolicy } = await import("../policy.mjs");
  const { session, resourceLoader } = await createSdkSession({
    cwd,
    agentDir: scratchAgentDir,
    tools: null,
    excludeTools: "edit,write,bash,powershell",
    extensionPaths: [pkgRoot],
    policy: safeDefaultPolicy(),
    uiContext: {
      select: async () => undefined,
      confirm: async () => false,
      input: async () => undefined,
      notify: () => {},
      onTerminalInput: () => () => {},
      setStatus: () => {},
      setWorkingMessage: () => {},
      setWorkingVisible: () => {},
      setWorkingIndicator: () => {},
      setHiddenThinkingLabel: () => {},
      setWidget: () => {},
      setFooter: () => {},
    },
    onEvent: null,
  });
  try {
    assert.ok(session && typeof session.sessionId === "string" && session.sessionId);
    // The fixture package root resolved through Pi's own manifest
    // semantics (pi.extensions) and its factory executed natively: the
    // marker side effect proves the extension loaded without provider
    // quota. Auto-discovery stayed off: no skills or context files.
    const deadline = Date.now() + 15000;
    while (!fs.existsSync(marker) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    assert.ok(fs.existsSync(marker), "fixture extension factory did not execute");
    assert.deepEqual(resourceLoader.getSkills().skills, []);
    assert.deepEqual(resourceLoader.getAgentsFiles().agentsFiles, []);
    // Provider-free availability proof: the extension's registered tool
    // is enumerable on the live session.
    const names = session.getAllTools().map((t) => t && t.name).filter(Boolean);
    assert.ok(names.includes("fixture_ping"), "fixture extension tool is not registered");
  } finally {
    try {
      session.dispose();
    } catch { /* best effort */ }
  }
});

// ------------------------------------------- real isolated-profile check
test("real isolated-profile pi-web-access inventory + load (gated)", async (t) => {
  // Provider-free validation against the REAL isolated Bridge profile.
  // Read-only: never modifies the installed package or its settings
  // (loading runs in-process against a throwaway agentDir plus the
  // explicit package root). Run with WB_REAL_PROFILE_CHECK=1; skipped
  // otherwise.
  if (process.env.WB_REAL_PROFILE_CHECK !== "1") {
    t.skip("set WB_REAL_PROFILE_CHECK=1 to check the real isolated profile");
  }
  const agentDir = path.join(os.homedir(), ".pi", "workspace-bridge");
  if (!fs.existsSync(path.join(agentDir, "settings.json"))) {
    t.skip("isolated Bridge Pi profile is not present on this machine");
  }
  const { packages } = readExtensionInventory(agentDir);
  const row = packages.find((p) => p.id === "npm:pi-web-access");
  assert.ok(row, "npm:pi-web-access is not listed in the real profile settings");
  assert.equal(row.supported, true);
  assert.ok(row.has_extensions && row.extension_count >= 1);
  for (const marker of row.extensions) {
    assert.ok(!path.isAbsolute(marker), "inventory markers stay relative");
  }
  // Resolve the managed install root in-test (inventory rows never carry
  // paths) and require marker containment explicitly.
  const managedRoot = path.join(agentDir, "npm", "node_modules", "pi-web-access");
  const realRoot = fs.realpathSync(managedRoot);
  assert.ok(realRoot.startsWith(fs.realpathSync(path.join(agentDir, "npm", "node_modules"))));
  const scratchAgentDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-real-agent-"));
  const { createSdkSession } = await import("../sdk-transport.mjs");
  const { safeDefaultPolicy } = await import("../policy.mjs");
  const noopUi = {
    select: async () => undefined,
    confirm: async () => false,
    input: async () => undefined,
    notify: () => {},
    onTerminalInput: () => () => {},
    setStatus: () => {},
    setWorkingMessage: () => {},
    setWorkingVisible: () => {},
    setWorkingIndicator: () => {},
    setHiddenThinkingLabel: () => {},
    setWidget: () => {},
    setFooter: () => {},
  };
  // createSdkSession throws on any extension load error, so a resolved
  // session proves the real package root loaded under the managed
  // boundary (auto-discovery off, denylist exposure).
  const { session } = await createSdkSession({
    cwd: os.tmpdir(),
    agentDir: scratchAgentDir,
    tools: null,
    excludeTools: "edit,write,bash,powershell",
    extensionPaths: [realRoot],
    policy: safeDefaultPolicy(),
    uiContext: noopUi,
    onEvent: null,
  });
  try {
    assert.ok(session && typeof session.sessionId === "string" && session.sessionId);
    assert.ok(Array.isArray(session.getAllTools()));
  } finally {
    try {
      session.dispose();
    } catch { /* best effort */ }
  }
});
