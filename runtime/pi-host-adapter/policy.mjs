// Package-owned Pi file-tool permission policy (milestone 3B1).
//
// Single shared evaluator used by the trusted permission extension and by
// the adapter's RPC correlation layer. The Bridge web manager owns the
// *operational* policy (master enable, per-tool allow/ask/deny, protected
// workspace-relative globs, session-always availability). Everything else
// here is a non-configurable protocol/security invariant:
//
// - exact session + toolCallId + UI-request correlation (adapter layer);
// - canonical/symlink-aware confinement to the exact mapped workspace cwd;
// - outside-workspace access always denied;
// - malformed/unknown tool input fails closed;
// - package-owned permission implementation paths always deny edit/write;
// - no bash; no project/global extension discovery; no approval persistence.
//
// Defense in depth: the adapter validates Bridge-delivered policy AGAIN with
// validatePolicy() before storing a session snapshot; the extension
// re-validates the snapshot it receives via environment. Invalid policy
// fails closed to the read-only safe default.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const POLICY_VERSION = 1;
export const SUPPORTED_TOOLS = ["read", "grep", "find", "ls", "edit", "write"];
export const POLICY_MODES = ["allow", "ask", "deny"];
export const MAX_PROTECTED_PATTERNS = 64;
export const MAX_PATTERN_CHARS = 400;
export const MAX_POLICY_BYTES = 32768;
export const MAX_REASON_CHARS = 200;

// Fixed safety invariants shown read-only in the web manager.
export const FIXED_INVARIANTS = [
  "Exact session + toolCallId + UI-request correlation",
  "Canonical/symlink-aware confinement to the mapped workspace",
  "Outside-workspace access denied",
  "Malformed or unknown tool input denied",
  "Permission implementation is self-protected from edit/write",
  "Trusted extension path is package-owned, never admin/project/env-selectable",
  "No bash tool",
  "No project or global extension discovery",
  "Approvals are session-local and never persisted to disk",
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
    enabled: false,
    tools: {
      read: "allow",
      grep: "allow",
      find: "allow",
      ls: "allow",
      edit: "ask",
      write: "ask",
    },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
  };
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

// Strictly validate a full v1 policy object. Returns the canonical policy.
// Throws PolicyError on any violation; invalid policy never partially applies.
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
    "version", "enabled", "tools", "protected_patterns",
    "protected_template_exceptions", "allow_session_always",
  ]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new PolicyError("policy has unknown fields");
  }
  if (value.version !== POLICY_VERSION) {
    throw new PolicyError(`policy version must be ${POLICY_VERSION}`);
  }
  if (typeof value.enabled !== "boolean") {
    throw new PolicyError("policy 'enabled' must be a boolean");
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
  return {
    version: POLICY_VERSION,
    enabled: value.enabled,
    tools: Object.fromEntries(SUPPORTED_TOOLS.map((t) => [t, tools[t]])),
    protected_patterns: protectedPatterns,
    protected_template_exceptions: exceptions,
    allow_session_always: value.allow_session_always,
  };
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

// Resolve one requested path operand against the session cwd.
// Returns {ok, rel, canonical} where rel is the workspace-relative path
// (forward slashes, "." for cwd itself). Failures carry a bounded code.
function resolveOperand(cwd, operand) {
  if (typeof operand !== "string" || !operand || operand.includes("\0")) {
    return { ok: false, code: "malformed_path" };
  }
  if (operand.length > 4096) return { ok: false, code: "malformed_path" };
  let absolute;
  if (path.isAbsolute(operand)) {
    absolute = path.normalize(operand);
  } else {
    absolute = path.normalize(path.join(cwd, operand));
  }
  // Existing target: canonical realpath.
  let real = null;
  try {
    real = fs.realpathSync(absolute);
  } catch {
    real = null;
  }
  if (real !== null) {
    const rel = path.relative(cwd, real);
    if (rel === "") return { ok: true, rel: ".", canonical: real };
    if (rel.startsWith("..") || path.isAbsolute(rel)) {
      // absolute input that canonicalizes outside, or a symlink escape.
      return { ok: false, code: "outside_workspace" };
    }
    return { ok: true, rel: rel.split(path.sep).join("/"), canonical: real };
  }
  // New write target: nearest-existing-ancestor realpath + rebuild suffix.
  const parts = [];
  let cursor = absolute;
  for (;;) {
    if (fs.existsSync(cursor)) break;
    const parent = path.dirname(cursor);
    if (parent === cursor) return { ok: false, code: "outside_workspace" };
    parts.unshift(path.basename(cursor));
    cursor = parent;
    if (parts.length > 64) return { ok: false, code: "malformed_path" };
  }
  let realBase;
  try {
    realBase = fs.realpathSync(cursor);
  } catch {
    return { ok: false, code: "outside_workspace" };
  }
  const rebuilt = parts.length ? path.join(realBase, ...parts) : realBase;
  const rel = path.relative(cwd, rebuilt);
  if (rel === "") return { ok: true, rel: ".", canonical: rebuilt };
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    return { ok: false, code: "outside_workspace" };
  }
  return { ok: true, rel: rel.split(path.sep).join("/"), canonical: rebuilt };
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
//    alwaysPattern, grantKey, reason, code}
//
// - resource: workspace-relative public target (or "." for the cwd root).
// - requested: bounded list of requested workspace-relative targets.
// - alwaysPattern: exact scope ("<tool>:<rel>") for ask; "" otherwise.
// - grantKey: internal exact grant key for ask; "" otherwise.
// - reason/code: bounded machine-readable explanation.
export function evaluateToolCall({ cwd, policy, toolName, input, selfProtectedDirs = [] }) {
  const tool = typeof toolName === "string" ? toolName : "";
  if (!SUPPORTED_TOOLS.includes(tool)) {
    return {
      effect: "deny", action: "unknown", tool: tool.slice(0, 40) || "unknown",
      resource: "", requested: [], alwaysPattern: "", grantKey: "",
      reason: boundedReason("Tool is not enabled for this session"),
      code: "unknown_tool",
    };
  }
  let snapshot;
  try {
    snapshot = validatePolicy(policy);
  } catch {
    return {
      effect: "deny", action: tool, tool, resource: "", requested: [],
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Permission policy is invalid; failing closed"),
      code: "invalid_policy",
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
  // Resolve every relevant operand; any failure denies the whole call.
  const resolved = [];
  for (const operand of operands) {
    const result = resolveOperand(cwd, operand);
    if (!result.ok) {
      return {
        effect: "deny", action: tool, tool, resource: "", requested: [],
        alwaysPattern: "", grantKey: "",
        reason: boundedReason(result.code === "outside_workspace"
          ? "Target is outside the mapped workspace"
          : "Tool input is malformed"),
        code: result.code,
      };
    }
    resolved.push(result);
  }
  const primary = resolved[0];
  const resource = primary.rel;
  const requested = resolved.map((r) => r.rel).slice(0, 8);

  const isWrite = tool === "edit" || tool === "write";
  if (isWrite) {
    // Non-configurable fixed self-protection: the package-owned
    // permission implementation (trusted extension + policy module can
    // never edit/write themselves.
    for (const dir of selfProtectedDirs) {
      if (typeof dir !== "string" || !dir) continue;
      const canon = primary.canonical;
      if (canon === dir || canon.startsWith(dir + path.sep)) {
        return {
          effect: "deny", action: tool, tool, resource, requested,
          alwaysPattern: "", grantKey: "",
          reason: boundedReason("Target is protected permission implementation"),
          code: "self_protected",
        };
      }
    }
    // Configured protected patterns (with validated template exceptions)
    // are a hard deny and are never remotely approvable.
    if (matchesAny(snapshot.protected_patterns, resource)
        && !matchesAny(snapshot.protected_template_exceptions, resource)) {
      return {
        effect: "deny", action: tool, tool, resource, requested,
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Target matches a protected pattern"),
        code: "protected_pattern",
      };
    }
  } else {
    // Read-family tools honor configured protected patterns too when the
    // admin configures them: outside-workspace/symlink escapes already
    // denied above; protected matches stay a hard deny.
    if (matchesAny(snapshot.protected_patterns, resource)
        && !matchesAny(snapshot.protected_template_exceptions, resource)) {
      return {
        effect: "deny", action: tool, tool, resource, requested,
        alwaysPattern: "", grantKey: "",
        reason: boundedReason("Target matches a protected pattern"),
        code: "protected_pattern",
      };
    }
  }

  const mode = snapshot.tools[tool];
  if (mode === "allow") {
    return {
      effect: "allow", action: tool, tool, resource, requested,
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Allowed by permission policy"),
      code: "tool_allow",
    };
  }
  if (mode === "deny") {
    return {
      effect: "deny", action: tool, tool, resource, requested,
      alwaysPattern: "", grantKey: "",
      reason: boundedReason("Denied by permission policy"),
      code: "tool_deny",
    };
  }
  // ask: exact scope is action + exact canonical workspace-relative
  // target, never a wildcard/sibling/other tool.
  const alwaysPattern = `${tool}:${resource}`;
  const grantKey = `${tool}\n${resource}`;
  return {
    effect: "ask", action: tool, tool, resource, requested,
    alwaysPattern, grantKey,
    reason: boundedReason("Requires approval"),
    code: "tool_ask",
  };
}

// Directory containing this package-owned module: the non-configurable
// self-protection root for edit/write.
export function selfProtectionDir() {
  return path.dirname(new URL(import.meta.url).pathname);
}
