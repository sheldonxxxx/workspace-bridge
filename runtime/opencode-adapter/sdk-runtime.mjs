// Narrow @opencode-ai/sdk client wrapper for the private adapter sidecar.
//
// This module is the ONLY place that imports the OpenCode SDK. It deliberately
// uses createOpencodeClient() against an externally managed base URL and never
// imports or calls createOpencode()/createOpencodeServer(): Workspace Bridge
// must never start, supervise or package an OpenCode server.
import { createOpencodeClient } from "@opencode-ai/sdk";

export const ADAPTER_VERSION = "0.1.4";

const MAX_EVENTS = 2000;
const MAX_METADATA_KEYS = 40;
const MAX_TEXT = 8000;

export function buildClient({ baseUrl, username, password, createClient = createOpencodeClient }) {
  const headers = {};
  if (username || password) {
    headers.Authorization = "Basic " + Buffer.from(`${username || ""}:${password || ""}`).toString("base64");
  }
  return createClient({ baseUrl, headers });
}

export class SdkError extends Error {
  constructor(message, status = 502, code = "runtime_error") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function errorMessage(error, fallback) {
  if (!error) return fallback;
  if (typeof error === "string") return error.slice(0, 300);
  if (typeof error.message === "string") return error.message.slice(0, 300);
  if (error.data && typeof error.data.message === "string") return error.data.message.slice(0, 300);
  if (typeof error.name === "string") return error.name.slice(0, 300);
  return fallback;
}

function unwrap(result, { fallback, notFound = false } = {}) {
  if (!result || typeof result !== "object") throw new SdkError("Empty SDK response");
  const { data, error, response } = result;
  if (error) {
    const status = response && typeof response.status === "number" ? response.status : 502;
    if (notFound && (status === 404 || (error.name && error.name === "NotFoundError"))) return null;
    if (status === 400 || status === 409 || status === 412 || status === 422) {
      throw new SdkError(errorMessage(error, fallback || "Runtime rejected the request"), status, "rejected");
    }
    throw new SdkError(errorMessage(error, fallback || "OpenCode request failed"), status, "runtime_error");
  }
  return data;
}

function sanitizeValue(value, depth = 0) {
  if (depth > 2) return undefined;
  if (value === null || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "string") return value.slice(0, 400);
  if (Array.isArray(value)) {
    return value.slice(0, MAX_METADATA_KEYS).map((item) => sanitizeValue(item, depth + 1)).filter((v) => v !== undefined);
  }
  if (typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).slice(0, MAX_METADATA_KEYS)) {
      const mapped = sanitizeValue(value[key], depth + 1);
      if (mapped !== undefined) out[String(key).slice(0, 80)] = mapped;
    }
    return out;
  }
  return undefined;
}

const SECRET_PATTERNS = [
  /-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----/g,
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g,
  /\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g,
  /(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|authorization)\s*["']?\s*[:=]\s*["']?[^\s"',;}{]{8,}/gi,
];

export function sanitizeMetadata(value) {
  const flattened = sanitizeValue(value);
  if (flattened === undefined) return { value: {}, redacted: false };
  let text = JSON.stringify(flattened);
  let redacted = false;
  for (const pattern of SECRET_PATTERNS) {
    if (pattern.test(text)) {
      redacted = true;
      text = text.replace(pattern, "[REDACTED_SECRET]");
    }
    pattern.lastIndex = 0;
  }
  if (text.length > 6000) {
    return { value: {}, redacted };
  }
  try {
    return { value: JSON.parse(text), redacted };
  } catch {
    return { value: {}, redacted };
  }
}

function normalizeMessage(entry) {
  const info = entry && entry.info ? entry.info : {};
  const parts = Array.isArray(entry && entry.parts) ? entry.parts : [];
  let text = "";
  const tools = [];
  for (const part of parts) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text" && typeof part.text === "string") {
      text += (text ? "\n" : "") + part.text.slice(0, MAX_TEXT);
    } else if (part.type === "tool" && typeof part.tool === "string") {
      tools.push(part.tool.slice(0, 120));
    }
  }
  return {
    id: typeof info.id === "string" ? info.id : "",
    role: typeof info.role === "string" ? info.role : "",
    created: info.time && Number.isFinite(info.time.created) ? info.time.created : null,
    completed: info.time && Number.isFinite(info.time.completed) ? info.time.completed : null,
    text: text.slice(0, MAX_TEXT),
    tools: tools.slice(0, 24),
    error: info.error ? errorMessage(info.error, "error").slice(0, 300) : null,
  };
}

function observedDirectory(value) {
  return typeof value === "string" && value.length > 0 ? value : "";
}

function boundedStringArray(value, limit = 32) {
  if (typeof value === "string") return [value.slice(0, 400)];
  if (Array.isArray(value)) {
    return value.filter((p) => typeof p === "string").slice(0, limit).map((p) => p.slice(0, 400));
  }
  return [];
}

function hasOwn(value, key) {
  return !!value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, key);
}

export class SdkRuntime {
  constructor({ client, clock = () => new Date().toISOString() }) {
    this.client = client;
    this.clock = clock;
    this.lastVersion = null;
  }

  async health() {
    const base = { adapter_version: ADAPTER_VERSION, version: this.lastVersion, server_configured: true };
    try {
      const data = unwrap(await this.client.provider.list(), { fallback: "Provider list failed" });
      const providers = data && Array.isArray(data.all) ? data.all.length : Array.isArray(data?.providers) ? data.providers.length : 0;
      return { ok: true, providers, ...base };
    } catch (error) {
      return { ok: false, error: error instanceof SdkError ? error.code : "unreachable", ...base };
    }
  }

  async listModels() {
    // Global discovery: the native server/provider configuration is the source
    // of available models. No workspace directory is passed or used; the
    // `directory` query parameter of /config/providers is optional and callers
    // must not depend on it.
    const data = unwrap(await this.client.config.providers({}),
      { fallback: "Model list failed" });
    const providers = data && Array.isArray(data.providers) ? data.providers : [];
    const defaults = data && data.default && typeof data.default === "object" ? data.default : {};
    const models = [];
    for (const provider of providers) {
      const providerId = provider && provider.id ? String(provider.id) : "";
      if (!providerId || !provider.models) continue;
      for (const [modelId, model] of Object.entries(provider.models)) {
        models.push({
          provider: providerId,
          model: String(modelId),
          selector: `${providerId}/${modelId}`,
          name: model && typeof model.name === "string" ? model.name.slice(0, 200) : String(modelId),
          default: defaults[providerId] === modelId,
          variants: [],
        });
      }
    }
    return models;
  }

  async createSession(directory, title) {
    const data = unwrap(await this.client.session.create({ query: { directory }, body: { title } }),
      { fallback: "Session creation failed" });
    if (!data || !data.id) throw new SdkError("Session creation returned no id");
    if (typeof data.version === "string") this.lastVersion = data.version;
    // The requested query directory is routing input, never evidence of session
    // ownership: if upstream omits it, return an explicit empty observed value so
    // the bridge fails closed instead of masking a missing directory.
    return { id: data.id, directory: observedDirectory(data.directory),
             title: data.title || title, version: data.version || null };
  }

  async getSession(directory, id) {
    const data = unwrap(await this.client.session.get({ path: { id }, query: { directory } }),
      { fallback: "Session lookup failed", notFound: true });
    if (!data || !data.id) return null;
    if (typeof data.version === "string") this.lastVersion = data.version;
    return { id: data.id, directory: observedDirectory(data.directory),
             title: data.title || "", version: data.version || null };
  }

  async promptAsync(directory, id, text, model) {
    const body = { parts: [{ type: "text", text: String(text).slice(0, 60000) }] };
    if (model && model.providerID && model.modelID) {
      body.model = { providerID: model.providerID, modelID: model.modelID };
    }
    unwrap(await this.client.session.promptAsync({ path: { id }, query: { directory }, body }),
      { fallback: "Prompt submission failed" });
    return true;
  }

  async messages(directory, id, limit) {
    const data = unwrap(await this.client.session.messages({ path: { id }, query: { directory, limit } }),
      { fallback: "Message read failed" });
    if (!Array.isArray(data)) return [];
    return data.map(normalizeMessage);
  }

  async respondPermission(directory, id, permissionID, response) {
    if (!["once", "always", "reject"].includes(response)) {
      throw new SdkError("Permission response must be once, always or reject", 400, "rejected");
    }
    // Pass the decision through unchanged. "always" approves OpenCode's own
    // proposed pattern; the adapter never rewrites or broadens it.
    const data = unwrap(await this.client.postSessionIdPermissionsPermissionId({
      path: { id, permissionID }, query: { directory }, body: { response },
    }), { fallback: "Permission response failed" });
    return data === true;
  }

  async abortSession(directory, id) {
    const data = unwrap(await this.client.session.abort({ path: { id }, query: { directory } }),
      { fallback: "Abort failed" });
    return data === true;
  }

  async sessionStatus(directory, id) {
    // Official OpenCode contract: GET /session/status returns a map of
    // session id -> {type: idle|busy|retry}. An entry absent from the map
    // means the native server currently tracks no active work for that
    // session, which the installed v1.18.31 server uses for idle sessions;
    // treat a missing entry as idle. A present but malformed/unknown entry
    // fails validation so callers fail closed instead of guessing.
    const data = unwrap(await this.client.session.status({ query: { directory } }),
      { fallback: "Session status failed" });
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new SdkError("Session status response was invalid");
    }
    if (!Object.prototype.hasOwnProperty.call(data, id)) return "idle";
    const entry = data[id];
    const kind = entry && typeof entry.type === "string" ? entry.type : "";
    if (kind === "idle" || kind === "busy" || kind === "retry") return kind;
    throw new SdkError("Session status response was invalid");
  }
}

function normalizeEvent(raw, cursor) {
  if (!raw || typeof raw !== "object" || typeof raw.type !== "string") return null;
  const properties = raw.properties && typeof raw.properties === "object" ? raw.properties : {};
  const sessionId = typeof properties.sessionID === "string" ? properties.sessionID
    : typeof properties.sessionId === "string" ? properties.sessionId : null;
  const base = { cursor, type: raw.type, session_id: sessionId, data: {} };
  // OpenCode v1.18.31 emits the user approval prompt as "permission.asked".
  // "permission.updated" is kept as a compatibility alias for earlier/newer SDK
  // behavior. Both normalize to ONE canonical internal representation,
  // "permission.asked", so downstream code has a single ask branch.
  if (raw.type === "permission.asked" || raw.type === "permission.updated") {
    base.type = "permission.asked";
    const metadata = sanitizeMetadata(properties.metadata);
    // Real OpenCode V1 PermissionV1.Request: {id, sessionID, permission,
    // patterns, always, metadata, tool}. `patterns` is what is being
    // requested; `always` is OpenCode's exact proposed session-scoped always
    // approval scope. They must not be collapsed: the Bridge's canonical
    // `pattern` field is the Approve-always display and reflects `always`.
    const requestedPatterns = hasOwn(properties, "patterns")
      ? boundedStringArray(properties.patterns)
      : boundedStringArray(properties.pattern);
    const alwaysScope = hasOwn(properties, "always")
      ? boundedStringArray(properties.always)
      : boundedStringArray(hasOwn(properties, "patterns") ? properties.patterns : properties.pattern);
    let tool = null;
    if (typeof properties.tool === "string") {
      tool = properties.tool.slice(0, 200);
    } else if (properties.tool && typeof properties.tool === "object") {
      tool = sanitizeMetadata(properties.tool).value;
    }
    const id = typeof properties.id === "string" ? properties.id
      : typeof properties.requestID === "string" ? properties.requestID
      : typeof properties.requestId === "string" ? properties.requestId : "";
    const action = typeof properties.permission === "string" ? properties.permission
      : typeof properties.type === "string" ? properties.type
      : typeof properties.action === "string" ? properties.action : "";
    base.session_id = typeof properties.sessionID === "string" ? properties.sessionID : sessionId;
    base.data = {
      id,
      session_id: base.session_id,
      action,
      title: typeof properties.title === "string" ? properties.title.slice(0, 300) : "",
      pattern: alwaysScope,
      requested_patterns: requestedPatterns,
      tool,
      call_id: typeof properties.callID === "string" ? properties.callID
        : typeof properties.call_id === "string" ? properties.call_id : null,
      metadata: metadata.value,
      redacted: metadata.redacted,
      created: properties.time && Number.isFinite(properties.time.created)
        ? new Date(properties.time.created).toISOString() : null,
    };
  } else if (raw.type === "permission.replied") {
    // Real V1 Event.Replied: {sessionID, requestID, reply}. permissionID and
    // response remain only as compatibility fallbacks at this boundary.
    const permissionId = typeof properties.requestID === "string" ? properties.requestID
      : typeof properties.requestId === "string" ? properties.requestId
      : typeof properties.permissionID === "string" ? properties.permissionID
      : typeof properties.permissionId === "string" ? properties.permissionId
      : typeof properties.id === "string" ? properties.id : "";
    const response = typeof properties.reply === "string" ? properties.reply
      : typeof properties.response === "string" ? properties.response : "";
    base.data = {
      permission_id: permissionId,
      response: response.slice(0, 40),
    };
  } else if (raw.type === "session.error") {
    base.data = {
      name: properties.error && typeof properties.error.name === "string" ? properties.error.name.slice(0, 80) : "error",
      message: errorMessage(properties.error, "OpenCode session error").slice(0, 300),
    };
  } else if (raw.type === "session.status") {
    base.data = { status: properties.status && typeof properties.status.type === "string" ? properties.status.type : "" };
  } else if (raw.type === "session.idle") {
    base.data = {};
  } else if (!raw.type.toLowerCase().includes("question")) {
    return null; // Forward only the bounded, relevant event surface.
  }
  return base;
}

export class EventHub {
  constructor({ client, maxEvents = MAX_EVENTS, sleep = (ms) => new Promise((r) => setTimeout(r, ms)) }) {
    this.client = client;
    this.maxEvents = maxEvents;
    this.sleep = sleep;
    this.events = [];
    this.cursor = 0;
    this.waiters = [];
    this.running = false;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.loop().catch(() => {});
  }

  stop() {
    this.running = false;
  }

  async loop() {
    while (this.running) {
      try {
        const result = await this.client.event.subscribe();
        const stream = result && result.stream ? result.stream : result;
        for await (const event of stream) {
          if (!this.running) break;
          this.push(event);
        }
      } catch {
        // Reconnect with a small bounded delay; the bridge reconciles state anyway.
      }
      if (this.running) await this.sleep(1500);
    }
  }

  push(raw) {
    const event = normalizeEvent(raw, this.cursor + 1);
    if (!event) return;
    this.cursor = event.cursor;
    this.events.push(event);
    if (this.events.length > this.maxEvents) this.events.splice(0, this.events.length - this.maxEvents);
    for (const waiter of this.waiters.splice(0)) waiter();
  }

  poll(cursor, timeoutMs) {
    const from = Number.isFinite(cursor) ? cursor : 0;
    const available = () => this.events.filter((event) => event.cursor > from);
    const immediate = available();
    if (immediate.length || timeoutMs <= 0) {
      return Promise.resolve({ events: immediate, cursor: this.cursor });
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        resolve({ events: available(), cursor: this.cursor });
      }, timeoutMs);
      this.waiters.push(() => {
        clearTimeout(timer);
        resolve({ events: available(), cursor: this.cursor });
      });
    });
  }
}
