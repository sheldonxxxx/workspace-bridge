// Environment configuration for the native Pi host adapter.
//
// Runs natively as the normal macOS user (not Docker, not root). Resolves the
// Pi binary, the projects parent, and the isolated PI_CODING_AGENT_DIR without
// touching the user's normal ~/.pi/agent tree and without logging full paths.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const ADAPTER_VERSION = "0.2.0";
export const DEFAULT_HOST = "127.0.0.1";
export const DEFAULT_PORT = 8780;
export const DEFAULT_PI_BINARY = "pi";
export const READONLY_TOOLS = ["read", "grep", "find", "ls"];
export const PI_VERSION_TIMEOUT_MS = 10000;

export function defaultAgentDir(homeDir) {
  const home = homeDir || os.homedir();
  return path.join(home, ".pi", "workspace-bridge");
}

// Expand a leading ~, $HOME or ${HOME} using the explicit home directory.
// Only a leading token is expanded; anything else is left untouched.
export function expandAgentDir(raw, homeDir) {
  const home = homeDir || os.homedir();
  const value = String(raw || "").trim();
  if (!value) return defaultAgentDir(home);
  if (value === "~") return home;
  if (value.startsWith("~/")) return path.join(home, value.slice(2));
  if (value === "$HOME") return home;
  if (value.startsWith("$HOME/")) return path.join(home, value.slice(6));
  if (value === "${HOME}") return home;
  if (value.startsWith("${HOME}/")) return path.join(home, value.slice(8));
  return value;
}

// Resolve the agent dir to an absolute path. Never creates directories here;
// creation (if needed) is the Pi child's own behavior at runtime.
export function resolveAgentDir(raw, homeDir) {
  const expanded = expandAgentDir(raw, homeDir);
  const absolute = path.isAbsolute(expanded)
    ? path.normalize(expanded)
    : path.resolve(expanded);
  return absolute;
}

// Fail closed when the resolved agent dir would collide with the user's
// normal Pi agent tree (~/.pi/agent) or the ~/.pi root itself.
//
// The normal Pi tree means ~/.pi/agent AND every descendant. The check is
// symlink-aware without requiring the final agent dir to already exist and
// without creating anything: the nearest existing ancestor is resolved with
// realpath, the remaining suffix is reconstructed, and canonical paths are
// compared. The home/normal-tree side is canonicalized the same way, so
// symlinked ancestors or aliases resolving into the normal tree are rejected
// while valid dirs elsewhere (including ~/.pi/workspace-bridge) stay allowed.
function canonicalizeCandidate(absolute) {
  const normalized = path.normalize(absolute);
  const existing = [normalized];
  let cursor = normalized;
  while (!fs.existsSync(cursor)) {
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
    existing.push(cursor);
  }
  let realBase;
  try {
    realBase = fs.realpathSync(cursor);
  } catch {
    realBase = cursor;
  }
  let rebuilt = realBase;
  for (let index = existing.length - 2; index >= 0; index -= 1) {
    rebuilt = path.join(rebuilt, path.basename(existing[index]));
  }
  return path.normalize(rebuilt);
}

function isWithinOrEqual(parent, candidate) {
  if (candidate === parent) return true;
  const rel = path.relative(parent, candidate);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

export function isAgentDirAllowed(resolvedAbsolute, homeDir) {
  const home = homeDir || os.homedir();
  const candidate = canonicalizeCandidate(String(resolvedAbsolute));
  const homeCanon = canonicalizeCandidate(path.normalize(home));
  // The protected tree itself goes through the same nearest-existing-ancestor
  // realpath resolution: if ~/.pi/agent is a symlink to a physical location,
  // that physical target (and its descendants) is what gets protected.
  const normalTree = canonicalizeCandidate(path.join(homeCanon, ".pi", "agent"));
  const piRoot = canonicalizeCandidate(path.join(homeCanon, ".pi"));
  if (isWithinOrEqual(normalTree, candidate)) return false;
  if (candidate === piRoot) return false;
  return true;
}

export function loadConfig(env = process.env, homeDir = os.homedir()) {
  const token = String(env.WB_RUNTIME_TOKEN || "");
  const host = String(env.WB_PI_ADAPTER_HOST || DEFAULT_HOST).trim() || DEFAULT_HOST;
  const portRaw = String(env.WB_PI_ADAPTER_PORT || DEFAULT_PORT).trim();
  const port = Number(portRaw);
  const binary = String(env.WB_PI_BINARY || DEFAULT_PI_BINARY).trim() || DEFAULT_PI_BINARY;
  const projectsDir = String(env.WB_PI_PROJECTS_DIR || env.WB_PROJECTS_DIR || "").trim();
  const agentDirRaw = String(env.PI_CODING_AGENT_DIR || "").trim();
  return {
    adapterVersion: ADAPTER_VERSION,
    host,
    port: Number.isSafeInteger(port) && port > 0 && port < 65536 ? port : DEFAULT_PORT,
    token,
    locked: !token,
    tokenConfigured: Boolean(token),
    piBinary: binary,
    projectsDirRaw: projectsDir,
    projectsConfigured: Boolean(projectsDir),
    // Absolute resolved agent dir injected into EVERY Pi child. The full
    // path is never logged or returned; health exposes booleans only.
    agentDir: resolveAgentDir(agentDirRaw, homeDir),
    agentDirExplicit: Boolean(agentDirRaw),
  };
}

// Synchronously check `binary --version` with a bounded timeout. Never
// installs or upgrades Pi. Returns { usable, version } with a short bounded
// version string, or usable=false when missing/unusable.
export function checkPiBinary(binary, timeoutMs = PI_VERSION_TIMEOUT_MS) {
  const target = String(binary || "").trim() || DEFAULT_PI_BINARY;
  try {
    const result = spawnSync(target, ["--version"], {
      timeout: timeoutMs,
      encoding: "utf8",
      windowsHide: true,
    });
    if (result.error || result.status !== 0) {
      return { usable: false, version: "" };
    }
    const version = String(result.stdout || result.stderr || "").trim().split(/\s+/)[0].slice(0, 40);
    if (!version) return { usable: false, version: "" };
    return { usable: true, version };
  } catch {
    return { usable: false, version: "" };
  }
}

// Legacy read-only argv without the trusted extension. Kept only for the
// legacy no-policy session-creation compatibility path; normal v2 sessions
// (read-only or writable) always use piRpcArgvFor(), which adds the
// package-owned trusted extension. No shell is ever used.
// --no-approve overrides project trust for the run and --no-extensions
// disables extension discovery, so project-local resources cannot enable
// extensions inside this milestone's read-only spike.
export function piRpcArgv() {
  return ["--mode", "rpc", "--tools", READONLY_TOOLS.join(","), "--no-approve", "--no-extensions"];
}

export const WRITABLE_TOOLS = ["read", "grep", "find", "ls", "edit", "write"];

// Package-owned trusted permission extension path. Derived from this
// package's own module location: never admin/project/env-selectable, so a
// hostile project or environment cannot substitute its own extension.
export function trustedExtensionPath() {
  return path.join(path.dirname(new URL(import.meta.url).pathname),
    "trusted-permission-extension.mjs");
}

// Fail closed unless the trusted extension is a regular file owned by this
// package (exists, is a file, non-empty). Session creation in writable mode
// requires this check to pass; the path itself is never logged.
export function isTrustedExtensionUsable(candidate) {
  const target = candidate || trustedExtensionPath();
  try {
    const stat = fs.statSync(target);
    return stat.isFile() && stat.size > 0 && stat.size < 1024 * 1024;
  } catch {
    return false;
  }
}

// Centralized spawn contract (3C1, v3):
// - writable=false: read/grep/find/ls plus exactly one package-owned
//   trusted extension (read policy is enforced in read-only mode too).
// - writable=true: + edit/write.
// - shellMode != deny: + bash (Pi built-in bash only, no powershell).
//   deny removes bash; ask keeps bash with suspended-call approval;
//   allow keeps bash without prompt. No command rules. Throws when the
//   trusted extension is missing.
// - piRpcArgv() stays as the legacy no-extension read-only argv, used only
//   for the legacy no-policy compatibility path.
export function piRpcArgvFor({ writable = false, shellMode = "deny" } = {}) {
  const extension = trustedExtensionPath();
  if (!isTrustedExtensionUsable(extension)) {
    throw new Error("Trusted permission extension is unavailable");
  }
  const tools = [...(writable ? WRITABLE_TOOLS : READONLY_TOOLS)];
  if (shellMode !== "deny") tools.push("bash");
  return ["--mode", "rpc", "--tools", tools.join(","),
    "--no-approve", "--no-extensions", "-e", extension];
}
