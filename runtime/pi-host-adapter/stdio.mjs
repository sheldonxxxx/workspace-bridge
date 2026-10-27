// Agent Protocol v2 over stdio for the Pi adapter.
//
// A supervising Workspace Bridge Node starts `workspace-bridge-pi-adapter
// --stdio`, sends `initialize` with the projects root and plugin settings,
// then named JSON-RPC requests, one JSON object per line. Responses are
// written whole, one line each. Nothing else may write to stdout in this
// mode: logs go to stderr. No port, no token, no shell or command route.
import { createInterface } from "node:readline";

import { AdapterError } from "./adapter.mjs";
import { PathError } from "./paths.mjs";
import { PiRuntimeProtocol } from "./wbrp.mjs";

export const PROTOCOL = 2;
export const MAX_LINE = 16 * 1024 * 1024;
const CODE = /^[a-z][a-z0-9_]{0,79}$/;

function errorPayload(status, code, message) {
  const safeStatus = Number.isInteger(status) && status >= 400 && status <= 599 ? status : 502;
  const safeCode = typeof code === "string" && CODE.test(code) ? code : "runtime_rejected";
  return { code: -32000, message: String(message || "").slice(0, 300),
           data: { status: safeStatus, code: safeCode } };
}

function failure(error) {
  if (error instanceof AdapterError || error instanceof PathError) {
    return errorPayload(error.status, error.code, error.message);
  }
  return errorPayload(502, "runtime_unavailable", "Pi runtime is unavailable");
}

function body(params) {
  const value = params.body === undefined ? {} : params.body;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AdapterError("body must be an object", 400, "invalid_arguments");
  }
  return value;
}

export const HANDLERS = {
  descriptor: (p, _params) => p.descriptor(),
  models: (p) => p.models(),
  "conversations.create": (p, params) => p.createConversation(body(params)),
  "conversations.get": (p, params) => p.conversation(params.conversationId),
  "runs.start": (p, params) => p.startRun(params.conversationId, body(params)),
  "runs.find": (p, params) => p.findRun(params.conversationId, params.clientRunId),
  "runs.get": (p, params) => p.run(params.runId),
  "runs.cancel": (p, params) => p.cancel(params.runId),
  "runs.steer": (p, params) => p.submitInput(params.runId, "steer", body(params)),
  "runs.queue": (p, params) => p.submitInput(params.runId, "queue", body(params)),
  "runs.interactions": (p, params) => p.interactions(params.runId),
  "runs.activities": (p, params) => p.activityList(params.runId),
  "interactions.resolve": (p, params) => p.resolve(params.interactionId, body(params)),
  "activities.get": (p, params) => p.activity(params.activityId),
};

// `buildAdapter(params)` returns a PiAdapter for the initialize params;
// `write(line)` writes one protocol line to the real stdout.
export function serveStdio({ input = process.stdin, write, buildAdapter, onLog = null,
                             exit = (code) => process.exit(code) }) {
  let adapter = null;
  let protocol = null;
  const send = (message) => write(JSON.stringify(message));
  const log = (level, event, fields = {}) => {
    try {
      if (typeof onLog === "function") onLog(level, "pi-adapter", event, fields);
    } catch { /* logging never breaks the protocol */ }
  };

  async function handle(id, method, params) {
    try {
      if (!protocol) throw new AdapterError("initialize first", 400, "invalid_arguments");
      const handler = HANDLERS[method];
      if (!handler) {
        send({ jsonrpc: "2.0", id, error: errorPayload(404, "not_found", "Unknown method") });
        return;
      }
      const result = await handler(protocol, params);
      send({ jsonrpc: "2.0", id, result });
    } catch (error) {
      if (!(error instanceof AdapterError || error instanceof PathError)) {
        log("ERROR", "request_error", { code: "runtime_unavailable" });
      }
      send({ jsonrpc: "2.0", id, error: failure(error) });
    }
  }

  async function shutdown() {
    try {
      if (adapter) await adapter.shutdown();
    } catch { /* exiting anyway */ }
    exit(0);
  }

  const lines = createInterface({ input, crlfDelay: Infinity });
  lines.on("line", (line) => {
    if (line.length > MAX_LINE) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (!message || typeof message !== "object" || typeof message.method !== "string") return;
    const params = message.params && typeof message.params === "object" ? message.params : {};
    if (message.method === "initialize") {
      if (protocol || params.protocol !== PROTOCOL) {
        send({ jsonrpc: "2.0", id: message.id,
               error: errorPayload(400, "protocol_mismatch", `Expected protocol ${PROTOCOL}`) });
        return;
      }
      try {
        adapter = buildAdapter(params);
        protocol = new PiRuntimeProtocol(adapter);
        send({ jsonrpc: "2.0", id: message.id,
               result: { protocol: PROTOCOL, instanceId: protocol.instanceId } });
      } catch (error) {
        log("ERROR", "adapter_config_error", { code: error && error.code ? String(error.code) : "startup_failed" });
        send({ jsonrpc: "2.0", id: message.id, error: failure(error) });
      }
      return;
    }
    if (message.method === "shutdown") {
      shutdown();
      return;
    }
    if (message.id !== undefined) handle(message.id, message.method, params);
  });
  lines.on("close", () => { shutdown(); });
  return lines;
}
