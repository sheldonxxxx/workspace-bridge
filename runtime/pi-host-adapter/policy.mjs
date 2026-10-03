// Package-owned Pi permission policy (milestone 3C1, policy v3).
//
// Single shared evaluator used by the trusted permission extension and by
// the adapter's RPC correlation layer. The Bridge web manager owns the
// *operational* policy (write-tool exposure switch, per-tool allow/ask/deny
// for the six file tools, protected workspace-relative globs,
// session-always availability, external file scope, and a single shell
// authority mode deny|ask|allow over the Pi built-in bash tool only).
// There are no project-name/path-specific rules, no permission-source or
// control-directory fixed denies, and no command rule list. Protected
// patterns remain ADMIN-CONFIGURED rules; ordinary configurable
// file/external policy applies to normal project edits.
//
// Non-configurable protocol integrity only:
//
// - exact session + toolCallId + UI-request correlation (adapter layer);
// - stale/forged approvals never authorize;
// - immutable session policy revision (changes apply to new sessions only);
// - package-owned trusted extension loading identity;
// - malformed protocol/tool input fails closed;
// - no silent authority widening or fallback.
//
// Shell Allow runs with native macOS-user authority and can bypass
// structured file path controls; Ask pauses each bash invocation with the
// existing suspended-call permission flow. No powershell, no command
// regex/prefix/allowlist/denylist.
//
// Defense in depth: the adapter validates Bridge-delivered policy AGAIN with
// validatePolicy() before storing a session snapshot; the extension
// re-validates the snapshot it receives via environment. Invalid policy
// fails closed.
//
// Version history: v1 (3B1) used an `enabled` master switch. v2 (3B2) used
// `write_tools_enabled` plus `external_access`. v3 (3C1) adds `shell_mode`
// and removes fixed filesystem denies. v1/v2 payloads are NOT
// interpretable here: the adapter fails them clearly (invalid_policy)
// instead of silently misinterpreting them, so Bridge and adapter must be
// upgraded together (the Bridge migrates stored v1/v2 to v3 in memory and
// only ever sends v3).
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const POLICY_VERSION = 3;
export const SUPPORTED_TOOLS = ["read", "grep", "find", "ls", "edit", "write"];
export const POLICY_MODES = ["allow", "ask", "deny"];
export const SHELL_TOOL = "bash";
export const SHELL_MODES = ["deny", "ask", "allow"];
export const DEFAULT_SHELL_MODE = "deny";
export const MAX_PROTECTED_PATTERNS = 64;
export const MAX_PATTERN_CHARS = 400;
export const MAX_POLICY_BYTES = 32768;
export const MAX_REASON_CHARS = 200;
export const MAX_EXTERNAL_ROOTS = 32;
export const MAX_EXTERNAL_ROOT_CHARS = 1024;
// Shell command evidence bounds: exact bounded command target ~16 KiB.
export const MAX_SHELL_COMMAND_CHARS = 16384;

// Rank for "most restrictive wins" composition: deny > ask > allow.
const MODE_RANK = { allow: 0, ask: 1, deny: 2 };

// Fixed protocol-integrity invariants shown read-only in the web manager.
export const FIXED_INVARIANTS = [
  "Exact session + toolCallId + UI-request correlation",
  "Stale or forged approvals never authorize",
  "Session policy revision is immutable; changes apply to new sessions only",
  "Trusted extension path is package-owned, never admin/project/env-selectable",
  "Malformed protocol or tool input fails closed",
  "No silent authority widening or fallback",
];

export class PolicyError extends Error {
  constructor(message) {
    super(message);
    this.name = "PolicyError";
  }
}

export function safeDefaultPolicy() {
  return {
    version: POLICY_VERSION,
    write_tools_enabled: false,
    tools: {
      read: "allow",
      grep: "allow",
      find: "allow",
      ls: "allow",
      edit: "ask",
      write: "ask",
    },
    protected_patterns: [".git/**", ".env", ".env.*"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
    external_access: {
      default_mode: "deny",
      roots: [],
    },
    shell_mode: DEFAULT_SHELL_MODE,
  };
}

function checkShellMode(value) {
  if (!SHELL_MODES.includes(value)) {
    throw new PolicyError("policy 'shell_mode' must be one of deny, ask, allow");
  }
  return value;
}

function checkPatternList(value, name) {
  if (!Array.isArray(value)) {
    throw new PolicyError(`${name} must be a list`);
  }
  if (value.length > MAX_PROTECTED_PATTERNS) {
    throw new PolicyError(`${name} allows at most ${MAX_PROTECTED_PATTERNS} patterns`);
  }
  const cleaned = [];
  for (const item of value) {
    if (typeof item !== "string" || !item || item.length > MAX_PATTERN_CHARS) {
      throw new PolicyError(`${name} entries must be 1..${MAX_PATTERN_CHARS} chars`);
    }
    if (item.includes("\0")) throw new PolicyError(`${name} entry is invalid`);
    const text = item.trim();
    if (!text || text.length > MAX_PATTERN_CHARS) {
      throw new PolicyError(`${name} entry is invalid`);
    }
    if (text.startsWith("/") || text.startsWith("\\")) {
      throw new PolicyError(`${name} must be workspace-relative, not absolute`);
    }
    const parts = text.replace(/\\/g, "/").split("/").filter((p) => p !== "" && p !== ".");
    if (parts.includes("..")) {
      throw new PolicyError(`${name} must not traverse with '..'`);
    }
    if (!/^[A-Za-z0-9\-_. /+*{ }?[\],!@#%^()=:#]+$/.test(text)) {
      throw new PolicyError(`${name} entry uses unsupported syntax`);
    }
    if (text.includes("[") && !text.includes("]")) {
      throw new PolicyError(`${name} entry has an unterminated character class`);
    }
    cleaned.push(text);
  }
  return [...new Set(cleaned)];
}

function checkExternalRoot(value, index) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PolicyError(`external root #${index} must be an object`);
  }
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes("path") || !keys.includes("mode")) {
    throw new PolicyError(`external root #${index} must have exactly 'path' and 'mode'`);
  }
  const rawPath = value.path;
  if (typeof rawPath !== "string" || !rawPath) {
    throw new PolicyError(`external root #${index} 'path' must be a non-empty string`);
  }
  if (rawPath.includes("\0")) throw new PolicyError(`external root #${index} 'path' is invalid`);
  const text = rawPath.trim();
  if (!text || text.length > MAX_EXTERNAL_ROOT_CHARS) {
    throw new PolicyError(`external root #${index} 'path' must be 1..${MAX_EXTERNAL_ROOT_CHARS} chars`);
  }
  // Absolute POSIX host path syntax only. The Bridge validates this same
  // syntax (it may lack host filesystem visibility); the native adapter
  // canonicalizes each root with realpath and requires an existing
  // directory at session creation.
  if (!text.startsWith("/")) {
    throw new PolicyError(`external root #${index} 'path' must be an absolute path`);
  }
  if (text.includes("//") || (text.endsWith("/") && text !== "/")) {
    throw new PolicyError(`external root #${index} 'path' is not normalized`);
  }
  const segments = text.split("/").filter((s) => s !== "" && s !== ".");
  if (segments.includes("..")) {
    throw new PolicyError(`external root #${index} 'path' must not traverse with '..'`);
  }
  if (!POLICY_MODES.includes(value.mode)) {
    throw new PolicyError(`external root #${index} 'mode' must be one of allow, ask, deny`);
  }
  return { path: text, mode: value.mode };
}

function checkExternalAccess(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PolicyError("'external_access' must be an object");
  }
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes("default_mode") || !keys.includes("roots")) {
    throw new PolicyError("'external_access' must have exactly 'default_mode' and 'roots'");
  }
  if (!POLICY_MODES.includes(value.default_mode)) {
    throw new PolicyError("external 'default_mode' must be one of allow, ask, deny");
  }
  if (!Array.isArray(value.roots)) {
    throw new PolicyError("external 'roots' must be a list");
  }
  if (value.roots.length > MAX_EXTERNAL_ROOTS) {
    throw new PolicyError(`external 'roots' allows at most ${MAX_EXTERNAL_ROOTS} roots`);
  }
  const cleaned = value.roots.map((item, index) => checkExternalRoot(item, index));
  const seen = new Set();
  for (const entry of cleaned) {
    if (seen.has(entry.path)) {
      throw new PolicyError(`external root '${entry.path.slice(0, 80)}' is duplicated`);
    }
    seen.add(entry.path);
  }
  return { default_mode: value.default_mode, roots: cleaned };
}

// Strictly validate a full v3 policy object. Returns the canonical policy.
// Throws PolicyError on any violation; invalid policy never partially applies.
// There is no command rule list: shell_mode is the only shell control.
export function validatePolicy(raw) {
  let value = raw;
  if (typeof value === "string") {
    if (Buffer.byteLength(value, "utf8") > MAX_POLICY_BYTES) {
      throw new PolicyError("policy body is too large");
    }
    try {
      value = JSON.parse(value);
    } catch {
      throw new PolicyError("policy must be a JSON object");
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PolicyError("policy must be a JSON object");
  }
  const allowed = new Set([
    "version", "write_tools_enabled", "tools", "protected_patterns",
    "protected_template_exceptions", "allow_session_always", "external_access",
    "shell_mode",
  ]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new PolicyError("policy has unknown fields");
  }
  if (value.version !== POLICY_VERSION) {
    throw new PolicyError(`policy version must be ${POLICY_VERSION}`);
  }
  if (typeof value.write_tools_enabled !== "boolean") {
    throw new PolicyError("policy 'write_tools_enabled' must be a boolean");
  }
  const tools = value.tools;
  if (!tools || typeof tools !== "object" || Array.isArray(tools)) {
    throw new PolicyError("policy 'tools' must be an object");
  }
  const names = Object.keys(tools);
  if (names.length !== SUPPORTED_TOOLS.length || !SUPPORTED_TOOLS.every((t) => names.includes(t))) {
    throw new PolicyError(`policy 'tools' must cover exactly: ${SUPPORTED_TOOLS.join(",")}`);
  }
  for (const name of SUPPORTED_TOOLS) {
    if (!POLICY_MODES.includes(tools[name])) {
      throw new PolicyError(`policy tool ${name} must be one of allow, ask, deny`);
    }
  }
  const protectedPatterns = checkPatternList(value.protected_patterns, "protected_patterns");
  const exceptions = checkPatternList(
    value.protected_template_exceptions, "protected_template_exceptions");
  if (typeof value.allow_session_always !== "boolean") {
    throw new PolicyError("policy 'allow_session_always' must be a boolean");
  }
  const externalAccess = checkExternalAccess(value.external_access);
  const shellMode = checkShellMode(value.shell_mode);
  return {
    version: POLICY_VERSION,
    write_tools_enabled: value.write_tools_enabled,
    tools: Object.fromEntries(SUPPORTED_TOOLS.map((t) => [t, tools[t]])),
    protected_patterns: protectedPatterns,
    protected_template_exceptions: exceptions,
    allow_session_always: value.allow_session_always,
    external_access: externalAccess,
    shell_mode: shellMode,
  };
}

// Extract the exact bounded bash command + verified timeout from tool input.
// Verified against installed Pi 0.86.1 bashSchema: {command, timeout?}
// where timeout is SECONDS (optional, no default). Millisecond aliases
// (timeoutMs/timeout_ms) are accepted from scripted fakes. Returns
// {ok, command, commandHash, timeoutMs} or {ok:false}. Never throws;
// unknown shapes fail closed without dumping args. No command
// regex/prefix/allowlist/denylist: the full command string is the
// authority scope.
export function extractBashCommand(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return { ok: false };
  }
  const raw = input.command ?? input.cmd ?? input.script ?? input.code ?? null;
  if (typeof raw !== "string" || !raw) return { ok: false };
  const command = raw.slice(0, MAX_SHELL_COMMAND_CHARS);
  const truncated = raw.length > MAX_SHELL_COMMAND_CHARS;
  let timeoutMs = 30000;
  const msCandidate = input.timeoutMs ?? input.timeout_ms ?? null;
  const secCandidate = input.timeout ?? null;
  const coerceMs = (value) => {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
    return null;
  };
  const msValue = coerceMs(msCandidate);
  if (msValue !== null) {
    timeoutMs = Math.max(1000, Math.min(Math.floor(msValue), 300000));
  } else {
    const secValue = coerceMs(secCandidate);
    if (secValue !== null) {
      timeoutMs = Math.max(1000, Math.min(Math.floor(secValue * 1000), 300000));
    }
  }
  const commandHash = createHash("sha256").update(raw, "utf8").digest("hex");
  return { ok: true, command, truncated, commandHash, timeoutMs };
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

// Canonical JSON: sorted keys, no spaces (matches the Bridge Python
// canonicalization for ASCII content).
export function canonicalJson(policy) {
  return JSON.stringify(sortKeysDeep(policy));
}

// Stable policy revision: sha256 hex of the canonical policy JSON.
export function policyRevision(policy) {
  return createHash("sha256").update(canonicalJson(policy), "utf8").digest("hex");
}

// Convert a validated workspace-relative glob to a RegExp over
// workspace-relative paths (forward slashes). Supports *, ?, ** and [...]
// character classes. A trailing "/**" also matches the base directory.
export function globToRegExp(glob) {
  let out = "";
  let i = 0;
  const n = glob.length;
  while (i < n) {
    const c = glob[i];
    if (c === "*") {
      if (glob[i + 1] === "*") {
        // "**" consumes slashes; an optional following "/" is folded in.
        if (glob[i + 2] === "/") {
          out += "(?:.*/)?";
          i += 3;
        } else {
          out += ".*";
          i += 2;
        }
      } else {
        out += "[^/]*";
        i += 1;
      }
    } else if (c === "?") {
      out += "[^/]";
      i += 1;
    } else if (c === "[") {
      const close = glob.indexOf("]", i + 1);
      if (close === -1) {
        out += "\\[";
        i += 1;
      } else {
        out += glob.slice(i, close + 1);
        i = close + 1;
      }
    } else {
      out += c.replace(/[.+^${}()|\\]/g, "\\$&");
      i += 1;
    }
  }
  return new RegExp(`^(?:${out})$`);
}

function matchesAny(patterns, rel) {
  return patterns.some((p) => {
    try {
      return globToRegExp(p).test(rel);
    } catch {
      return false;
    }
  });
}

// Canonicalize the session cwd once. Falls back to the normalized input
// when it cannot be resolved (fail-closed classification still applies).
function canonicalizeCwd(cwd) {
  try {
    return fs.realpathSync(cwd);
  } catch {
    return path.normalize(String(cwd || ""));
  }
}

// Canonicalize one configured external root. Returns the canonical path,
// or null when the root cannot be resolved (callers fail closed).
function canonicalizeRoot(rootPath) {
  try {
    const real = fs.realpathSync(rootPath);
    const stat = fs.statSync(real);
    if (!stat.isDirectory()) return null;
    return real;
  } catch {
    return null;
  }
}

// Canonicalize configured external roots for a session snapshot.
// Throws PolicyError when any root is unresolvable, not a directory, or a
// canonical duplicate of another root (conflicting or repeated definitions
// fail session creation rather than guess precedence). Overlapping
// (nested) roots are allowed: the most-specific match wins at evaluation.
export function canonicalizeExternalRoots(roots) {
  const list = Array.isArray(roots) ? roots : [];
  const canonical = [];
  const seen = new Map();
  for (let index = 0; index < list.length; index += 1) {
    const entry = list[index];
    const configured = typeof entry?.path === "string" ? entry.path : "";
    const real = canonicalizeRoot(configured);
    if (real === null) {
      throw new PolicyError(`external root '${configured.slice(0, 80)}' is unavailable`);
    }
    if (seen.has(real)) {
      throw new PolicyError(`external root '${configured.slice(0, 80)}' duplicates another root`);
    }
    seen.set(real, true);
    canonical.push({ path: real, mode: entry.mode });
  }
  return canonical;
}

// Canonicalize fixed control-plane roots (agent credential/config dir and
// similar). Best-effort nearest-existing-ancestor resolution: a missing
// tail does not void protection of the existing prefix.
export function canonicalizeControlRoots(dirs) {
  const out = [];
  for (const dir of Array.isArray(dirs) ? dirs : []) {
    if (typeof dir !== "string" || !dir) continue;
    const normalized = path.normalize(dir);
    const existing = [normalized];
    let cursor = normalized;
    for (;;) {
      if (fs.existsSync(cursor)) break;
      const parent = path.dirname(cursor);
      if (parent === cursor) break;
      cursor = parent;
      existing.push(cursor);
      if (existing.length > 64) break;
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
    const canon = path.normalize(rebuilt);
    if (canon && !out.includes(canon)) out.push(canon);
  }
  return out;
}

function isWithinOrEqual(parent, candidate) {
  if (candidate === parent) return true;
  const rel = path.relative(parent, candidate);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

// Resolve one requested path operand against the session cwd.
// Returns {ok, scope, rel, canonical}:
// - scope "workspace": rel is the workspace-relative path ("." for cwd).
// - scope "external": canonical is the exact absolute host target; the
//   caller applies external policy. Relative ".." escapes and symlink
//   escapes are external, never malformed solely for leaving the cwd.
// Failures carry a bounded code and fail closed.
function resolveOperand(cwdCanon, operand) {
  if (typeof operand !== "string" || !operand || operand.includes("\0")) {
    return { ok: false, code: "malformed_path" };
  }
  if (operand.length > 4096) return { ok: false, code: "malformed_path" };
  let absolute;
  if (path.isAbsolute(operand)) {
    absolute = path.normalize(operand);
  } else {
    absolute = path.normalize(path.join(cwdCanon, operand));
  }
  // Existing target: canonical realpath.
  let real = null;
  try {
    real = fs.realpathSync(absolute);
  } catch {
    real = null;
  }
  let canonical;
  if (real !== null) {
    canonical = real;
  } else {
    // New write target: nearest-existing-ancestor realpath + rebuild suffix.
    const parts = [];
    let cursor = absolute;
    for (;;) {
      if (fs.existsSync(cursor)) break;
      const parent = path.dirname(cursor);
      if (parent === cursor) return { ok: false, code: "malformed_path" };
      parts.unshift(path.basename(cursor));
      cursor = parent;
      if (parts.length > 64) return { ok: false, code: "malformed_path" };
    }
    let realBase;
    try {
      realBase = fs.realpathSync(cursor);
    } catch {
      return { ok: false, code: "malformed_path" };
    }
    canonical = parts.length ? path.join(realBase, ...parts) : realBase;
  }
  const rel = path.relative(cwdCanon, canonical);
  if (rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel))) {
    return {
      ok: true,
      scope: "workspace",
      rel: rel === "" ? "." : rel.split(path.sep).join("/"),
      canonical,
    };
  }
  return { ok: true, scope: "external", rel: "", canonical };
}

// Choose the external location mode for a canonical absolute target:
// the most-specific matching canonical root wins; otherwise the default.
function externalModeFor(canonicalTarget, effectiveRoots, defaultMode) {
  let best = null;
  for (const root of effectiveRoots) {
    if (isWithinOrEqual(root.path, canonicalTarget)) {
      if (best === null || root.path.length > best.path.length) best = root;
    }
  }
  return best ? best.mode : defaultMode;
}

// Compose per-tool mode with location mode: deny > ask > allow.
function composeModes(toolMode, locationMode) {
  return MODE_RANK[toolMode] >= MODE_RANK[locationMode] ? toolMode : locationMode;
}

// Build protected-pattern match candidates for a resolved target:
// - workspace: the workspace-relative resource (as before);
// - external under an override root: the root-relative path plus the
//   canonical absolute path with leading slash stripped;
// - external under the default: canonical absolute with leading slash stripped.
function protectedCandidates(resolved, effectiveRoots) {
  if (resolved.scope === "workspace") return [resolved.rel];
  const canonical = resolved.canonical;
  const stripped = canonical.startsWith("/") ? canonical.slice(1) : canonical;
  let best = null;
  for (const root of effectiveRoots) {
    if (isWithinOrEqual(root.path, canonical)) {
      if (best === null || root.path.length > best.path.length) best = root;
    }
  }
  if (best) {
    const rel = path.relative(best.path, canonical);
    const rootRel = (rel === "" ? "." : rel).split(path.sep).join("/");
    if (rootRel === stripped) return [stripped];
    return [rootRel, stripped];
  }
  return [stripped];
}

// Path operands per tool per the installed Pi schemas (verified 0.86.1):
// read {path}, grep {path?}, find {path?}, ls {path?}, edit {path},
// write {path}. Absent optional roots default to the session cwd.
function operandsFor(toolName, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  switch (toolName) {
    case "read":
    case "edit":
    case "write":
      return [input.path];
    case "grep":
    case "find":
    case "ls":
      return [input.path === undefined ? "." : input.path];
    default:
      return null;
  }
}

function boundedReason(text) {
  return String(text || "").slice(0, MAX_REASON_CHARS);
}

// Evaluate one validated tool call against the immutable session snapshot.
// Returns a bounded structured effect:
//   {effect: "allow"|"ask"|"deny", action, tool, resource, requested,
//    alwaysPattern, grantKey, reason, code, commandHash?, timeoutMs?}
//
// File tools:
// - resource: workspace-relative target (or "." for the cwd root) for
//   workspace targets; the exact canonical absolute host target for
//   external targets (so reviewers see what is actually requested).
// - requested: bounded list of requested targets in the same form.
// - alwaysPattern: exact scope ("<tool>:<target>") for ask; "" otherwise.
// - grantKey: internal exact grant key for ask; "" otherwise.
//
// Bash tool (Pi built-in bash only, no powershell, no command rules):
// - shell_mode deny: bash absent from --tools, deny here in depth.
// - shell_mode ask: bash present and each invocation uses the suspended-call
//   flow; permission detail carries the exact bounded command + verified
//   timeout; once approves the exact call; always stays session-local and
//   exact-command scoped (hash + timeout); reject blocks it.
// - shell_mode allow: bash present without prompt.
// Shell authority is independent of file/external policy. Allow runs with
// native macOS-user authority and can bypass structured file path controls.
//
// v3 has no fixed filesystem denies: selfProtectedDirs/controlDirs and any
// project-specific rules are ignored (ordinary configurable file/external
// policy applies). Configured protected_patterns remain ADMIN-CONFIGURED
// hard denies. Only protocol/session/policy integrity is hardcoded.
export function evaluateToolCall({
  cwd, policy, toolName, input,
  selfProtectedDirs = [], controlDirs = [],
  effectiveRoots = null,
}) {
  const tool = typeof toolName === "string" ? toolName : "";
  let snapshot;
  try {
    snapshot = validatePolicy(policy);
  } catch {
    return {
      effect: "deny", action: tool.slice(0, 40) || "unknown", tool: tool.slice(0, 40) || "unknown",
      resource: "", requested: [],
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Permission policy is invalid; failing closed"),
      code: "invalid_policy",
    };
  }
  // Bash is governed solely by shell_mode, independent of file policy.
  if (tool === SHELL_TOOL) {
    const extracted = extractBashCommand(input);
    if (!extracted.ok) {
      return {
        effect: "deny", action: tool, tool, resource: "", requested: [],
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Tool input is malformed"),
        code: "malformed_input",
      };
    }
    const mode = snapshot.shell_mode || DEFAULT_SHELL_MODE;
    const resource = extracted.command;
    const requested = [extracted.command];
    if (mode === "deny") {
      return {
        effect: "deny", action: tool, tool, resource, requested,
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Denied by shell policy"),
        code: "shell_deny",
      };
    }
    if (mode === "allow") {
      return {
        effect: "allow", action: tool, tool, resource, requested,
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Allowed by shell policy; runs with native user authority"),
        code: "shell_allow",
        commandHash: extracted.commandHash,
        timeoutMs: extracted.timeoutMs,
        commandTruncated: Boolean(extracted.truncated),
      };
    }
    // ask: exact-command scope via hash + verified timeout.
    const alwaysPattern = `${SHELL_TOOL}:${extracted.commandHash}:${extracted.timeoutMs}`;
    const grantKey = `${SHELL_TOOL}\n${extracted.commandHash}\n${extracted.timeoutMs}`;
    return {
      effect: "ask", action: tool, tool, resource, requested,
      alwaysPattern, grantKey,
      reason: boundedReason("Requires approval"),
      code: "shell_ask",
      commandHash: extracted.commandHash,
      timeoutMs: extracted.timeoutMs,
      commandTruncated: Boolean(extracted.truncated),
    };
  }
  if (!SUPPORTED_TOOLS.includes(tool)) {
    return {
      effect: "deny", action: "unknown", tool: tool.slice(0, 40) || "unknown",
      resource: "", requested: [], alwaysPattern: "", grantKey: "",
      reason: boundedReason("Tool is not enabled for this session"),
      code: "unknown_tool",
    };
  }
  const operands = operandsFor(tool, input);
  if (operands === null) {
    return {
      effect: "deny", action: tool, tool, resource: "", requested: [],
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Tool input is malformed"),
      code: "malformed_input",
    };
  }
  const cwdCanon = canonicalizeCwd(cwd);
  // Effective canonical external roots: prefer caller-supplied session
  // snapshot roots (adapter), else canonicalize the policy roots
  // best-effort (trusted extension). Unresolvable roots fail closed.
  let roots;
  try {
    if (effectiveRoots !== null && effectiveRoots !== undefined) {
      if (!Array.isArray(effectiveRoots)) throw new PolicyError("effective roots are invalid");
      roots = effectiveRoots.map((entry, index) => {
        if (!entry || typeof entry.path !== "string" || !POLICY_MODES.includes(entry.mode)) {
          throw new PolicyError(`effective root #${index} is invalid`);
        }
        return { path: entry.path, mode: entry.mode };
      });
    } else {
      roots = canonicalizeExternalRoots(snapshot.external_access.roots);
    }
  } catch {
    return {
      effect: "deny", action: tool, tool, resource: "", requested: [],
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("External policy roots are unavailable; failing closed"),
      code: "invalid_roots",
    };
  }
  // v3: no fixed filesystem denies. selfProtectedDirs/controlDirs and any
  // project-specific rules are ignored; ordinary configurable file/external
  // policy applies. Retained params are ignored for backward compatibility.
  void selfProtectedDirs;
  void controlDirs;
  void canonicalizeControlRoots;
  // Resolve every relevant operand; any failure denies the whole call.
  const resolved = [];
  for (const operand of operands) {
    const result = resolveOperand(cwdCanon, operand);
    if (!result.ok) {
      return {
        effect: "deny", action: tool, tool, resource: "", requested: [],
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Tool input is malformed"),
        code: result.code,
      };
    }
    resolved.push(result);
  }
  const primary = resolved[0];
  const isExternal = primary.scope === "external";
  const resource = isExternal ? primary.canonical : primary.rel;
  const requested = resolved.map((r) => (r.scope === "external" ? r.canonical : r.rel)).slice(0, 8);

  // Configured protected patterns are a hard deny and are never remotely
  // approvable. Workspace targets match the workspace-relative resource;
  // external targets match root-relative/absolute candidates above.
  const candidates = protectedCandidates(primary, roots);
  const protectedHit = candidates.some((c) => matchesAny(snapshot.protected_patterns, c))
    && !candidates.some((c) => matchesAny(snapshot.protected_template_exceptions, c));
  if (protectedHit) {
    return {
      effect: "deny", action: tool, tool, resource, requested,
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Target matches a protected pattern"),
      code: "protected_pattern",
    };
  }

  const toolMode = snapshot.tools[tool];
  const locationMode = isExternal
    ? externalModeFor(primary.canonical, roots, snapshot.external_access.default_mode)
    : "allow";
  const finalMode = composeModes(toolMode, locationMode);
  if (finalMode === "allow") {
    return {
      effect: "allow", action: tool, tool, resource, requested,
      alwaysPattern: "", grantKey: "",
      reason: boundedReason(isExternal
        ? "Allowed by permission policy"
        : "Allowed by permission policy"),
      code: isExternal ? "external_allow" : "tool_allow",
    };
  }
  if (finalMode === "deny") {
    const external = isExternal && toolMode !== "deny";
    return {
      effect: "deny", action: tool, tool, resource, requested,
      alwaysPattern: "", grantKey: "",
      reason: boundedReason(external
        ? "Denied by external access policy"
        : "Denied by permission policy"),
      code: external ? "external_deny" : "tool_deny",
    };
  }
  // ask: exact scope is action + exact target (workspace-relative for
  // workspace targets, canonical absolute for external targets), never a
  // wildcard/root/sibling/other tool.
  const alwaysPattern = `${tool}:${resource}`;
  const grantKey = `${tool}\n${resource}`;
  return {
    effect: "ask", action: tool, tool, resource, requested,
    alwaysPattern, grantKey,
    reason: boundedReason("Requires approval"),
    code: "tool_ask",
  };
}

// Directory containing this package-owned module. Retained for
// fingerprint/identity diagnostics only; v3 performs no fixed filesystem
// deny on it (ordinary configurable file/external policy applies).
export function selfProtectionDir() {
  return path.dirname(fileURLToPath(import.meta.url));
}
