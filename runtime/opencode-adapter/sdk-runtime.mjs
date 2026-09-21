// Narrow @opencode-ai/sdk client wrapper for the private adapter sidecar.
//
// This module is the ONLY place that imports the OpenCode SDK. It deliberately
// uses createOpencodeClient() against an externally managed base URL and never
// imports or calls createOpencode()/createOpencodeServer(): Workspace Bridge
// must never start, supervise or package an OpenCode server.
import { createOpencodeClient } from "@opencode-ai/sdk";
import { createOpencodeClient as createOpencodeV2Client } from "@opencode-ai/sdk/v2";

import { emit as oplogEmit, errorCode as oplogErrorCode } from "./oplog.mjs";

export const ADAPTER_VERSION = "0.1.10";

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

export function buildPermissionClient({ baseUrl, username, password, createClient = createOpencodeV2Client }) {
  // Official pending-permission listing surface in @opencode-ai/sdk 1.18.31:
  // the v2 namespace client exposes permission.list({query: {directory}})
  // (GET /permission) returning Array<PermissionRequest>. The default v1
  // client has no listing method, so this second client is required. It
  // shares the same base URL/auth and is read-only in practice: only .list
  // is ever called through it.
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

// V2 snapshot/reply surfaces are unavailable on older servers: the generated
// client throws SdkError 404/405/501, or the client's own interceptor rejects
// with "not supported by this version" (text/html from a server that does
// not serve the /api/session/.../permission or .../question routes). Only
// these narrow signals may trigger the V1 compatibility fallback; every
// other error fails closed and is never converted to [].
function isV2Unsupported(error) {
  if (!error) return false;
  if (error instanceof SdkError
      && (error.status === 404 || error.status === 405 || error.status === 501)) return true;
  const message = typeof error.message === "string" ? error.message : "";
  if (/not supported by this version/i.test(message)) return true;
  return false;
}

export class SdkRuntime {
  constructor({ client, permissionClient = null, clock = () => new Date().toISOString() }) {
    this.client = client;
    this.permissionClient = permissionClient;
    this.clock = clock;
    this.lastVersion = null;
    // Last pending-list source used for operational logging: "v2" (primary
    // session-scoped V2 snapshot) or "v1" (compatibility fallback). Never
    // permission contents.
    this.lastPermissionSource = null;
    // Same source tracking for the pending-question snapshot (verified V2
    // session-scoped primary, verified V1 global fallback filtered by
    // exact session). Never question contents.
    this.lastQuestionSource = null;
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

  async respondPermission(directory, id, permissionID, response, generation = "v1") {
    if (!["once", "always", "reject"].includes(response)) {
      throw new SdkError("Permission response must be once, always or reject", 400, "rejected");
    }
    if (generation !== "v1" && generation !== "v2") {
      throw new SdkError("Unknown permission generation for reply routing", 400, "rejected");
    }
    // Pass the decision through unchanged. "always" approves OpenCode's own
    // proposed scope; the adapter never rewrites or broadens it.
    if (generation === "v2") {
      // Verified V2 session-scoped reply (installed @opencode-ai/sdk
      // 1.18.31): client.v2.session.permission.reply ->
      // POST /api/session/{sessionID}/permission/{requestID}/reply with
      // body {reply: once|always|reject}. The native server answers 204
      // (no content) on success.
      const target = this.client && this.client.v2 && this.client.v2.session
        ? this.client.v2.session.permission : null;
      if (!target || typeof target.reply !== "function") {
        throw new SdkError("V2 permission reply is not available from the installed runtime");
      }
      unwrap(await target.reply({
        path: { sessionID: id, requestID: permissionID }, body: { reply: response },
      }), { fallback: "Permission response failed" });
      return true;
    }
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

  async listPendingPermissions(directory, sessionId) {
    // Narrow recovery read for a missed permission ask: return the canonical
    // pending permissions for exactly one session.
    //
    // Primary source on current OpenCode (verified against installed
    // @opencode-ai/sdk 1.18.31): the V2 session-scoped snapshot
    // client.v2.session.permission.list({path: {sessionID}}) ->
    // GET /api/session/{sessionID}/permission, returning
    // 200 {data: Array<PermissionV2Request>}. Its result is authoritative
    // for discovery: a successful empty snapshot never falls back to V1.
    //
    // Compatibility fallback: the V1 global surface
    // permissionClient.permission.list({query: {directory}}) ->
    // GET /permission, returning Array<PermissionV1.Request>. Used ONLY when
    // the V2 snapshot is unavailable/unsupported (missing method, 404, 501,
    // or the client's explicit "not supported by this version" rejection),
    // never when V2 successfully returns empty.
    //
    // Transport/API failures and malformed responses throw and must never be
    // converted to [] by callers: an error is not evidence that nothing is
    // pending. Never scrape TUI output, internal DB/state files, private
    // endpoints, or shell text.
    if (typeof sessionId !== "string" || !sessionId) {
      throw new SdkError("A session id is required to list pending permissions", 400, "rejected");
    }
    const v2Target = this.client && this.client.v2 && this.client.v2.session
      ? this.client.v2.session.permission : null;
    if (v2Target && typeof v2Target.list === "function") {
      try {
        const data = unwrap(
          await v2Target.list({ path: { sessionID: sessionId } }),
          { fallback: "Permission list failed" });
        // The V2 session-permission list answers 200 {data: [...]}; accept a
        // bare array only as a forward-compatible tolerance, never as a
        // reason to consult another source.
        const rows = Array.isArray(data) ? data
          : data && Array.isArray(data.data) ? data.data : null;
        if (rows === null) {
          throw new SdkError("Permission list response was invalid");
        }
        const out = [];
        for (const item of rows) {
          const normalized = normalizePermissionV2Request(item, sessionId);
          // Strict session scoping: malformed entries and entries for
          // another session are dropped here and must never be assigned to
          // this run.
          if (!normalized.id || normalized.session_id !== sessionId) continue;
          out.push(normalized);
        }
        this.lastPermissionSource = "v2";
        return out;
      } catch (error) {
        if (!isV2Unsupported(error)) throw error;
        // Fall through to the V1 compatibility listing below.
      }
    }
    if (!this.permissionClient || !this.permissionClient.permission
        || typeof this.permissionClient.permission.list !== "function") {
      throw new SdkError("Pending permission listing is not available from the installed runtime");
    }
    const data = unwrap(
      await this.permissionClient.permission.list({ query: { directory } }),
      { fallback: "Permission list failed" });
    if (!Array.isArray(data)) {
      throw new SdkError("Permission list response was invalid");
    }
    const out = [];
    for (const item of data) {
      const normalized = normalizePermissionRequest(item, null);
      // Strict session scoping: malformed entries and entries for another
      // session are dropped here and must never be assigned to this run.
      if (!normalized.id || normalized.session_id !== sessionId) continue;
      out.push(normalized);
    }
    this.lastPermissionSource = "v1";
    return out;
  }

  async listPendingQuestions(directory, sessionId) {
    // Narrow recovery read for a missed question ask: return the official
    // pending questions for exactly one session.
    //
    // Primary source on current OpenCode (verified against installed
    // @opencode-ai/sdk 1.18.31): the V2 session-scoped snapshot
    // client.v2.session.question.list({path: {sessionID}}) ->
    // GET /api/session/{sessionID}/question, returning
    // 200 {data: Array<QuestionV2Request>}. Its result is authoritative
    // for discovery: a successful empty snapshot never falls back to V1.
    //
    // Compatibility fallback (verified against the same installed SDK):
    // the V1 global surface permissionClient.question.list({query:
    // {directory}}) -> GET /question, returning 200
    // Array<QuestionRequest> where QuestionRequest = {id, sessionID,
    // questions, tool?: {messageID, callID}}. Every item carries its
    // exact owning sessionID, so client-side exact-session filtering is
    // explicit and reliable. Used ONLY when the V2 snapshot is
    // unavailable/unsupported (missing method, 404, 405, 501, or the
    // client's explicit "not supported by this version" rejection),
    // never when V2 successfully returns empty.
    //
    // Transport/API failures and malformed responses throw and must never be
    // converted to [] by callers: an error is not evidence that nothing is
    // pending. Never scrape TUI output, internal DB/state files, private
    // endpoints, or infer questions from text. Never infer ownership from
    // text, timestamps, request-id formatting, the active session, or TUI
    // state: only the item's own sessionID binds it.
    if (typeof sessionId !== "string" || !sessionId) {
      throw new SdkError("A session id is required to list pending questions", 400, "rejected");
    }
    const v2Target = this.client && this.client.v2 && this.client.v2.session
      ? this.client.v2.session.question : null;
    if (v2Target && typeof v2Target.list === "function") {
      try {
        const data = unwrap(
          await v2Target.list({ path: { sessionID: sessionId } }),
          { fallback: "Question list failed" });
        // The V2 session-question list answers 200 {data: [...]}; accept a
        // bare array only as a forward-compatible tolerance, never as a
        // reason to consult another source.
        const rows = Array.isArray(data) ? data
          : data && Array.isArray(data.data) ? data.data : null;
        if (rows === null) {
          throw new SdkError("Question list response was invalid");
        }
        const out = [];
        for (const item of rows) {
          const normalized = normalizeQuestionV2Request(item, sessionId);
          // Strict session scoping: malformed entries and entries for
          // another session are dropped here and must never be assigned to
          // this run.
          if (!normalized.id || normalized.session_id !== sessionId) continue;
          out.push(normalized);
        }
        this.lastQuestionSource = "v2";
        return out;
      } catch (error) {
        if (!isV2Unsupported(error)) throw error;
        // Fall through to the V1 compatibility listing below.
      }
    }
    const legacy = this.permissionClient && this.permissionClient.question
      ? this.permissionClient.question : null;
    if (!legacy || typeof legacy.list !== "function") {
      throw new SdkError("Pending question listing is not available from the installed runtime");
    }
    const data = unwrap(
      await legacy.list({ query: { directory } }),
      { fallback: "Question list failed" });
    // The V1 global question list answers 200 with a bare array; any
    // other shape is malformed and fails closed, never empty.
    if (!Array.isArray(data)) {
      throw new SdkError("Question list response was invalid");
    }
    const out = [];
    for (const item of data) {
      const normalized = normalizeQuestionV1Request(item, null);
      // Strict session scoping on the item's own sessionID: malformed
      // entries and entries for another session are dropped here and
      // must never be assigned to this run.
      if (!normalized.id || normalized.session_id !== sessionId) continue;
      out.push(normalized);
    }
    this.lastQuestionSource = "v1";
    return out;
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

// Canonical PermissionV1.Request -> bridge permission mapping shared by the
// live permission.asked/permission.updated event path and the pending-list
// recovery path. Both shapes carry the same V1 fields: {id, sessionID,
// permission, patterns (requested), always (exact proposed always scope),
// metadata, tool}. `pattern` is the Approve-always display and reflects
// `always` exactly; `requested_patterns` stays separately reviewable.
export function normalizePermissionRequest(properties, sessionFallback = null) {
  const props = properties && typeof properties === "object" ? properties : {};
  const sessionId = typeof props.sessionID === "string" ? props.sessionID
    : typeof props.sessionId === "string" ? props.sessionId
    : typeof sessionFallback === "string" ? sessionFallback : null;
  const metadata = sanitizeMetadata(props.metadata);
  const requestedPatterns = hasOwn(props, "patterns")
    ? boundedStringArray(props.patterns)
    : boundedStringArray(props.pattern);
  const alwaysScope = hasOwn(props, "always")
    ? boundedStringArray(props.always)
    : boundedStringArray(hasOwn(props, "patterns") ? props.patterns : props.pattern);
  let tool = null;
  if (typeof props.tool === "string") {
    tool = props.tool.slice(0, 200);
  } else if (props.tool && typeof props.tool === "object") {
    tool = sanitizeMetadata(props.tool).value;
  }
  const id = typeof props.id === "string" ? props.id
    : typeof props.requestID === "string" ? props.requestID
    : typeof props.requestId === "string" ? props.requestId : "";
  const action = typeof props.permission === "string" ? props.permission
    : typeof props.type === "string" ? props.type
    : typeof props.action === "string" ? props.action : "";
  // Listed PermissionRequest items carry the call id inside tool
  // ({messageID, callID}) instead of top-level callID; accept it only as a
  // fallback so live-event semantics are unchanged.
  let callId = typeof props.callID === "string" ? props.callID
    : typeof props.call_id === "string" ? props.call_id : null;
  if (!callId && tool && typeof tool === "object" && !Array.isArray(tool)) {
    if (typeof tool.callID === "string") callId = tool.callID.slice(0, 200);
    else if (typeof tool.call_id === "string") callId = tool.call_id.slice(0, 200);
  }
  return {
    id,
    session_id: typeof props.sessionID === "string" ? props.sessionID : sessionId,
    action,
    title: typeof props.title === "string" ? props.title.slice(0, 300) : "",
    pattern: alwaysScope,
    requested_patterns: requestedPatterns,
    tool,
    call_id: callId,
    metadata: metadata.value,
    redacted: metadata.redacted,
    created: props.time && Number.isFinite(props.time.created)
      ? new Date(props.time.created).toISOString() : null,
    generation: "v1",
  };
}

// Canonical PermissionV2.Request -> bridge permission mapping, verified
// against installed @opencode-ai/sdk 1.18.31:
// PermissionV2Request = {id, sessionID, action, resources: Array<string>,
//   save?: Array<string>, metadata?, source?: {type: "tool", messageID,
//   callID}}. `resources` is what is being requested (requested_patterns);
// `save` is OpenCode's exact proposed always scope (pattern) and is never
// synthesized or broadened: when absent, pattern is [] and an "always"
// reply must fail closed upstream. V2 requests carry no title or time, so
// title is "" and created is null. generation is always "v2" so reply
// routing never guesses from request-id formatting.
export function normalizePermissionV2Request(properties, sessionFallback = null) {
  const props = properties && typeof properties === "object" ? properties : {};
  const sessionId = typeof props.sessionID === "string" ? props.sessionID
    : typeof props.sessionId === "string" ? props.sessionId
    : typeof sessionFallback === "string" ? sessionFallback : null;
  const metadata = sanitizeMetadata(props.metadata);
  const requestedPatterns = boundedStringArray(props.resources);
  const alwaysScope = hasOwn(props, "save") ? boundedStringArray(props.save) : [];
  const source = props.source && typeof props.source === "object" ? props.source : null;
  let tool = null;
  if (source) {
    tool = sanitizeMetadata(source).value;
  }
  let callId = null;
  if (source && typeof source.callID === "string") callId = source.callID.slice(0, 200);
  else if (source && typeof source.call_id === "string") callId = source.call_id.slice(0, 200);
  const id = typeof props.id === "string" ? props.id
    : typeof props.requestID === "string" ? props.requestID
    : typeof props.requestId === "string" ? props.requestId : "";
  const action = typeof props.action === "string" ? props.action
    : typeof props.permission === "string" ? props.permission : "";
  return {
    id,
    session_id: sessionId,
    action,
    title: typeof props.title === "string" ? props.title.slice(0, 300) : "",
    pattern: alwaysScope,
    requested_patterns: requestedPatterns,
    tool,
    call_id: callId,
    metadata: metadata.value,
    redacted: metadata.redacted,
    created: props.time && Number.isFinite(props.time.created)
      ? new Date(props.time.created).toISOString() : null,
    generation: "v2",
  };
}

// Canonical QuestionV2.Request -> bridge question mapping, verified
// against installed @opencode-ai/sdk 1.18.31:
// QuestionV2Request = {id, sessionID, questions: Array<QuestionV2Info>,
//   tool?: {messageID, callID}}. Only the request id, owning session,
// question count and tool call reference are kept: question bodies,
// headers, options and answers never cross this boundary and are never
// logged.
export function normalizeQuestionV2Request(properties, sessionFallback = null) {
  const props = properties && typeof properties === "object" ? properties : {};
  const sessionId = typeof props.sessionID === "string" ? props.sessionID
    : typeof props.sessionId === "string" ? props.sessionId
    : typeof sessionFallback === "string" ? sessionFallback : null;
  const id = typeof props.id === "string" ? props.id
    : typeof props.requestID === "string" ? props.requestID
    : typeof props.requestId === "string" ? props.requestId : "";
  const questions = Array.isArray(props.questions) ? props.questions : [];
  const source = props.tool && typeof props.tool === "object" ? props.tool : null;
  let callId = null;
  if (source && typeof source.callID === "string") callId = source.callID.slice(0, 200);
  else if (source && typeof source.call_id === "string") callId = source.call_id.slice(0, 200);
  return {
    id,
    session_id: sessionId,
    question_count: Math.max(0, Math.min(questions.length, 100)),
    call_id: callId,
  };
}

// Canonical legacy QuestionRequest -> bridge question mapping, verified
// against installed @opencode-ai/sdk 1.18.31:
// QuestionRequest = {id, sessionID, questions: Array<QuestionInfo>,
//   tool?: {messageID, callID}} (GET /question, 200 Array<...>). Every
// item carries its exact owning sessionID, so exact-session filtering is
// explicit: the caller retains only items whose sessionID equals the
// requested session. Same minimal shape as the V2 normalizer: only the
// request id, owning session, question count and tool call reference are
// kept; question bodies, options and answers never cross this boundary.
export function normalizeQuestionV1Request(properties, sessionFallback = null) {
  const props = properties && typeof properties === "object" ? properties : {};
  const sessionId = typeof props.sessionID === "string" ? props.sessionID
    : typeof props.sessionId === "string" ? props.sessionId
    : typeof sessionFallback === "string" ? sessionFallback : null;
  const id = typeof props.id === "string" ? props.id
    : typeof props.requestID === "string" ? props.requestID
    : typeof props.requestId === "string" ? props.requestId : "";
  const questions = Array.isArray(props.questions) ? props.questions : [];
  const tool = props.tool && typeof props.tool === "object" ? props.tool : null;
  let callId = null;
  if (tool && typeof tool.callID === "string") callId = tool.callID.slice(0, 200);
  else if (tool && typeof tool.call_id === "string") callId = tool.call_id.slice(0, 200);
  return {
    id,
    session_id: sessionId,
    question_count: Math.max(0, Math.min(questions.length, 100)),
    call_id: callId,
  };
}

// Raw control frames that keep transport alive but can never affect run
// state or transcript. Direct live validation (curl -N against the native
// server event endpoint) produced exactly server.connected and
// server.heartbeat while a real session ran; only these exact verified
// type names count as control.
export const CONTROL_EVENT_TYPES = new Set(["server.connected", "server.heartbeat"]);

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
    const normalized = normalizePermissionRequest(properties, sessionId);
    base.session_id = normalized.session_id;
    base.data = normalized;
  } else if (raw.type === "permission.v2.asked") {
    // Verified V2 ask (installed SDK 1.18.31 EventPermissionV2Asked):
    // properties {id, sessionID, action, resources, save?, metadata?,
    // source?}. Normalized through the dedicated V2 normalizer to the same
    // internal permission.asked shape, carrying generation "v2".
    base.type = "permission.asked";
    const normalized = normalizePermissionV2Request(properties, sessionId);
    base.session_id = normalized.session_id;
    base.data = normalized;
  } else if (raw.type === "permission.v2.replied") {
    // Verified V2 reply (EventPermissionV2Replied): properties
    // {sessionID, requestID, reply: once|always|reject}.
    base.type = "permission.replied";
    const permissionId = typeof properties.requestID === "string" ? properties.requestID
      : typeof properties.requestId === "string" ? properties.requestId
      : typeof properties.id === "string" ? properties.id : "";
    const response = typeof properties.reply === "string" ? properties.reply
      : typeof properties.response === "string" ? properties.response : "";
    base.data = {
      permission_id: permissionId,
      response: response.slice(0, 40),
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
  constructor({ client, maxEvents = MAX_EVENTS, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), onLog = null }) {
    this.client = client;
    this.maxEvents = maxEvents;
    this.sleep = sleep;
    // Optional structured-log sink (adapter passes oplogEmit; tests may
    // capture). Null disables operational logs; health() is unaffected.
    this.onLog = onLog;
    this.events = [];
    this.cursor = 0;
    this.waiters = [];
    this.running = false;
    // Small sanitized subscription health: status only, no event contents
    // or secrets. Transitions count status changes; lastTransition is an
    // ISO timestamp. The in-adapter ring buffer can replay only events it
    // actually observed: reconnect never advances this.cursor past buffered
    // events, and the installed SDK Event.subscribe surface exposes no
    // resumable subscription / last-event-id parameter, so gaps across a
    // disconnected upstream stream cannot be replayed (documented, not invented).
    this._health = {
      status: "starting",
      transitions: 0,
      lastTransition: null,
      consecutiveFailures: 0,
    };
    // Functional event-stream health: bounded counters/timestamps only,
    // never payloads or contents. rawEventCount counts every raw frame
    // observed (including control frames); controlEventCount counts
    // server.connected/heartbeat; functionalEventCount counts frames
    // that normalized to a Bridge-relevant event. A heartbeat-only
    // stream therefore shows raw/control advancing with functional at
    // zero — "transport connected but functionally dead".
    this._eventCounts = {
      raw: 0,
      control: 0,
      functional: 0,
      lastRawAt: null,
      lastFunctionalAt: null,
    };
  }

  _log(level, event, fields) {
    try {
      if (typeof this.onLog === "function") this.onLog(level, "adapter", event, fields);
    } catch {
      // Logging must never break the event stream.
    }
  }

  _setHealth(status) {
    if (!status || status === this._health.status) return;
    const from = this._health.status;
    this._health.status = status;
    this._health.transitions += 1;
    try {
      this._health.lastTransition = new Date().toISOString();
    } catch {
      this._health.lastTransition = null;
    }
    // INFO on every starting -> subscribed -> reconnecting transition:
    // transitions + consecutiveFailures only, never event contents.
    this._log("INFO", "eventhub_transition", {
      status, reason: String(from).slice(0, 80),
      transitions: this._health.transitions,
      consecutive_failures: this._health.consecutiveFailures,
    });
  }

  health() {
    // Sanitized copy: status/transitions/timing plus bounded
    // raw/control/functional counters. Never event contents.
    return {
      status: this._health.status,
      transitions: this._health.transitions,
      lastTransition: this._health.lastTransition,
      consecutiveFailures: this._health.consecutiveFailures,
      rawEventCount: this._eventCounts.raw,
      controlEventCount: this._eventCounts.control,
      functionalEventCount: this._eventCounts.functional,
      lastRawEventAt: this._eventCounts.lastRawAt,
      lastFunctionalEventAt: this._eventCounts.lastFunctionalAt,
    };
  }

  _noteRawEvent(rawType) {
    // Classify BEFORE normalizeEvent: control frames are dropped as
    // unsupported downstream, but they still prove transport liveness.
    // Never logs per-frame (heartbeats would spam); a DEBUG aggregate
    // is emitted every 100 raw frames.
    this._eventCounts.raw += 1;
    try {
      this._eventCounts.lastRawAt = new Date().toISOString();
    } catch {
      this._eventCounts.lastRawAt = null;
    }
    if (CONTROL_EVENT_TYPES.has(rawType)) {
      this._eventCounts.control += 1;
    }
    if (this._eventCounts.raw % 100 === 0) {
      this._log("DEBUG", "eventhub_counts", {
        raw_event_count: this._eventCounts.raw,
        control_event_count: this._eventCounts.control,
        functional_event_count: this._eventCounts.functional,
      });
    }
  }

  _noteFunctionalEvent() {
    this._eventCounts.functional += 1;
    try {
      this._eventCounts.lastFunctionalAt = new Date().toISOString();
    } catch {
      this._eventCounts.lastFunctionalAt = null;
    }
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
        // A confirmed subscription means live asks are observable again.
        this._health.consecutiveFailures = 0;
        this._setHealth("subscribed");
        for await (const event of stream) {
          if (!this.running) break;
          this.push(event);
        }
      } catch (error) {
        // Reconnect with a small bounded delay; the bridge reconciles state anyway.
        // The cursor is never advanced here: only push() (observed events)
        // moves it, so buffered but unpolled events survive reconnect.
        this._health.consecutiveFailures += 1;
        // WARNING with bounded error class/code only, never raw error/body.
        this._log("WARNING", "eventhub_error", {
          status: "reconnecting", code: oplogErrorCode(error),
          consecutive_failures: this._health.consecutiveFailures,
        });
        this._setHealth("reconnecting");
      }
      if (this.running) {
        // A clean stream end (no throw) is also a disconnected subscription.
        if (this._health.status === "subscribed") this._setHealth("reconnecting");
        await this.sleep(1500);
      }
    }
  }

  push(raw) {
    const rawType = raw && typeof raw.type === "string" ? raw.type : "";
    this._noteRawEvent(rawType);
    const event = normalizeEvent(raw, this.cursor + 1);
    if (!event) return;
    this._noteFunctionalEvent();
    this.cursor = event.cursor;
    this.events.push(event);
    if (this.events.length > this.maxEvents) this.events.splice(0, this.events.length - this.maxEvents);
    // INFO when a permission ask/reply is normalized/forwarded:
    // generation + session_id/request_id/action only, never permission
    // objects/patterns/resources/metadata.
    if (event.type === "permission.asked") {
      const data = event.data && typeof event.data === "object" ? event.data : {};
      this._log("INFO", "permission_event", {
        generation: typeof data.generation === "string" ? data.generation : "v1",
        session_id: typeof data.session_id === "string" ? data.session_id : event.session_id,
        request_id: typeof data.id === "string" ? data.id : undefined,
        action: typeof data.action === "string" ? data.action : undefined,
        source: "event",
      });
    } else if (event.type === "permission.replied") {
      const data = event.data && typeof event.data === "object" ? event.data : {};
      this._log("INFO", "permission_event", {
        session_id: event.session_id,
        request_id: typeof data.permission_id === "string" ? data.permission_id : undefined,
        decision: typeof data.response === "string" ? data.response : undefined,
        source: "event",
      });
    }
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
