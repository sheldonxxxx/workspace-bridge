// Managed Pi extension inventory + policy (milestone 3C2, extension policy v1).
//
// Source of installed packages is ONLY the isolated PI_CODING_AGENT_DIR user
// settings/profile: <agentDir>/settings.json packages[] (string/object source
// forms). Project .pi settings and the user's normal ~/.pi/agent tree are
// never consulted here. Required v1 support is user-scope npm packages:
// normalized npm identity (scoped names and pinned specs supported), resolved
// only under <agentDir>/npm/node_modules/<package-name>. Symlink/path escapes
// outside that managed npm root are rejected.
//
// For each package only bounded metadata crosses this boundary:
// id/source identity, package name, installed version, whether it has
// extension resources, bounded relative resource markers in exact Pi
// resolution order, package_json_sha256 fingerprint, supported flag +
// bounded reason. Full install paths, agentDir, tokens,
// arbitrary settings fields, package file contents and dependency lists are
// never returned. Markers are reported only for fully validated packages:
// count/path bounds are fail-closed acceptance limits, never truncation.
//
// Pi 0.86.1 resolution semantics (verified against the installed bundle
// and empirically with marker fixtures 2026-09-22):
// - an explicit `-e <dir>` whose directory holds package.json with
//   pi.extensions[] loads each listed entry verbatim (files AND
//   directories); otherwise index.ts/index.js; otherwise every nested
//   extension file (top-level .ts/.js plus one-level subdirectory
//   manifest/index resolution).
// - a manifest entry resolving to a directory is passed to the module
//   loader, which resolves <dir>/index.ts, else <dir>/index.js, else
//   <dir>/package.json "main" limited to same-directory files (bare,
//   ./-prefixed, .js/.ts extension guessing); subpath mains, index.mjs,
//   and main-less/index-less directories load nothing. A directory
//   always yields at most one concrete file; nested pi.extensions
//   manifests are never consulted and no deep walking occurs.
// Passing the package root is therefore sufficient and no glob behavior
// is invented here.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const EXTENSION_POLICY_VERSION = 1;
export const MAX_ENABLED_PACKAGES = 64;
export const MAX_INVENTORY_PACKAGES = 200;
export const MAX_POLICY_BYTES = 32768;
export const MAX_SETTINGS_BYTES = 256 * 1024;
export const MAX_PACKAGE_JSON_BYTES = 64 * 1024;
export const MAX_MANIFEST_ENTRIES = 32;
export const MAX_MANIFEST_ENTRY_CHARS = 400;
export const MAX_DISCOVERY_ENTRIES = 500;
// Bounds above are fail-closed package acceptance limits, not display
// truncation: a manifest with more than MAX_MANIFEST_ENTRIES entries, a
// declaration longer than MAX_MANIFEST_ENTRY_CHARS, or discovery beyond
// MAX_MANIFEST_ENTRIES loadable resources fails the whole package
// (manifest_too_large / resource_path_too_long / discovery_too_large),
// because Pi receives the entire package root and would load the
// unvalidated remainder. MAX_DISCOVERY_ENTRIES caps readdir traversal
// work; beyond it the package is rejected as discovery_unavailable.
export const MAX_ID_CHARS = 214;
export const MAX_VERSION_CHARS = 80;
export const MAX_REASON_CHARS = 200;

// Normalized npm package identity: `npm:<name>` without any version spec.
// The version pin (if any) selects the installed copy; it is never part of
// the policy identity.
export function normalizeExtensionId(source) {
  const text = typeof source === "string" ? source.trim() : "";
  if (!text.startsWith("npm:")) return "";
  const spec = text.slice(4).trim();
  if (!spec) return "";
  const parsed = parseNpmSpec(spec);
  if (!parsed.name) return "";
  if (!isValidPackageName(parsed.name)) return "";
  return `npm:${parsed.name}`;
}

// Mirror of Pi 0.86.1 parseNpmSpec: `name[@version]` with scoped names.
// Returns {name, version} where version may be undefined.
export function parseNpmSpec(spec) {
  const text = String(spec || "").trim();
  const match = text.match(/^(@?[^@]+(?:\/[^@]+)?)(?:@(.+))?$/);
  if (!match) return { name: text };
  return { name: match[1] ?? text, version: match[2] };
}

// Parse a full `npm:` settings source into {name, version, isAlias}.
// `isAlias` marks the npm alias form (`npm:alias@npm:real@x.y.z`), where
// the install directory name differs from the real package name by
// design. Aliases are explicitly unsupported in v1 (never silently
// resolved to another identity).
export function parseNpmSource(source) {
  const text = typeof source === "string" ? source.trim() : "";
  if (!text.startsWith("npm:")) return { name: "", version: undefined, isAlias: false };
  const parsed = parseNpmSpec(text.slice(4).trim());
  const version = parsed.version;
  return {
    name: parsed.name || "",
    version,
    isAlias: typeof version === "string" && version.startsWith("npm:"),
  };
}

export function isValidPackageName(name) {
  if (typeof name !== "string" || !name || name.length > MAX_ID_CHARS) return false;
  if (/[\s\0]/.test(name)) return false;
  if (name.startsWith(".") || name.startsWith("_")) return false;
  if (name.includes("\\") || name.includes(":")) return false;
  if (name.startsWith("@")) {
    const parts = name.split("/");
    if (parts.length !== 2) return false;
    const [scope, pkg] = parts;
    if (!scope || scope === "@" || !pkg) return false;
    if (!/^[a-z0-9-~][a-z0-9-._~]*$/i.test(scope.slice(1))) return false;
    if (/^[._]/.test(pkg)) return false;
    if (!/^[a-z0-9-~][a-z0-9-._~]*$/i.test(pkg)) return false;
    return true;
  }
  if (name.includes("/")) return false;
  if (/^[._]/.test(name)) return false;
  return /^[a-z0-9-~][a-z0-9-._~]*$/i.test(name);
}

export function isValidExtensionId(id) {
  if (typeof id !== "string" || !id.startsWith("npm:")) return false;
  return isValidPackageName(id.slice(4));
}

export function defaultExtensionPolicy() {
  return { version: EXTENSION_POLICY_VERSION, enabled: [] };
}

function sortKeysDeep(value) {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeysDeep(value[key]);
    return out;
  }
  return value;
}

export function canonicalExtensionJson(policy) {
  return JSON.stringify(sortKeysDeep(policy));
}

export function extensionRevision(policy) {
  return createHash("sha256").update(canonicalExtensionJson(policy), "utf8").digest("hex");
}

export class ExtensionPolicyError extends Error {
  constructor(message) {
    super(message);
    this.name = "ExtensionPolicyError";
  }
}

// Strictly validate a full v1 extension policy. Returns the canonical
// {version, enabled[]} with deterministic order preserved. Throws on any
// violation; invalid policy never partially applies.
export function validateExtensionPolicy(raw) {
  let value = raw;
  if (typeof value === "string") {
    if (Buffer.byteLength(value, "utf8") > MAX_POLICY_BYTES) {
      throw new ExtensionPolicyError("extension policy body is too large");
    }
    try {
      value = JSON.parse(value);
    } catch {
      throw new ExtensionPolicyError("extension policy must be a JSON object");
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ExtensionPolicyError("extension policy must be a JSON object");
  }
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes("version") || !keys.includes("enabled")) {
    throw new ExtensionPolicyError("extension policy must have exactly 'version' and 'enabled'");
  }
  if (value.version !== EXTENSION_POLICY_VERSION) {
    throw new ExtensionPolicyError(`extension policy version must be ${EXTENSION_POLICY_VERSION}`);
  }
  if (!Array.isArray(value.enabled)) {
    throw new ExtensionPolicyError("extension policy 'enabled' must be a list");
  }
  if (value.enabled.length > MAX_ENABLED_PACKAGES) {
    throw new ExtensionPolicyError(`extension policy allows at most ${MAX_ENABLED_PACKAGES} packages`);
  }
  const seen = new Set();
  const enabled = [];
  for (const item of value.enabled) {
    if (typeof item !== "string" || !isValidExtensionId(item)) {
      throw new ExtensionPolicyError("extension policy entries must be npm package identities");
    }
    if (seen.has(item)) {
      throw new ExtensionPolicyError(`extension policy entry ${item.slice(0, 80)} is duplicated`);
    }
    seen.add(item);
    enabled.push(item);
  }
  return { version: EXTENSION_POLICY_VERSION, enabled };
}

function sha256Hex(text) {
  return createHash("sha256").update(String(text ?? ""), "utf8").digest("hex");
}

function boundedReason(text) {
  return String(text || "").slice(0, MAX_REASON_CHARS);
}

// Read the user-scope packages[] list from the isolated agentDir settings.
// Returns {entries: [{source, raw}], error} where error is a bounded reason
// when settings are missing/malformed. Project settings are never read.
function readUserPackageEntries(agentDir) {
  const settingsPath = path.join(String(agentDir), "settings.json");
  let bytes;
  try {
    const stat = fs.statSync(settingsPath);
    if (!stat.isFile() || stat.size > MAX_SETTINGS_BYTES) {
      return { entries: [], error: "settings_unavailable" };
    }
    bytes = fs.readFileSync(settingsPath, "utf8");
  } catch {
    return { entries: [], error: "settings_unavailable" };
  }
  let parsed;
  try {
    parsed = JSON.parse(bytes);
  } catch {
    return { entries: [], error: "settings_malformed" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { entries: [], error: "settings_malformed" };
  }
  const list = parsed.packages;
  if (list === undefined) return { entries: [] };
  if (!Array.isArray(list)) return { entries: [], error: "settings_malformed" };
  const entries = [];
  for (const item of list.slice(0, MAX_INVENTORY_PACKAGES)) {
    const source = typeof item === "string" ? item
      : (item && typeof item === "object" && typeof item.source === "string" ? item.source : "");
    if (!source) continue;
    // Non-string/non-source entries are skipped: they cannot identify an
    // installable package and must not be guessed.
    const parsed = parseNpmSource(source);
    entries.push({ source, raw: item, name: parsed.name, version: parsed.version, isAlias: parsed.isAlias });
  }
  return { entries };
}

function isWithinOrEqual(parent, candidate) {
  if (candidate === parent) return true;
  const rel = path.relative(parent, candidate);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

// Resolve <agentDir>/npm/node_modules/<name> and reject symlink/path
// escapes outside that managed npm root. Returns {root} or {error}.
function resolveManagedPackageRoot(agentDir, name) {
  const npmRoot = path.normalize(path.join(String(agentDir), "npm", "node_modules"));
  const candidate = path.normalize(path.join(npmRoot, name));
  if (!isWithinOrEqual(npmRoot, candidate) && candidate !== npmRoot) {
    return { error: "path_escape" };
  }
  // Canonicalize the managed root itself (macOS /tmp -> /private/tmp and
  // similar aliases) before comparing the realpath of the candidate, so
  // valid installs are not misreported as escapes.
  let npmRootReal = npmRoot;
  try {
    npmRootReal = fs.realpathSync(npmRoot);
  } catch {
    npmRootReal = npmRoot;
  }
  let real;
  try {
    real = fs.realpathSync(candidate);
  } catch {
    return { error: "not_installed" };
  }
  if (!isWithinOrEqual(npmRootReal, real) && real !== npmRootReal) {
    return { error: "path_escape" };
  }
  let stat;
  try {
    stat = fs.statSync(real);
  } catch {
    return { error: "not_installed" };
  }
  if (!stat.isDirectory()) return { error: "not_installed" };
  return { root: real };
}

function readJsonFileBounded(full, maxBytes) {
  try {
    const stat = fs.statSync(full);
    if (!stat.isFile() || stat.size <= 0 || stat.size > maxBytes) return { error: "unreadable" };
    const text = fs.readFileSync(full, "utf8");
    return { text };
  } catch {
    return { error: "unreadable" };
  }
}

// Read bounded package.json metadata. Returns {name, version,
// fingerprint} or {error}. Never returns file contents or dependencies.
// The pi.extensions declarations are read separately by readPiDecls()
// with exact Pi 0.86.1 semantics (non-array declarations are absent,
// not malformed).
function readPackageMetadata(root) {
  const full = path.join(root, "package.json");
  const read = readJsonFileBounded(full, MAX_PACKAGE_JSON_BYTES);
  if (read.error) return { error: "manifest_unavailable" };
  let parsed;
  try {
    parsed = JSON.parse(read.text);
  } catch {
    return { error: "manifest_malformed" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { error: "manifest_malformed" };
  }
  const name = typeof parsed.name === "string" ? parsed.name.slice(0, MAX_ID_CHARS) : "";
  const version = typeof parsed.version === "string"
    ? parsed.version.slice(0, MAX_VERSION_CHARS)
    : "";
  if (!name || !isValidPackageName(name)) return { error: "manifest_malformed" };
  return { name, version, fingerprint: sha256Hex(read.text) };
}

// Read pi.extensions declarations with exact Pi 0.86.1 readPiManifest
// semantics: kept only when an array of strings, otherwise absent (Pi
// falls through to index/discovery; it never fails the package for a
// non-array shape). Returns the FULL declaration list or null.
//
// Deployment-critical: declarations are NEVER sliced or truncated here.
// Bridge passes the entire package root to Pi via `-e <package-root>`,
// so Pi reads the original full manifest. Count and path-length bounds
// are fail-closed package acceptance limits enforced by the resolver
// below, never partial-validation truncation.
function readPiDecls(root) {
  const full = path.join(root, "package.json");
  const read = readJsonFileBounded(full, MAX_PACKAGE_JSON_BYTES);
  if (read.error) return null;
  let parsed;
  try {
    parsed = JSON.parse(read.text);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const pi = parsed.pi;
  if (!pi || typeof pi !== "object" || Array.isArray(pi)) return null;
  const decls = pi.extensions;
  if (!Array.isArray(decls) || !decls.every((e) => typeof e === "string")) return null;
  if (!decls.length) return null;
  return decls;
}

function isExtensionFileName(name) {
  return typeof name === "string" && (name.endsWith(".ts") || name.endsWith(".js"));
}

// Resolve one manifest/discovery entry that is a directory using the
// empirically verified Pi 0.86.1 explicit-loader (jiti import) rule:
//   <dir>/index.ts, else <dir>/index.js,
//   else <dir>/package.json "main" resolving to a same-directory file
//   (bare or ./-prefixed, with .js/.ts extension guessing).
//
// Verified 2026-09-22 against installed Pi 0.86.1 with marker fixtures
// (`-e <dir>` + get_state, no provider): index.ts beats index.js (even a
// throwing main is ignored when index.js exists); same-dir mains
// ("main.js", "./main.js", extension-guessed "main") load; subpath mains
// ("lib/main.js", "./lib/main.js") and index.mjs do NOT load;
// main-less/index-less directories load nothing and fail no session.
// A directory therefore resolves to AT MOST one concrete extension file,
// which counts 1 toward the fail-closed resource limit. No nested
// pi.extensions manifests are consulted (Pi passes the directory
// straight to the module loader), and no recursive walking occurs.
// Returns {rel} of the single concrete file or {error}.
function resolveDirectoryResource(pkgRootReal, dirReal, lexResolved) {
  const markerFor = (name) => toPosixRel(pkgRootReal, path.join(lexResolved, name));
  // An index file that exists in ANY form is decisive: Pi never falls
  // past it, so an unusable index.ts/index.js fails the package instead
  // of silently trying the next candidate.
  for (const indexName of ["index.ts", "index.js"]) {
    let exists = false;
    try {
      exists = fs.existsSync(path.join(dirReal, indexName));
    } catch {
      exists = false;
    }
    if (!exists) continue;
    const checked = checkContainedFile(pkgRootReal, path.join(dirReal, indexName));
    if (checked.error) return { error: checked.error };
    return { rel: markerFor(indexName) };
  }
  const manifestRead = readJsonFileBounded(path.join(dirReal, "package.json"), MAX_PACKAGE_JSON_BYTES);
  if (!manifestRead.error) {
    let inner = null;
    try {
      inner = JSON.parse(manifestRead.text);
    } catch {
      inner = null;
    }
    const main = inner && typeof inner === "object" && !Array.isArray(inner) ? inner.main : undefined;
    if (typeof main === "string" && main && !main.includes("\0")) {
      if (main.length > MAX_MANIFEST_ENTRY_CHARS) return { error: "resource_path_too_long" };
      let target = main.startsWith("./") ? main.slice(2) : main;
      // Verified: only same-directory mains load; subpaths do not.
      if (!path.isAbsolute(target) && target !== "" && target !== "." && target !== ".."
          && !target.includes("/") && !target.includes("\\")) {
        const candidates = /[.](js|ts)$/.test(target)
          ? [target]
          : [target, `${target}.js`, `${target}.ts`];
        for (const candidate of candidates) {
          let exists = false;
          try {
            exists = fs.existsSync(path.join(dirReal, candidate));
          } catch {
            exists = false;
          }
          if (!exists) continue;
          const checked = checkContainedFile(pkgRootReal, path.join(dirReal, candidate));
          if (checked.error) return { error: checked.error };
          if (!/[.](js|ts)$/.test(candidate)) return { error: "resource_invalid" };
          return { rel: markerFor(candidate) };
        }
      } else {
        return { error: "resource_invalid" };
      }
    }
  }
  return { error: "resource_invalid" };
}

// Validate an existing path as a contained regular extension file:
// must be a file (following symlinks) with a .ts/.js name and a
// realpath inside the canonical package root. Returns {} or {error}.
function checkContainedFile(pkgRootReal, absolute) {
  let stat;
  try {
    stat = fs.statSync(absolute);
  } catch {
    return { error: "resource_missing" };
  }
  if (!stat.isFile()) return { error: "resource_invalid" };
  const base = path.basename(absolute);
  if (!(base.endsWith(".ts") || base.endsWith(".js"))) return { error: "resource_invalid" };
  let real;
  try {
    real = fs.realpathSync(absolute);
  } catch {
    return { error: "resource_missing" };
  }
  if (!isWithinOrEqual(pkgRootReal, real) && real !== pkgRootReal) {
    return { error: "resource_escape" };
  }
  return {};
}

function toPosixRel(pkgRootReal, absolute) {
  return path.relative(pkgRootReal, absolute).split(path.sep).join("/");
}

// Validate one extension resource Pi would resolve from baseDir (the
// package root or a discovery subdirectory). Mirrors Pi's
// path.resolve(baseDir, entry) but enforces containment: rejects
// NUL/absolute/traversal escapes, requires an existing regular file,
// and realpaths symlinks requiring them to stay within the canonical
// package root. Internal symlinks (resolving inside the root) are
// allowed because Pi itself loads them. Returns {rel} or {error}.
function checkResourceFile(pkgRootReal, baseDir, entry) {
  if (typeof entry !== "string" || !entry || entry.includes("\0")) {
    return { error: "resource_invalid" };
  }
  // Path-length bounds are fail-closed acceptance limits: the FULL
  // declared string is judged, never a truncated prefix (Pi resolves the
  // original string, so prefix validation could approve a path Pi loads
  // differently).
  if (entry.length > MAX_MANIFEST_ENTRY_CHARS) return { error: "resource_path_too_long" };
  if (path.isAbsolute(entry)) return { error: "resource_escape" };
  const resolved = path.resolve(baseDir, entry);
  if (!isWithinOrEqual(pkgRootReal, resolved)) return { error: "resource_escape" };
  let stat;
  try {
    stat = fs.statSync(resolved);
  } catch {
    return { error: "resource_missing" };
  }
  if (stat.isDirectory()) {
    // Directory resources go through the verified explicit-loader rule
    // (index.ts/index.js/same-dir main); the single concrete file counts
    // 1. No nested-manifest recursion, no deep walking.
    let real;
    try {
      real = fs.realpathSync(resolved);
    } catch {
      return { error: "resource_missing" };
    }
    if (!isWithinOrEqual(pkgRootReal, real) && real !== pkgRootReal) {
      return { error: "resource_escape" };
    }
    return resolveDirectoryResource(pkgRootReal, real, resolved);
  }
  if (!stat.isFile()) return { error: "resource_invalid" };
  let real;
  try {
    real = fs.realpathSync(resolved);
  } catch {
    return { error: "resource_missing" };
  }
  if (!isWithinOrEqual(pkgRootReal, real) && real !== pkgRootReal) {
    return { error: "resource_escape" };
  }
  // Display marker follows the declared/lexical path (what Pi actually
  // loads); containment was verified against the realpath above.
  return { rel: toPosixRel(pkgRootReal, resolved) };
}

// Mirror of Pi 0.86.1 resolveExtensionEntries2(dir): manifest entries
// that exist, else index.ts, else index.js, else null. Every candidate
// additionally passes containment; any failure is explicit (fail
// closed) instead of Pi's silent skip.
function resolveEntriesInDir(pkgRootReal, dir) {
  const decls = readPiDecls(dir);
  if (decls) {
    // Fail-closed count limit: Pi would load every declared entry, so a
    // manifest beyond the acceptance maximum fails the package instead
    // of validating only the first 32.
    if (decls.length > MAX_MANIFEST_ENTRIES) {
      return { resources: [], error: "manifest_too_large" };
    }
    const resources = [];
    for (const entry of decls) {
      const checked = checkResourceFile(pkgRootReal, dir, entry);
      if (checked.error) return { resources: [], error: checked.error };
      resources.push(checked.rel);
    }
    if (resources.length) return { resources, kind: "manifest" };
  }
  for (const indexName of ["index.ts", "index.js"]) {
    const full = path.join(dir, indexName);
    let exists = false;
    try {
      exists = fs.existsSync(full);
    } catch {
      exists = false;
    }
    if (!exists) continue;
    const checked = checkResourceFile(pkgRootReal, dir, indexName);
    if (checked.error) return { resources: [], error: checked.error };
    return { resources: [checked.rel], kind: "index" };
  }
  return { resources: [], kind: "none" };
}

// Mirror of Pi 0.86.1 discoverExtensionsInDir(dir): top-level .ts/.js
// files plus, for each subdirectory, resolveExtensionEntries2(subdir).
// Bounded traversal; symlinked candidates must stay contained; any
// ambiguity fails closed.
function discoverExtensions(pkgRootReal, dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return { resources: [], error: "discovery_unavailable" };
  }
  if (entries.length > MAX_DISCOVERY_ENTRIES) {
    return { resources: [], error: "discovery_too_large" };
  }
  const resources = [];
  for (const entry of entries) {
    let isFile = false;
    let isDir = false;
    try {
      isFile = entry.isFile() || entry.isSymbolicLink();
      isDir = entry.isDirectory() || entry.isSymbolicLink();
    } catch {
      return { resources: [], error: "resource_invalid" };
    }
    const entryPath = path.join(dir, entry.name);
    if (isExtensionFileName(entry.name) && isFile) {
      const checked = checkResourceFile(pkgRootReal, dir, entry.name);
      if (checked.error) return { resources: [], error: checked.error };
      resources.push(checked.rel);
      continue;
    }
    if (isDir) {
      // A symlinked directory escaping the package root must not be
      // traversed: Pi would follow it, so fail the package instead.
      let real;
      try {
        real = fs.realpathSync(entryPath);
      } catch {
        return { resources: [], error: "resource_missing" };
      }
      if (!isWithinOrEqual(pkgRootReal, real) && real !== pkgRootReal) {
        return { resources: [], error: "resource_escape" };
      }
      const nested = resolveEntriesInDir(pkgRootReal, entryPath);
      if (nested.error) return { resources: [], error: nested.error };
      resources.push(...nested.resources);
    }
    // No early break: every candidate in the bounded readdir set is
    // validated, so a dangerous resource after 32 harmless ones still
    // rejects the package. The count limit below fails the package
    // instead of returning a trimmed safe subset (Pi receives the whole
    // root).
  }
  if (resources.length > MAX_MANIFEST_ENTRIES) {
    return { resources: [], error: "discovery_too_large" };
  }
  return { resources, kind: "discovery" };
}

// One package-resource resolver used by BOTH inventory classification and
// enabled-root validation (3C2 corrective). Exact Pi 0.86.1 order for
// `-e <package-root>`: manifest entries, else root index.ts/index.js,
// else fallback directory discovery. Deliberate fail-closed strictness
// beyond Pi: missing/escaping/non-file manifest resources fail the
// package instead of being silently skipped. Returns
// {kind, resources} or {kind, resources: [], error}.
export function resolvePackageExtensionResources(rootReal) {
  const pkgRootReal = path.normalize(String(rootReal));
  const top = resolveEntriesInDir(pkgRootReal, pkgRootReal);
  if (top.error) return { kind: "manifest", resources: [], error: top.error };
  if (top.resources.length) return { kind: top.kind, resources: top.resources };
  if (top.kind !== "none") return { kind: top.kind, resources: [] };
  const found = discoverExtensions(pkgRootReal, pkgRootReal);
  if (found.error) return { kind: "discovery", resources: [], error: found.error };
  if (found.resources.length) return { kind: "discovery", resources: found.resources };
  return { kind: "none", resources: [] };
}

// Build the bounded native inventory for the isolated agentDir. Never
// includes host paths, agentDir, tokens, or arbitrary settings fields.
export function readExtensionInventory(agentDir) {
  const { entries, error } = readUserPackageEntries(agentDir);
  if (error && entries.length === 0) {
    return { packages: [], error };
  }
  const packages = [];
  const seen = new Set();
  for (const entry of entries) {
    const id = normalizeExtensionId(entry.source);
    if (!id) {
      // Non-npm package sources are reported as unsupported rows only
      // when cheaply identifiable; git/local resolution is NOT attempted.
      const hint = String(entry.source || "").slice(0, 80);
      packages.push({
        id: hint.startsWith("npm:") ? hint.slice(0, MAX_ID_CHARS + 4) : `unsupported:${sha256Hex(hint).slice(0, 16)}`,
        name: "",
        version: "",
        has_extensions: false,
        extensions: [],
        extension_count: 0,
        package_json_sha256: "",
        supported: false,
        reason: "unsupported_source",
      });
      continue;
    }
    if (seen.has(id)) continue;
    seen.add(id);
    const resolved = resolveManagedPackageRoot(agentDir, id.slice(4));
    if (resolved.error) {
      packages.push({
        id,
        name: id.slice(4),
        version: "",
        has_extensions: false,
        extensions: [],
        extension_count: 0,
        package_json_sha256: "",
        supported: false,
        reason: resolved.error === "path_escape" ? "path_escape" : "not_installed",
      });
      continue;
    }
    const meta = readPackageMetadata(resolved.root);
    if (meta.error) {
      packages.push({
        id,
        name: id.slice(4),
        version: "",
        has_extensions: false,
        extensions: [],
        extension_count: 0,
        package_json_sha256: "",
        supported: false,
        reason: boundedReason(meta.error),
      });
      continue;
    }
    // Package identity: the installed package.json name must equal the
    // settings-derived install name. Pi itself derives the install dir
    // from the spec name without cross-checking, so a mismatch is
    // ambiguous: npm aliases (`npm:alias@npm:real@...`, directory `alias`
    // holding package `real`) are explicitly unsupported in v1, and any
    // other mismatch fails explicitly rather than silently treating
    // `npm:foo` as package `bar`.
    if (meta.name !== entry.name) {
      packages.push({
        id,
        name: meta.name,
        version: meta.version,
        has_extensions: false,
        extensions: [],
        extension_count: 0,
        package_json_sha256: meta.fingerprint,
        supported: false,
        reason: entry.isAlias ? "npm_alias_unsupported" : "identity_mismatch",
      });
      continue;
    }
    // Shared Pi-parity resolver: manifest, else root index.ts/index.js,
    // else fallback directory discovery. has_extensions/support counts
    // reflect what Pi would actually load from `-e <package-root>`.
    const resolvedResources = resolvePackageExtensionResources(resolved.root);
    const hasExtensions = !resolvedResources.error && resolvedResources.resources.length > 0;
    // Reported only for fully validated packages: the validated set is
    // already within acceptance bounds, so the slice below is a display
    // safeguard that never narrows what was validated.
    packages.push({
      id,
      name: meta.name,
      version: meta.version,
      has_extensions: hasExtensions,
      extensions: hasExtensions ? resolvedResources.resources.slice(0, MAX_MANIFEST_ENTRIES) : [],
      extension_count: hasExtensions ? resolvedResources.resources.length : 0,
      package_json_sha256: meta.fingerprint,
      supported: hasExtensions,
      reason: hasExtensions ? "" : boundedReason(resolvedResources.error || "no_extension_resources"),
    });
    if (packages.length >= MAX_INVENTORY_PACKAGES) break;
  }
  return { packages };
}

// Canonicalize enabled IDs into live inventory/settings order (no reorder
// UI in v1). The revision represents the active set/load order, so
// semantically identical sets always share one stored policy + revision
// regardless of checkbox/API input ordering. Unknown IDs keep their
// relative order at the end; validation rejects them elsewhere.
export function canonicalizeEnabledOrder(packages, enabledIds) {
  const order = new Map();
  for (const row of packages) {
    if (row && typeof row.id === "string" && !order.has(row.id)) order.set(row.id, order.size);
  }
  return [...enabledIds].sort((a, b) => {
    const ia = order.has(a) ? order.get(a) : Number.MAX_SAFE_INTEGER;
    const ib = order.has(b) ? order.get(b) : Number.MAX_SAFE_INTEGER;
    return ia - ib;
  });
}

// Resolve enabled policy IDs to native installed package roots in
// deterministic inventory/settings order. Throws ExtensionPolicyError when
// any enabled package is missing, invalid, or unusable. Never silently
// skips an enabled package.
export function resolveEnabledExtensionRoots(agentDir, enabledIds) {
  const policy = validateExtensionPolicy({ version: EXTENSION_POLICY_VERSION, enabled: enabledIds });
  const { packages, error } = readExtensionInventory(agentDir);
  if (error && packages.length === 0) {
    throw new ExtensionPolicyError("Extension inventory is unavailable; refusing to broaden policy");
  }
  const byId = new Map(packages.map((p) => [p.id, p]));
  // Fail closed on any enabled ID that is unknown, not installed, or has
  // no extension resources: never silently skip an enabled package.
  for (const id of policy.enabled) {
    const row = byId.get(id);
    if (!row || row.supported !== true || row.has_extensions !== true) {
      throw new ExtensionPolicyError(
        `Enabled extension ${id.slice(0, 80)} is not installed or has no extension resources`);
    }
  }
  // Deterministic inventory/settings order (no reorder UI in v1), not
  // caller order.
  const roots = [];
  const snapshot = [];
  for (const row of packages) {
    if (!policy.enabled.includes(row.id)) continue;
    const id = row.id;
    const resolved = resolveManagedPackageRoot(agentDir, id.slice(4));
    if (resolved.error || !resolved.root) {
      throw new ExtensionPolicyError(
        `Enabled extension ${id.slice(0, 80)} is unavailable`);
    }
    // Re-verify the manifest fingerprint matches the inventoried row so a
    // package swapped between inventory and spawn cannot slip through.
    const meta = readPackageMetadata(resolved.root);
    if (meta.error || meta.fingerprint !== row.package_json_sha256) {
      throw new ExtensionPolicyError(
        `Enabled extension ${id.slice(0, 80)} changed during session creation`);
    }
    // Re-run the shared resolver at spawn time: resource escapes or
    // removals introduced after inventory must fail the session instead
    // of reaching Pi's loader.
    const atSpawn = resolvePackageExtensionResources(resolved.root);
    if (atSpawn.error || !atSpawn.resources.length) {
      throw new ExtensionPolicyError(
        `Enabled extension ${id.slice(0, 80)} is unavailable`);
    }
    roots.push(resolved.root);
    snapshot.push({
      id: row.id,
      name: row.name,
      version: row.version,
      fingerprint: row.package_json_sha256,
    });
  }
  return { roots, snapshot };
}
