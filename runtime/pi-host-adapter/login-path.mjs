// Bounded interactive-login-shell executable search path resolution.
//
// Obtains only the final PATH value from the current OS user's configured
// login shell (os.userInfo().shell, $SHELL fallback, /bin/sh last resort)
// by spawning that shell once in interactive + login + command mode and
// printing $PATH between unique markers. Interactive mode matters: on zsh
// .zshrc is interactive-only, so login-only probing misses normal terminal
// PATH entries. Shell configuration files are never read directly and the
// complete environment is never captured or imported: stdout is scanned for
// the marked value, stderr is discarded at the spawn boundary, and only a
// validated PATH string is ever applied to process.env.PATH.
//
// Validation is strict: bounded length, no control/NUL/newline, absolute
// non-empty entries only, no relative/dot entries. On failure/timeout the
// caller keeps the inherited service search path.
//
// Observability exposes only resolved (bool), shell basename, entry count,
// and a bounded failure code; the actual PATH value is never exposed.
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const MAX_LOGIN_PATH_CHARS = 8192;
export const MAX_LOGIN_PATH_ENTRIES = 256;
export const MAX_LOGIN_SHELL_OUTPUT = 65536;
export const LOGIN_PATH_TIMEOUT_MS = 5000;

const MARK_BEGIN = "__WB_LOGIN_PATH_BEGIN__";
const MARK_END = "__WB_LOGIN_PATH_END__";
// Probe isolated on dedicated fd 3: ordinary stdout/stderr are ignored at
// the spawn boundary and never buffered. Only fd 3 is piped and captured
// with an explicit maxBuffer bound. No temp file is used.
const PROBE_COMMAND_POSIX = `printf '${MARK_BEGIN}%s${MARK_END}' "$PATH" >&3`;
const PROBE_COMMAND_FISH = `printf '${MARK_BEGIN}%s${MARK_END}' (string join : $PATH) >&3`;

// Shells supporting `-l -i -c <probe>` (interactive + login + command).
const INTERACTIVE_LOGIN_SHELLS = new Set([
  "bash", "zsh", "fish", "ksh", "ksh93", "mksh", "pdksh", "yash",
]);
// Minimal Bourne-family fallback: `-l` is not portable here (dash rejects
// it), so use conservative interactive-only `-i -c <probe>`.
const CONSERVATIVE_SH_SHELLS = new Set(["sh", "dash", "ash"]);

const SAFE_CODES = new Set([
  "ok",
  "timeout",
  "spawn_error",
  "unsupported",
  "nonzero_exit",
  "output_too_large",
  "marker_missing",
  "too_long",
  "empty",
  "control",
  "entry_count",
  "empty_entry",
  "relative",
  "dot_entry",
]);

function safeCode(value) {
  if (typeof value === "string" && SAFE_CODES.has(value)) return value;
  const cleaned = String(value ?? "spawn_error").replace(/[^A-Za-z0-9_]/g, "_").slice(0, 32);
  return cleaned || "spawn_error";
}

export function shellBasename(shell) {
  try {
    const base = path.basename(String(shell || ""));
    const cleaned = base.replace(/[^A-Za-z0-9._+-]/g, "").slice(0, 64);
    return cleaned || "sh";
  } catch {
    return "sh";
  }
}

function isUsableShell(value) {
  return typeof value === "string"
    && value.startsWith("/")
    && !value.includes("\x00")
    && !value.includes("\n")
    && !value.includes("\r")
    && value.length > 0
    && value.length <= 1024;
}

// Current OS user's configured login shell. Prefers the OS user database
// (os.userInfo().shell), then $SHELL when absolute, then /bin/sh.
export function getLoginShell({ userInfoShell, shellEnv } = {}) {
  let infoShell = userInfoShell;
  if (infoShell === undefined) {
    try {
      infoShell = os.userInfo().shell;
    } catch {
      infoShell = "";
    }
  }
  if (isUsableShell(infoShell)) return infoShell;
  const envShell = shellEnv !== undefined ? shellEnv : process.env.SHELL;
  if (isUsableShell(envShell)) return envShell;
  return "/bin/sh";
}

// Strictly validate a candidate PATH string. Returns { ok, entries, code }
// with a bounded safe code; never echoes the candidate.
export function validateSearchPath(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return { ok: false, entries: [], code: "empty" };
  }
  if (raw.length > MAX_LOGIN_PATH_CHARS) {
    return { ok: false, entries: [], code: "too_long" };
  }
  for (let i = 0; i < raw.length; i += 1) {
    const code = raw.charCodeAt(i);
    if (code < 32 || code === 127) {
      return { ok: false, entries: [], code: "control" };
    }
  }
  if (raw.includes("\x00")) {
    return { ok: false, entries: [], code: "control" };
  }
  const entries = raw.split(":");
  if (entries.length === 0 || entries.length > MAX_LOGIN_PATH_ENTRIES) {
    return { ok: false, entries: [], code: "entry_count" };
  }
  for (const entry of entries) {
    if (!entry) {
      return { ok: false, entries: [], code: "empty_entry" };
    }
    if (!entry.startsWith("/")) {
      return { ok: false, entries: [], code: "relative" };
    }
    if (entry === "." || entry === "..") {
      return { ok: false, entries: [], code: "dot_entry" };
    }
    const parts = entry.split("/");
    for (const part of parts) {
      if (part === "." || part === "..") {
        return { ok: false, entries: [], code: "dot_entry" };
      }
    }
  }
  return { ok: true, entries, code: "ok" };
}

export function probeCommandForShell(shell) {
  try {
    if (shellBasename(shell).toLowerCase() === "fish") return PROBE_COMMAND_FISH;
  } catch {
    // Fall through to the POSIX probe.
  }
  return PROBE_COMMAND_POSIX;
}

// Build interactive-login argv for a shell path, or null if unsupported.
// Supported families use `-l -i -c <probe>`; the minimal `sh` family uses
// conservative `-i -c <probe>`. Unknown shells return null so callers fail
// safely to the inherited PATH instead of guessing unsafe semantics.
export function probeArgv(shell) {
  let name = "";
  try {
    name = shellBasename(shell).toLowerCase();
  } catch {
    return null;
  }
  if (INTERACTIVE_LOGIN_SHELLS.has(name)) {
    return [shell, "-l", "-i", "-c", probeCommandForShell(shell)];
  }
  if (CONSERVATIVE_SH_SHELLS.has(name)) {
    return [shell, "-i", "-c", probeCommandForShell(shell)];
  }
  return null;
}

function extractMarkedPath(output) {
  try {
    const text = String(output ?? "");
    const start = text.indexOf(MARK_BEGIN);
    if (start < 0) return null;
    const valueStart = start + MARK_BEGIN.length;
    const end = text.indexOf(MARK_END, valueStart);
    if (end < 0) return null;
    return text.slice(valueStart, end);
  } catch {
    return null;
  }
}

// Resolve the login-shell PATH once. spawnSyncFn is injectable for
// deterministic unit tests: (shell, args, options) => { stdout, status,
// error }. Returns { resolved, path, shell, shellBasename, entryCount, code }.
export function resolveLoginPathSync({
  shell,
  timeoutMs = LOGIN_PATH_TIMEOUT_MS,
  spawnSyncFn = null,
  shellOverride = null,
} = {}) {
  const chosenRaw = typeof shellOverride === "string" && shellOverride
    ? shellOverride
    : (typeof shell === "string" && shell ? shell : getLoginShell());
  const chosen = isUsableShell(chosenRaw) ? chosenRaw : "/bin/sh";
  if (!isUsableShell(chosen)) {
    return { resolved: false, path: null, shell: "/bin/sh", shellBasename: "sh", entryCount: 0, code: "spawn_error" };
  }
  const basename = shellBasename(chosen);
  let timeoutValue = Number(timeoutMs);
  if (!Number.isFinite(timeoutValue) || timeoutValue <= 0 || timeoutValue > 30000) {
    timeoutValue = LOGIN_PATH_TIMEOUT_MS;
  }
  const argv = probeArgv(chosen);
  if (!argv) {
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: "unsupported" };
  }
  const [, ...probeArgs] = argv;
  const run = typeof spawnSyncFn === "function"
    ? spawnSyncFn
    : (s, args, options) => {
        // Hard-bounded capture on dedicated fd 3. fds 0/1/2 are ignored at
        // the boundary (interactive startup noise never buffered); only fd 3
        // carries the marked probe value, capped by explicit maxBuffer.
        const res = spawnSync(s, args, {
          ...options,
          stdio: ["ignore", "ignore", "ignore", "pipe"],
          maxBuffer: MAX_LOGIN_SHELL_OUTPUT,
        });
        let fd3 = "";
        try {
          const out = Array.isArray(res.output) && res.output.length > 3 ? res.output[3] : null;
          if (typeof out === "string") fd3 = out;
          else if (out != null) fd3 = String(out);
        } catch {
          fd3 = "";
        }
        return { stdout: fd3 ?? "", status: res.status, error: res.error };
      };
  let result;
  try {
    result = run(chosen, probeArgs, {
      timeout: timeoutValue,
      encoding: "utf8",
      windowsHide: true,
      // Injected fakes may ignore stdio/maxBuffer; the default runner
      // above enforces boundary discard plus explicit maxBuffer.
      stdio: ["ignore", "ignore", "ignore", "pipe"],
      maxBuffer: MAX_LOGIN_SHELL_OUTPUT,
    });
  } catch {
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: "spawn_error" };
  }
  if (!result || typeof result !== "object") {
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: "spawn_error" };
  }
  if (result.error) {
    // maxBuffer breach (ENOBUFS) means the dedicated fd-3 capture exceeded
    // its explicit bound during capture: deterministic oversize fallback.
    // ETIMEDOUT maps to timeout; all else falls back safely.
    const code = result.error.code === "ETIMEDOUT" ? "timeout"
      : result.error.code === "ENOBUFS" ? "output_too_large" : "spawn_error";
    // A timed-out shell may still have printed markers, but the value is not
    // trusted after a timeout: keep the inherited path.
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code };
  }
  let stdout = result.stdout;
  if (typeof stdout !== "string") {
    try {
      stdout = String(stdout ?? "");
    } catch {
      stdout = "";
    }
  }
  if (stdout.length > MAX_LOGIN_SHELL_OUTPUT) {
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: "output_too_large" };
  }
  const status = result.status;
  let candidate = extractMarkedPath(stdout);
  if (candidate === null) {
    const code = status !== 0 ? "nonzero_exit" : "marker_missing";
    // Distinguish spawn failure (nonzero without markers) from a shell that
    // exited zero but printed no markers.
    if (status !== 0) {
      return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: "nonzero_exit" };
    }
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code };
  }
  const checked = validateSearchPath(candidate);
  if (!checked.ok) {
    return { resolved: false, path: null, shell: chosen, shellBasename: basename, entryCount: 0, code: safeCode(checked.code) };
  }
  return {
    resolved: true,
    path: candidate,
    shell: chosen,
    shellBasename: basename,
    entryCount: checked.entries.length,
    code: "ok",
  };
}

// Safe observability only: never the PATH value.
export function loginPathSummary(result) {
  try {
    const resolved = Boolean(result && result.resolved);
    const basename = shellBasename(result && result.shell ? result.shell : "");
    let count = 0;
    try {
      count = Number(result ? result.entryCount : 0);
    } catch {
      count = 0;
    }
    if (!Number.isFinite(count)) count = 0;
    count = Math.max(0, Math.min(Math.trunc(count), MAX_LOGIN_PATH_ENTRIES + 1));
    const code = safeCode(result ? result.code : "spawn_error");
    return { resolved, shellBasename: basename, entryCount: count, code };
  } catch {
    return { resolved: false, shellBasename: "sh", entryCount: 0, code: "spawn_error" };
  }
}

// Apply a resolved result to targetEnv (default process.env): update only
// PATH on success, otherwise leave the inherited path untouched. Returns the
// resolver result for observability.
export function applyLoginPathResult(result, targetEnv = process.env) {
  try {
    if (result && result.resolved && typeof result.path === "string") {
      const checked = validateSearchPath(result.path);
      if (checked.ok && targetEnv && typeof targetEnv === "object") {
        targetEnv.PATH = result.path;
      }
    }
  } catch {
    // Applying must never throw; fallback is the inherited path.
  }
  return result;
}
