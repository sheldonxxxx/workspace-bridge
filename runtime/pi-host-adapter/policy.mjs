// Bridge security profile rules for Pi.
//
// The JavaScript twin of workspace_bridge/security_profiles.py: the same
// rule validation, revision, path and command evaluation. Both are checked
// against tests/fixtures/profile_vectors.json, so a change here needs the
// same change there.
//
// Each run carries its profile's rules from the Bridge. The adapter
// validates them again and pins them to the Pi session; the package-owned
// trusted permission extension then judges every built-in tool call with
// evaluateToolCall() before it runs. Pi itself has no sandbox: this is a
// pre-tool check, not OS containment, and a shell command that the rules
// allow runs with the user's own authority.
//
// Evaluation order, most specific first: the fixed protected paths (.git,
// .env files, the workspace's checks.toml) and the profile's `protected`
// globs, then `commands` (bash) and `external_roots` (paths outside the
// workspace), then the category mode. Malformed input fails closed.
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const MODES = ["deny", "ask", "allow"];
export const CATEGORIES = ["read_inside", "read_outside", "write_inside", "write_outside",
  "shell", "web", "extensions"];
export const LIST_KEYS = ["protected", "external_roots", "commands"];
export const KEYS = [...CATEGORIES, ...LIST_KEYS];
export const PROFILE_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/;
export const REVISION_RE = /^prof_[0-9a-f]{32}$/;
export const READ_TOOLS = ["read", "grep", "find", "ls"];
export const WRITE_TOOLS = ["edit", "write"];
export const SUPPORTED_TOOLS = [...READ_TOOLS, ...WRITE_TOOLS];
export const SHELL_TOOL = "bash";
export const MAX_PATTERNS = 64;
export const MAX_PATTERN_CHARS = 400;
export const MAX_ROOTS = 32;
export const MAX_ROOT_CHARS = 1024;
export const MAX_COMMANDS = 64;
export const MAX_COMMAND_CHARS = 200;
export const MAX_SHELL_COMMAND_CHARS = 16384;
export const MAX_REASON_CHARS = 200;

const RANK = { allow: 0, ask: 1, deny: 2 };
const TEMPLATE_SUFFIXES = [".example", ".sample", ".template"];
const PATTERN_CHARS = /^[A-Za-z0-9_.\-/*?[\]!+@#%^()=:,{} ]+$/;
const SHELL_SPLIT = /&&|\|\||[;&|\n()`]|\$\(/;
const ENV_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=[^]*$/;
// `zsh -lc '<script>'` and the like: a shell running a script given inline.
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh"]);
const SCRIPT_FLAG = /^-[A-Za-z]*c[A-Za-z]*$/;
const MAX_SHELL_NESTING = 4;
// Python's str.split()/strip() whitespace, so both sides cut text alike.
const PY_SPACE = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_SPACES = new RegExp(`[${PY_SPACE}]+`);
const PY_TRIM = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");

export const BUILTINS = {
  "read-only": {
    read_inside: "allow", read_outside: "deny",
    write_inside: "deny", write_outside: "deny",
    shell: "deny", web: "deny", extensions: "deny",
    protected: [], external_roots: [], commands: [],
  },
  reviewed: {
    read_inside: "allow", read_outside: "ask",
    write_inside: "allow", write_outside: "deny",
    shell: "ask", web: "ask", extensions: "ask",
    protected: [], external_roots: [], commands: [],
  },
  trusted: {
    read_inside: "allow", read_outside: "allow",
    write_inside: "allow", write_outside: "ask",
    shell: "allow", web: "allow", extensions: "allow",
    protected: [], external_roots: [], commands: [],
  },
};

export class PolicyError extends Error {
  constructor(message) {
    super(message);
    this.name = "PolicyError";
  }
}

const pyStrip = (text) => text.replace(PY_TRIM, "");
const pyWords = (text) => text.split(PY_SPACES).filter(Boolean);
const codePoints = (text) => [...text].length;

function checkMode(value, where) {
  if (!MODES.includes(value)) throw new PolicyError(`${where} must be deny, ask or allow`);
  return value;
}

function checkPattern(value) {
  if (typeof value !== "string" || value.includes("\0")) {
    throw new PolicyError("protected entries must be text");
  }
  const text = pyStrip(value).replace(/\\/g, "/");
  if (!text || codePoints(text) > MAX_PATTERN_CHARS) {
    throw new PolicyError(`protected entries must be 1 to ${MAX_PATTERN_CHARS} characters`);
  }
  if (text.startsWith("/") || text.startsWith("~")) {
    throw new PolicyError("protected entries are workspace-relative globs");
  }
  if (text.split("/").includes("..")) throw new PolicyError("protected entries must not contain '..'");
  if (!PATTERN_CHARS.test(text)) throw new PolicyError("protected entry uses unsupported characters");
  return text;
}

function checkRoot(value, index) {
  const where = `external root ${index + 1}`;
  const keys = value && typeof value === "object" && !Array.isArray(value) ? Object.keys(value) : null;
  if (!keys || !keys.includes("path") || keys.some((key) => !["path", "read", "write"].includes(key))) {
    throw new PolicyError(`${where} needs path and optional read and write`);
  }
  if (typeof value.path !== "string" || value.path.includes("\0")) {
    throw new PolicyError(`${where} path must be text`);
  }
  const text = pyStrip(value.path);
  if (!text || codePoints(text) > MAX_ROOT_CHARS) {
    throw new PolicyError(`${where} path must be 1 to ${MAX_ROOT_CHARS} characters`);
  }
  if (!(text.startsWith("/") || text === "~" || text.startsWith("~/"))) {
    throw new PolicyError(`${where} path must be absolute or start with ~/`);
  }
  if (text.split("/").includes("..") || text.includes("//") || (text.endsWith("/") && text !== "/")) {
    throw new PolicyError(`${where} path is not normalized`);
  }
  const root = { path: text };
  for (const key of ["read", "write"]) {
    if (key in value) root[key] = checkMode(value[key], `${where} ${key}`);
  }
  return root;
}

function checkCommand(value, index) {
  const where = `command rule ${index + 1}`;
  const keys = value && typeof value === "object" && !Array.isArray(value) ? Object.keys(value) : null;
  if (!keys || keys.length !== 2 || !keys.includes("prefix") || !keys.includes("mode")) {
    throw new PolicyError(`${where} needs prefix and mode`);
  }
  if (typeof value.prefix !== "string" || value.prefix.includes("\0") || value.prefix.includes("\n")) {
    throw new PolicyError(`${where} prefix must be one line of text`);
  }
  const prefix = pyWords(value.prefix).join(" ");
  if (!prefix || codePoints(prefix) > MAX_COMMAND_CHARS) {
    throw new PolicyError(`${where} prefix must be 1 to ${MAX_COMMAND_CHARS} characters`);
  }
  return { prefix, mode: checkMode(value.mode, `${where} mode`) };
}

// Python json.dumps() with its default separators and ASCII escaping, for
// the same size limit on both sides.
function pyDumps(value) {
  if (Array.isArray(value)) return `[${value.map(pyDumps).join(", ")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value).map(([key, item]) => `${pyDumps(key)}: ${pyDumps(item)}`).join(", ")}}`;
  }
  return JSON.stringify(value).replace(/[\u007f-￿]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

// Return the canonical rules for one profile; throws PolicyError.
export function validateRules(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new PolicyError("Profile rules must be an object");
  }
  const unknown = Object.keys(raw).filter((key) => !KEYS.includes(key)).sort();
  if (unknown.length) throw new PolicyError(`Unknown profile keys: ${unknown.join(", ")}`);
  const missing = CATEGORIES.filter((key) => !(key in raw));
  if (missing.length) throw new PolicyError(`Profile needs ${missing.join(", ")}`);
  const rules = {};
  for (const key of CATEGORIES) rules[key] = checkMode(raw[key], key);
  const protectedList = raw.protected ?? [];
  if (!Array.isArray(protectedList) || protectedList.length > MAX_PATTERNS) {
    throw new PolicyError(`protected must be a list of at most ${MAX_PATTERNS} globs`);
  }
  rules.protected = [...new Set(protectedList.map(checkPattern))].sort();
  const roots = raw.external_roots ?? [];
  if (!Array.isArray(roots) || roots.length > MAX_ROOTS) {
    throw new PolicyError(`external_roots must be a list of at most ${MAX_ROOTS} roots`);
  }
  rules.external_roots = roots.map(checkRoot);
  if (new Set(rules.external_roots.map((root) => root.path)).size !== rules.external_roots.length) {
    throw new PolicyError("external_roots repeats a path");
  }
  const commands = raw.commands ?? [];
  if (!Array.isArray(commands) || commands.length > MAX_COMMANDS) {
    throw new PolicyError(`commands must be a list of at most ${MAX_COMMANDS} rules`);
  }
  rules.commands = commands.map(checkCommand);
  if (new Set(rules.commands.map((rule) => rule.prefix)).size !== rules.commands.length) {
    throw new PolicyError("commands repeats a prefix");
  }
  if (pyDumps(rules).length > 32768) throw new PolicyError("Profile is too large");
  return rules;
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

// Sorted keys, no spaces, UTF-8 text: equal to the Bridge's canonical_json.
export function canonicalJson(rules) {
  return JSON.stringify(sortKeysDeep(rules));
}

export function profileRevision(rules) {
  return `prof_${createHash("sha256").update(canonicalJson(rules), "utf8").digest("hex").slice(0, 32)}`;
}

// Validate a run's security block: {source:"profile", profile:{id, revision, rules}}.
export function fromWire(value) {
  if (!value || typeof value !== "object" || value.source !== "profile") {
    throw new PolicyError("Run security must name a profile");
  }
  const profile = value.profile;
  if (!profile || typeof profile !== "object" || typeof profile.id !== "string"
      || !PROFILE_ID_RE.test(profile.id)) {
    throw new PolicyError("Run security profile is invalid");
  }
  const rules = validateRules(profile.rules);
  if (profile.revision !== profileRevision(rules)) {
    throw new PolicyError("Run security profile revision does not match its rules");
  }
  return { id: profile.id, revision: profile.revision, rules };
}

export function stricter(...modes) {
  return modes.reduce((worst, mode) => (RANK[mode] > RANK[worst] ? mode : worst));
}

function escapeRegExp(char) {
  return /[.*+?^${}()|[\]\\/-]/.test(char) ? `\\${char}` : char;
}

function globRegExp(pattern) {
  let out = "";
  let index = 0;
  while (index < pattern.length) {
    const char = pattern[index];
    if (char === "*") {
      if (pattern.startsWith("**/", index)) {
        out += "(?:.*/)?";
        index += 3;
      } else if (pattern.startsWith("**", index)) {
        out += ".*";
        index += 2;
      } else {
        out += "[^/]*";
        index += 1;
      }
    } else if (char === "?") {
      out += "[^/]";
      index += 1;
    } else if (char === "[") {
      const close = pattern.indexOf("]", index + 1);
      if (close === -1) {
        out += escapeRegExp(char);
        index += 1;
      } else {
        let body = pattern.slice(index + 1, close);
        if (body.startsWith("!")) body = `^${body.slice(1)}`;
        out += `[${body.replace(/\\/g, "\\\\")}]`;
        index = close + 1;
      }
    } else {
      out += escapeRegExp(char);
      index += 1;
    }
  }
  return new RegExp(`^(?:${out})$`);
}

// Whether a workspace-relative path, or a folder containing it, matches.
// A pattern without "/" matches a name at any depth, as in .gitignore; a
// pattern with "/" is anchored at the workspace root.
export function globMatches(pattern, relative) {
  const parts = relative.split("/").filter((part) => part && part !== ".");
  if (!parts.length) return false;
  const trimmed = pattern.replace(/\/+$/, "");
  let regex;
  try {
    regex = globRegExp(trimmed);
  } catch {
    return true;
  }
  if (!trimmed.includes("/")) return parts.some((part) => regex.test(part));
  return parts.some((_, end) => regex.test(parts.slice(0, end + 1).join("/")));
}

// Paths no profile can open: Git internals, env files and checks.toml.
export function fixedProtected(relative) {
  const parts = relative.replace(/\\/g, "/").split("/").filter((part) => part && part !== ".");
  if (parts.includes(".git")) return "Git internals are protected";
  if (parts.some((part) => (part === ".env" || part.startsWith(".env."))
      && !TEMPLATE_SUFFIXES.some((suffix) => part.endsWith(suffix)))) {
    return "Environment files are protected";
  }
  if (parts.length === 1 && parts[0] === "checks.toml") return "checks.toml is protected";
  return null;
}

// POSIX realpath that, like Python's os.path.realpath, keeps missing
// components and applies ".." after the symlinks before it resolve.
export function realpathLoose(absolute) {
  const pending = absolute.split("/").filter(Boolean);
  let current = "/";
  let links = 0;
  while (pending.length) {
    const part = pending.shift();
    if (part === ".") continue;
    if (part === "..") {
      current = path.dirname(current);
      continue;
    }
    const next = current === "/" ? `/${part}` : `${current}/${part}`;
    let stat = null;
    try {
      stat = fs.lstatSync(next);
    } catch {
      stat = null;
    }
    if (stat && stat.isSymbolicLink()) {
      links += 1;
      if (links > 40) throw new PolicyError("Too many symbolic links");
      const target = fs.readlinkSync(next);
      pending.unshift(...target.split("/").filter(Boolean));
      if (target.startsWith("/")) current = "/";
      continue;
    }
    current = next;
  }
  return current;
}

function expandHome(text) {
  if (text === "~") return os.homedir();
  if (text.startsWith("~/")) return path.join(os.homedir(), text.slice(2));
  return text;
}

// Absolute real path of one tool path, or null when it is invalid. Only
// "~" and "~/" expand; another user's "~name" is refused on both sides.
export function resolvePath(root, raw) {
  if (typeof raw !== "string" || !raw || codePoints(raw) > 4096 || raw.includes("\0")) return null;
  if (raw.startsWith("~") && raw !== "~" && !raw.startsWith("~/")) return null;
  const expanded = expandHome(raw);
  const candidate = path.isAbsolute(expanded) ? expanded : `${root}/${expanded}`;
  try {
    return realpathLoose(candidate);
  } catch {
    return null;
  }
}

function inside(root, candidate) {
  return candidate === root || candidate.startsWith(`${root.replace(/\/+$/, "")}/`);
}

function classifyPath(rules, root, raw, write) {
  let rootReal;
  try {
    rootReal = realpathLoose(path.resolve(root));
  } catch {
    return { mode: "deny", reason: "Workspace directory is unavailable" };
  }
  const resolved = resolvePath(rootReal, raw);
  if (resolved === null) return { mode: "deny", reason: "Tool path is invalid" };
  const kind = write ? "write" : "read";
  if (inside(rootReal, resolved)) {
    const relative = resolved === rootReal ? "" : resolved.slice(rootReal.replace(/\/+$/, "").length + 1);
    const base = { scope: "workspace", resource: relative || ".", canonical: resolved };
    const fixed = fixedProtected(relative);
    if (fixed) return { ...base, mode: "deny", reason: fixed, code: "protected_path" };
    if (rules.protected.some((pattern) => globMatches(pattern, relative))) {
      return { ...base, mode: "deny", reason: "Path is protected by this security profile",
        code: "protected_path" };
    }
    const mode = rules[`${kind}_inside`];
    return { ...base, mode,
      reason: mode !== "deny" ? "" : `Workspace ${kind}s are disabled by this security profile` };
  }
  const base = { scope: "external", resource: resolved, canonical: resolved };
  if (fixedProtected(resolved)) {
    return { ...base, mode: "deny", reason: "Path is protected", code: "protected_path" };
  }
  let best = null;
  for (const entry of rules.external_roots) {
    let real = "";
    try {
      real = realpathLoose(path.resolve(expandHome(entry.path)));
    } catch {
      real = "";
    }
    if (real && inside(real, resolved) && (best === null || real.length > best.real.length)) {
      best = { real, entry };
    }
  }
  const mode = best?.entry[kind] ?? rules[`${kind}_outside`];
  return { ...base, mode,
    reason: mode !== "deny" ? "" : `Outside ${kind}s are disabled by this security profile` };
}

// [mode, reason] for reading or writing one path; never throws.
export function pathMode(rules, root, raw, { write }) {
  const result = classifyPath(rules, root, raw, write);
  return [result.mode, result.reason];
}

function matchesPrefix(segment, prefix) {
  return segment === prefix || segment.startsWith(`${prefix} `);
}

// POSIX-style words of `text`, or null when its quoting is unbalanced.
function shellWords(text) {
  const chars = Array.from(text);
  const words = [];
  let word = [];
  let quote = "";
  let started = false;
  for (let index = 0; index < chars.length; index += 1) {
    const char = chars[index];
    if (quote === "'") {
      if (char === "'") quote = "";
      else word.push(char);
    } else if (quote === '"') {
      if (char === '"') quote = "";
      else if (char === "\\" && index + 1 < chars.length && '"\\$`'.includes(chars[index + 1])) {
        index += 1;
        word.push(chars[index]);
      } else word.push(char);
    } else if (char === "'" || char === '"') {
      quote = char;
      started = true;
    } else if (char === "\\") {
      if (index + 1 === chars.length) return null;
      index += 1;
      word.push(chars[index]);
    } else if (char === " " || char === "\t" || char === "\n") {
      if (word.length || started) words.push(word.join(""));
      word = [];
      started = false;
    } else word.push(char);
  }
  if (quote) return null;
  if (word.length || started) words.push(word.join(""));
  return words;
}

function runsScript(words) {
  return words.length >= 2 && SHELLS.has(words[0].split("/").pop()) && SCRIPT_FLAG.test(words[1]);
}

// [mode, reason] for one shell command; never throws.
//
// A deny or ask rule applies when its prefix starts any simple command in
// the line, so `cd x && git push` still meets a `git push` rule. An allow
// rule applies only when every simple command in the line has an allow
// rule; otherwise the shell mode decides. A line that is only a shell
// running an inline script, such as `zsh -lc 'git push'`, is judged by its
// script; a script nested deeper hides its commands, so it asks at least
// whenever the profile has ask or deny command rules.
export function commandMode(rules, command, depth = 0) {
  if (typeof command !== "string" || !pyStrip(command)
      || codePoints(command) > MAX_SHELL_COMMAND_CHARS || command.includes("\0")) {
    return ["deny", "Shell command is missing or too long"];
  }
  const whole = shellWords(command);
  if (whole !== null && whole.length === 3 && runsScript(whole) && depth < MAX_SHELL_NESTING) {
    return commandMode(rules, whole[2], depth + 1);
  }
  const guarded = rules.commands.some((rule) => rule.mode !== "allow");
  const segments = command.split(SHELL_SPLIT).map((part) => pyWords(part).join(" ")).filter(Boolean);
  let restrictive = "allow";
  let covered = segments.length > 0;
  for (const raw of segments) {
    const words = raw.split(" ");
    // Leading environment assignments do not change which program runs.
    while (words.length && ENV_ASSIGNMENT.test(words[0])) words.shift();
    if (guarded && runsScript(words)) {
      restrictive = stricter(restrictive, "ask");
      covered = false;
      continue;
    }
    const segment = words.join(" ");
    let best = null;
    for (const rule of rules.commands) {
      if (matchesPrefix(segment, rule.prefix)
          && (best === null || codePoints(rule.prefix) > codePoints(best.prefix))) {
        best = rule;
      }
    }
    if (best === null) {
      covered = false;
      continue;
    }
    if (best.mode === "allow") continue;
    restrictive = stricter(restrictive, best.mode);
  }
  if (restrictive === "deny") return ["deny", "Command is denied by this security profile"];
  if (covered) return [restrictive, ""];
  // Simple commands without a rule fall to the shell mode.
  if (rules.shell === "deny") return ["deny", "Shell is disabled by this security profile"];
  return [stricter(restrictive, rules.shell), ""];
}

// Built-in tool exposure for a session: tools the rules can never allow
// stay out of the model's tool list.
export function toolExposure(rules) {
  const writable = rules.write_inside !== "deny" || rules.write_outside !== "deny"
    || rules.external_roots.some((root) => root.write !== undefined && root.write !== "deny");
  const shell = rules.shell !== "deny" || rules.commands.some((rule) => rule.mode !== "deny");
  return { writable, shellMode: shell ? (rules.shell === "deny" ? "ask" : rules.shell) : "deny" };
}

// Extract the exact bounded bash command + verified timeout from tool input.
// Verified against installed Pi 0.86.1 bashSchema: {command, timeout?}
// where timeout is SECONDS (optional, no default). Millisecond aliases
// (timeoutMs/timeout_ms) are accepted from scripted fakes. Returns
// {ok, command, raw, commandHash, timeoutMs} or {ok:false}. Never throws;
// unknown shapes fail closed without dumping args.
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
  return { ok: true, command, raw, truncated, commandHash, timeoutMs };
}

function boundedReason(text) {
  return String(text || "").slice(0, MAX_REASON_CHARS);
}

function verdict(effect, tool, fields) {
  return { effect, action: tool, tool, resource: "", requested: [], alwaysPattern: "",
    grantKey: "", ...fields, reason: boundedReason(fields.reason) };
}

// Path operands per tool per the installed Pi schemas (verified 0.86.1):
// read {path}, grep {path?}, find {path?}, ls {path?}, edit {path},
// write {path}. Absent optional roots default to the session cwd.
function operandFor(toolName, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  if (READ_TOOLS.includes(toolName) && toolName !== "read") {
    return input.path === undefined ? "." : input.path;
  }
  return input.path;
}

// Evaluate one built-in tool call against a session's rules. Returns
//   {effect: "allow"|"ask"|"deny", action, tool, resource, requested,
//    alwaysPattern, grantKey, reason, code, commandHash?, timeoutMs?}
// where resource is the workspace-relative target ("." for the cwd) or
// the canonical absolute path outside it, or the bounded bash command.
// For ask, grantKey scopes a session approval to the exact target (file
// tools) or the exact command hash and timeout (bash).
export function evaluateToolCall({ cwd, policy, toolName, input }) {
  const tool = typeof toolName === "string" ? toolName.slice(0, 40) : "";
  let rules;
  try {
    rules = validateRules(policy);
  } catch {
    return verdict("deny", tool || "unknown", { reason: "Security profile is invalid; failing closed",
      code: "invalid_policy" });
  }
  if (tool === SHELL_TOOL) {
    const extracted = extractBashCommand(input);
    if (!extracted.ok) {
      return verdict("deny", tool, { reason: "Tool input is malformed", code: "malformed_input" });
    }
    const [mode, reason] = commandMode(rules, extracted.raw);
    const fields = { resource: extracted.command, requested: [extracted.command],
      commandHash: extracted.commandHash, timeoutMs: extracted.timeoutMs,
      commandTruncated: Boolean(extracted.truncated) };
    if (mode === "deny") {
      return verdict("deny", tool, { resource: fields.resource, requested: fields.requested,
        reason: reason || "Denied by this security profile", code: "shell_deny" });
    }
    if (mode === "allow") {
      return verdict("allow", tool, { ...fields,
        reason: "Allowed by this security profile; runs with native user authority",
        code: "shell_allow" });
    }
    return verdict("ask", tool, { ...fields,
      alwaysPattern: `${SHELL_TOOL}:${extracted.commandHash}:${extracted.timeoutMs}`,
      grantKey: `${SHELL_TOOL}\n${extracted.commandHash}\n${extracted.timeoutMs}`,
      reason: "Requires approval", code: "shell_ask" });
  }
  if (!SUPPORTED_TOOLS.includes(tool)) {
    return { ...verdict("deny", "unknown", { reason: "Tool is not enabled for this session",
      code: "unknown_tool" }), tool: tool || "unknown" };
  }
  const operand = operandFor(tool, input);
  if (typeof operand !== "string" || !operand) {
    return verdict("deny", tool, { reason: "Tool input is malformed", code: "malformed_input" });
  }
  const result = classifyPath(rules, cwd, operand, WRITE_TOOLS.includes(tool));
  if (!result.scope) {
    return verdict("deny", tool, { reason: result.reason, code: "malformed_path" });
  }
  const external = result.scope === "external";
  const located = { resource: result.resource, requested: [result.resource] };
  if (result.mode === "deny") {
    return verdict("deny", tool, { ...located, reason: result.reason,
      code: result.code || (external ? "external_deny" : "tool_deny") });
  }
  if (result.mode === "allow") {
    return verdict("allow", tool, { ...located, reason: "Allowed by this security profile",
      code: external ? "external_allow" : "tool_allow" });
  }
  // ask: the exact action and target, never a wildcard, root or sibling.
  return verdict("ask", tool, { ...located, alwaysPattern: `${tool}:${result.resource}`,
    grantKey: `${tool}\n${result.resource}`, reason: "Requires approval",
    code: external ? "external_ask" : "tool_ask" });
}

// Compatibility names used across the adapter.
export const validatePolicy = validateRules;
export const policyRevision = profileRevision;
export function safeDefaultPolicy() {
  return structuredClone(BUILTINS["read-only"]);
}

// Directory containing this package-owned module, for fingerprint and
// identity diagnostics.
export function selfProtectionDir() {
  return path.dirname(fileURLToPath(import.meta.url));
}
