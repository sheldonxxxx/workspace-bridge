// Pi --mode rpc subprocess owner.
//
// One long-lived `pi --mode rpc` child per active adapter session, spawned
// directly with child_process.spawn (no shell). stdout is strict LF-delimited
// JSONL: commands are sent as `{type, id, ...params}` lines and responses
// correlate by `id` (`{type:"response", id, command, success, data|error}`).
// Non-response lines are agent events and never resolve a pending command.
//
// Framing is byte-oriented: raw stdout bytes accumulate in a Buffer until
// LF (0x0A, which can never appear inside a multi-byte UTF-8 sequence), so a
// code point split across chunks is always decoded as part of a complete
// frame. maxLineBytes applies independently to each LF-delimited raw-byte
// frame, never to aggregate chunk size. Complete frames are decoded with
// strict (fatal) UTF-8 validation. Invalid UTF-8, malformed JSON, oversized
// frames, uncorrelated responses, command mismatches, and incomplete trailing
// frames all fail closed.
//
// Fail-closed behavior:
// - protocol violations mark the session failed (dead) and reject every
//   pending command;
// - child exit/EOF marks the session dead; a replacement is never created;
// - stderr is bounded diagnostic input only and is never emitted in logs or
//   API responses (only its byte length is exposed for debugging).
import { spawn } from "node:child_process";

import { SUPPORTED_TOOLS } from "./policy.mjs";

export const RPC_TIMEOUT_MS = 30000;
export const MAX_LINE_BYTES = 1024 * 1024;
export const MAX_STDERR_BYTES = 8192;
// Independent permission-metadata bounds: preflight correlation retains
// only these small fields, never tool payloads.
export const MAX_TOOL_CALL_ID_CHARS = 200;
export const MAX_TOOL_NAME_CHARS = 120;
export const MAX_PERMISSION_PATH_CHARS = 4096;
export const MAX_UI_ID_CHARS = 200;
export const MAX_UI_TITLE_CHARS = 500;
export const MAX_UI_OPTIONS = 8;
export const MAX_UI_OPTION_CHARS = 400;

// Reduce a raw tool_execution_start to bounded path-only permission
// metadata. Only the single path operand per known file-tool schema is
// kept (read/edit/write path; grep/find/ls optional path). Write content,
// edit old/new text, grep patterns, find globs, and arbitrary args are
// NEVER retained. Returns null when the event is unusable for correlation.
export function normalizeToolStart(message) {
  if (!message || typeof message !== "object") return null;
  const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
  const toolName = typeof message.toolName === "string" ? message.toolName : "";
  if (!toolCallId || toolCallId.length > MAX_TOOL_CALL_ID_CHARS
      || !toolName || toolName.length > MAX_TOOL_NAME_CHARS) {
    return null;
  }
  const args = message.args;
  if (args !== undefined
      && (args === null || typeof args !== "object" || Array.isArray(args))) {
    // Present-but-malformed args: keep the call identity with an empty
    // input so evaluation fails closed as malformed (never allowed).
    return { type: "tool_execution_start", toolCallId, toolName, input: null };
  }
  let input = {};
  if (SUPPORTED_TOOLS.includes(toolName) && args && typeof args === "object"
      && Object.prototype.hasOwnProperty.call(args, "path")) {
    const rawPath = args.path;
    // Never truncate paths: an oversized/invalid path becomes an explicit
    // null so evaluation denies instead of judging a different target.
    input = (typeof rawPath === "string" && rawPath && !rawPath.includes("\0")
        && rawPath.length <= MAX_PERMISSION_PATH_CHARS)
      ? { path: rawPath }
      : { path: null };
  }
  return { type: "tool_execution_start", toolCallId, toolName, input };
}

export function normalizeToolEnd(message) {
  if (!message || typeof message !== "object") return null;
  const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
  if (!toolCallId || toolCallId.length > MAX_TOOL_CALL_ID_CHARS) return null;
  // Tool results may carry file content: never forwarded.
  return { type: "tool_execution_end", toolCallId };
}

export function normalizeUiRequest(message) {
  if (!message || typeof message !== "object") return null;
  const id = typeof message.id === "string" ? message.id : "";
  const method = typeof message.method === "string" ? message.method : "";
  const title = typeof message.title === "string" ? message.title : "";
  if (!id || id.length > MAX_UI_ID_CHARS || !method || title.length > MAX_UI_TITLE_CHARS) {
    return null;
  }
  let options = null;
  if (Array.isArray(message.options)) {
    options = message.options
      .filter((o) => typeof o === "string")
      .slice(0, MAX_UI_OPTIONS)
      .map((o) => o.slice(0, MAX_UI_OPTION_CHARS));
  }
  return { type: "extension_ui_request", id, method, title, options };
}

export class RpcError extends Error {
  constructor(message, code = "runtime_unavailable", status = 502) {
    super(message);
    this.name = "RpcError";
    this.code = code;
    this.status = status;
  }
}

let nextId = 1;

export class PiRpcProcess {
  constructor({ binary, cwd, agentDir, extraEnv = {}, spawnFn = spawn,
                timeoutMs = RPC_TIMEOUT_MS, maxLineBytes = MAX_LINE_BYTES,
                onEvent = null, extensionPath = null }) {
    this.binary = binary;
    this.cwd = cwd;
    this.agentDir = agentDir;
    this.extraEnv = extraEnv;
    this.spawnFn = spawnFn;
    this.timeoutMs = timeoutMs;
    this.maxLineBytes = maxLineBytes;
    // Bounded agent-event callback for permission correlation
    // (tool_execution_start/end, extension_ui_request). Unrelated events
    // are ignored. Listener errors never break framing.
    this.onEvent = typeof onEvent === "function" ? onEvent : null;
    // Package-owned trusted permission extension, loaded only in writable
    // mode. The full path is never logged or returned.
    this.extensionPath = typeof extensionPath === "string" && extensionPath ? extensionPath : null;
    this.child = null;
    this.pending = new Map();
    // Raw-byte frame accumulator: bytes are split on LF before any UTF-8
    // decoding, so multi-byte code points straddling chunks are never torn.
    this.raw = Buffer.alloc(0);
    this.utf8 = new TextDecoder("utf-8", { fatal: true });
    this.dead = false;
    this.exitInfo = null;
    this.stderrBytes = 0;
    this.sessionId = null;
    this.sessionFile = null;
    this.spawnArgs = null;
    this.onExit = null;
  }

  argv() {
    // Permission-aware spawn contract (3B1), centralized here:
    // - read-only (no trusted extension): strict read-family allowlist,
    //   project trust/extensions out (see README);
    // - writable: read/grep/find/ls/edit/write plus exactly one
    //   package-owned trusted extension. No bash in either mode.
    const base = ["--mode", "rpc"];
    if (this.extensionPath) {
      return [...base, "--tools", "read,grep,find,ls,edit,write",
        "--no-approve", "--no-extensions", "-e", this.extensionPath];
    }
    return [...base, "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"];
  }

  get alive() {
    return Boolean(this.child) && !this.dead && this.child.exitCode === null;
  }

  start() {
    if (this.child) throw new RpcError("RPC process already started", "internal", 500);
    const args = this.argv();
    this.spawnArgs = [this.binary, ...args];
    const env = { ...process.env, ...this.extraEnv, PI_CODING_AGENT_DIR: this.agentDir };
    const child = this.spawnFn(this.binary, args, {
      cwd: this.cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    if (!child || !child.stdin || !child.stdout || !child.stderr) {
      throw new RpcError("Pi process could not be started", "runtime_unavailable", 502);
    }
    this.child = child;
    child.stderr.on("data", (chunk) => {
      this.stderrBytes = Math.min(MAX_STDERR_BYTES, this.stderrBytes + chunk.length);
    });
    child.stdout.on("data", (chunk) => this._onStdout(chunk));
    const markDead = (info) => this._markDead(info);
    child.on("exit", (code, signal) => {
      markDead({ reason: "exit", code, signal });
    });
    child.on("error", (error) => {
      markDead({ reason: "error", message: String((error && error.message) || error).slice(0, 200) });
    });
    child.stdout.on("end", () => {
      if (this.dead) return;
      // Any non-empty raw remainder never saw a terminating LF: it is an
      // incomplete trailing frame (possibly a cut multi-byte sequence) and
      // fails closed instead of being silently accepted.
      if (this.raw.length > 0) {
        this.raw = Buffer.alloc(0);
        this._protocolFailure("incomplete trailing frame");
        return;
      }
      markDead({ reason: "eof" });
    });
    return this.command("get_state", {}, { timeoutMs: this.timeoutMs }).then((data) => {
      const sessionId = data && typeof data.sessionId === "string" ? data.sessionId : "";
      if (!sessionId) {
        this._markDead({ reason: "protocol", message: "get_state returned no sessionId" });
        throw new RpcError("Pi session binding failed", "runtime_unavailable", 502);
      }
      this.sessionId = sessionId;
      this.sessionFile = typeof data.sessionFile === "string" ? data.sessionFile : null;
      return data;
    }).catch((error) => {
      if (!this.dead) {
        // Startup failure: stop the child so no orphan survives.
        try { this.kill(); } catch { /* best effort */ }
      }
      throw error;
    });
  }

  command(type, params = {}, { timeoutMs = this.timeoutMs } = {}) {
    if (this.dead || !this.child) {
      return Promise.reject(new RpcError("Pi session is unavailable", "unavailable", 502));
    }
    const id = `cmd-${nextId++}`;
    const line = JSON.stringify({ type, id, ...params }) + "\n";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError(`Pi command timed out: ${type}`, "timeout", 504));
      }, timeoutMs);
      if (timer.unref) timer.unref();
      this.pending.set(id, { type, resolve, reject, timer });
      try {
        this.child.stdin.write(line, (error) => {
          if (error) {
            clearTimeout(timer);
            this.pending.delete(id);
            reject(new RpcError("Pi session is unavailable", "unavailable", 502));
          }
        });
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new RpcError("Pi session is unavailable", "unavailable", 502));
      }
    });
  }

  _onStdout(chunk) {
    if (this.dead) return;
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), "utf8");
    this.raw = this.raw.length === 0 ? bytes : Buffer.concat([this.raw, bytes]);
    // Split raw bytes on LF before decoding: 0x0A never occurs inside a
    // UTF-8 multi-byte sequence, so frames always contain whole code points.
    // The byte limit applies to each frame independently.
    for (;;) {
      if (this.dead) return;
      const lf = this.raw.indexOf(0x0a);
      if (lf === -1) {
        if (this.raw.length > this.maxLineBytes) {
          this.raw = Buffer.alloc(0);
          this._protocolFailure("oversized line");
          return;
        }
        return;
      }
      const frame = this.raw.subarray(0, lf);
      this.raw = this.raw.subarray(lf + 1);
      if (frame.length === 0) continue;
      if (frame.length > this.maxLineBytes) {
        this.raw = Buffer.alloc(0);
        this._protocolFailure("oversized line");
        return;
      }
      let line;
      try {
        line = this.utf8.decode(frame);
      } catch {
        this._protocolFailure("invalid utf8");
        return;
      }
      this._onLine(line);
    }
  }

  _onLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      this._protocolFailure("malformed line");
      return;
    }
    if (!message || typeof message !== "object") {
      this._protocolFailure("malformed line");
      return;
    }
    // Responses correlate by id AND command. Anything else is an agent
    // event line.
    if (message.type === "response" && typeof message.id === "string" && this.pending.has(message.id)) {
      const waiter = this.pending.get(message.id);
      // A mismatched command with the correct id is a protocol failure;
      // it must never resolve the pending command as success.
      if (typeof message.command === "string" && message.command !== waiter.type) {
        this._protocolFailure("command mismatch");
        return;
      }
      const { resolve, reject, timer } = waiter;
      this.pending.delete(message.id);
      clearTimeout(timer);
      if (message.success) {
        resolve(message.data === undefined ? {} : message.data);
      } else {
        const detail = typeof message.error === "string" && message.error
          ? message.error.slice(0, 300)
          : "Pi command failed";
        reject(new RpcError(detail, "command_failed", 502));
      }
      return;
    }
    if (message.type === "response") {
      // A response that correlates to nothing is a protocol violation.
      this._protocolFailure("uncorrelated response");
      return;
    }
    // Agent event (e.g. agent_settled, tool_execution_start/end,
    // extension_ui_request): never resolves a pending command. The bounded
    // permission-correlation subset is normalized to minimal path-only
    // metadata and forwarded to the adapter listener; everything else is
    // ignored here. Raw tool payloads never leave this process.
    if (this.onEvent) {
      let normalized = null;
      try {
        if (message.type === "tool_execution_start") {
          normalized = normalizeToolStart(message);
        } else if (message.type === "tool_execution_end") {
          normalized = normalizeToolEnd(message);
        } else if (message.type === "extension_ui_request") {
          normalized = normalizeUiRequest(message);
        }
      } catch {
        normalized = null;
      }
      if (normalized) {
        try {
          this.onEvent(normalized);
        } catch {
          // Listener errors never break framing or fail the session.
        }
      }
    }
  }

  // Validated direct extension_ui_response writer for permission resume.
  // Never enters the command pending map and never expects a response.
  // Returns a Promise that resolves true ONLY after the stdin write
  // callback positively confirms success; any validation failure, dead
  // session, throw, or callback error resolves false. Callers must remove
  // pending permission state only on a confirmed true.
  writeUiResponse(payload) {
    return new Promise((resolve) => {
      if (this.dead || !this.child) return resolve(false);
      if (!payload || typeof payload !== "object") return resolve(false);
      const id = payload.id;
      if (typeof id !== "string" || !id || id.length > MAX_UI_ID_CHARS) {
        return resolve(false);
      }
      let body;
      if (payload.cancelled === true) {
        body = { type: "extension_ui_response", id, cancelled: true };
      } else if (typeof payload.value === "string") {
        if (payload.value.length > MAX_UI_OPTION_CHARS) return resolve(false);
        body = { type: "extension_ui_response", id, value: payload.value };
      } else if (typeof payload.confirmed === "boolean") {
        body = { type: "extension_ui_response", id, confirmed: payload.confirmed };
      } else {
        return resolve(false);
      }
      try {
        this.child.stdin.write(`${JSON.stringify(body)}\n`, (error) => {
          resolve(!error);
        });
      } catch {
        resolve(false);
      }
    });
  }

  _protocolFailure(message) {
    this._markDead({ reason: "protocol", message });
  }

  // Public fail-closed hook for adapter-level binding violations (e.g.
  // post-start session drift). Marks the session dead and rejects every
  // pending command; never creates a replacement.
  failClosed(message) {
    this._protocolFailure(message);
  }

  _markDead(info) {
    if (this.dead) return;
    this.dead = true;
    this.exitInfo = info;
    for (const [, waiter] of this.pending) {
      clearTimeout(waiter.timer);
      waiter.reject(new RpcError("Pi session is unavailable", "unavailable", 502));
    }
    this.pending.clear();
    if (typeof this.onExit === "function") {
      try { this.onExit(info); } catch { /* listener must not throw */ }
    }
  }

  // Graceful stop of ONLY this owned child: SIGTERM, then SIGKILL after
  // graceMs. Never touches any other process.
  async close({ graceMs = 3000 } = {}) {
    const child = this.child;
    if (!child) return;
    if (child.exitCode !== null) return;
    await new Promise((resolve) => {
      const force = setTimeout(() => {
        try { child.kill("SIGKILL"); } catch { /* best effort */ }
        resolve();
      }, Math.max(0, graceMs));
      if (force.unref) force.unref();
      child.once("exit", () => {
        clearTimeout(force);
        resolve();
      });
      try { child.kill("SIGTERM"); } catch {
        clearTimeout(force);
        resolve();
      }
    });
  }

  kill() {
    try { this.child?.kill("SIGKILL"); } catch { /* best effort */ }
  }
}
