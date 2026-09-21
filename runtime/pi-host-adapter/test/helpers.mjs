// Shared fakes for pi-host-adapter tests: an in-process fake child process
// (no real spawn) plus a scripted responder.
import { EventEmitter } from "node:events";

export class FakeChild extends EventEmitter {
  constructor(binary, args, opts) {
    super();
    this.binary = binary;
    this.args = args;
    this.opts = opts;
    this.written = [];
    this.killedSignals = [];
    this.exitCode = null;
    this.autoExitCode = null;
    this.stdin = {
      write: (line, cb) => {
        this.written.push(String(line));
        this.emit("stdin", String(line));
        if (typeof cb === "function") cb(null);
        return true;
      },
    };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
  }

  requests() {
    return this.written.join("").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  }

  lastRequest() {
    const all = this.requests();
    return all[all.length - 1];
  }

  respond(message) {
    this.stdout.emit("data", Buffer.from(`${JSON.stringify(message)}\n`));
  }

  respondRaw(text) {
    this.stdout.emit("data", Buffer.from(text));
  }

  kill(signal = "SIGTERM") {
    this.killedSignals.push(signal || "SIGTERM");
    if (this.autoExitCode !== null && this.exitCode === null) {
      const code = this.autoExitCode;
      setImmediate(() => this.die(code));
    }
    return true;
  }

  die(code = 0, signal = null) {
    if (this.exitCode !== null) return;
    this.exitCode = code;
    this.emit("exit", code, signal);
  }

  eof() {
    this.stdout.emit("end");
  }
}

export function createFakeSpawn() {
  const calls = [];
  const children = [];
  function spawnFn(binary, args, opts) {
    const child = new FakeChild(binary, args, opts);
    calls.push({ binary, args, opts, child });
    children.push(child);
    return child;
  }
  return { spawnFn, calls, children };
}

export function stateData(overrides = {}) {
  return {
    sessionId: "ses-fake-1",
    sessionFile: "/tmp/fake-session.jsonl",
    isStreaming: false,
    messageCount: 0,
    pendingMessageCount: 0,
    ...overrides,
  };
}

export function respondState(child, id, overrides = {}) {
  child.respond({ id, type: "response", command: "get_state", success: true, data: stateData(overrides) });
}

// Start an rpc/command round trip: begins start(), answers get_state, awaits it.
export async function startRpc(rpc, children, overrides = {}) {
  const started = rpc.start();
  const child = children[0];
  const req = child.lastRequest();
  respondState(child, req.id, overrides);
  await started;
  return child;
}
