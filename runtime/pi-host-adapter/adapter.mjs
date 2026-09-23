// Pi session ownership for the native host adapter.
//
// Each Bridge session owns exactly one in-process AgentSession (Pi 0.87.0
// direct Node SDK) created in the isolated PI_CODING_AGENT_DIR profile.
// The map below ties each session id to its owned AgentSession, canonical
// cwd, immutable permission/extension snapshots, preflight correlation,
// suspended permission state, and bounded execution journal. Sessions are
// persistent (SessionManager files under the isolated profile), never
// in-memory; there is no subprocess, no JSONL framing, and no frame-size
// ceiling. Dispose/shutdown marks the session unavailable and every later
// operation fails closed instead of silently creating a replacement.
import { randomUUID } from "node:crypto";

import { ADAPTER_VERSION } from "./config.mjs";
import { isTrustedExtensionUsable, sdkToolLoadout, trustedExtensionPath } from "./config.mjs";
import { READONLY_TOOLS } from "./config.mjs";
import { ExecutionJournal, summaryRecord } from "./executions.mjs";
import {
  canonicalizeEnabledOrder,
  defaultExtensionPolicy,
  extensionRevision,
  readExtensionInventory,
  resolveEnabledExtensionRoots,
  validateExtensionPolicy,
} from "./extensions.mjs";
import { enforcementFingerprint } from "./fingerprint.mjs";
import { resolveSessionDir } from "./paths.mjs";
import {
  SHELL_TOOL,
  canonicalizeExternalRoots,
  canonicalJson,
  evaluateToolCall,
  policyRevision,
  safeDefaultPolicy,
  validatePolicy,
} from "./policy.mjs";
import { isManagedTool, normalizeToolEnd, normalizeToolStart, normalizeToolUpdate } from "./sdk-events.mjs";
import { createSdkSession, listSdkModels } from "./sdk-transport.mjs";
import {
  MARKER_PREFIX,
  OPTION_ALWAYS,
  OPTION_ONCE,
  OPTION_REJECT,
} from "./trusted-permission-extension.mjs";

export const MAX_MESSAGE_TEXT = 8000;
export const MAX_MESSAGES = 100;
export const MAX_TOOLS_PER_MESSAGE = 24;
export const MAX_MODELS = 2000;
export const TITLE_LIMIT = 200;
export const MAX_PREFLIGHTS = 200;
export const MAX_PENDING_PERMISSIONS = 50;
export const MAX_EXECUTIONS_LIMIT = 100;

// Opaque permission marker grammar: EXACTLY prefix + toolCallId, where the
// id is 1..200 non-whitespace characters. No trailing text (tool, action,
// resource, or path) is accepted; anything else fails closed.
export function parseOpaqueMarker(title) {
  if (typeof title !== "string" || !title.startsWith(MARKER_PREFIX)) return "";
  const id = title.slice(MARKER_PREFIX.length);
  if (!id || id.length > 200 || /[\s\0]/.test(id)) return "";
  return id;
}

export class AdapterError extends Error {
  constructor(message, status = 502, code = "runtime_unavailable") {
    super(message);
    this.name = "AdapterError";
    this.status = status;
    this.code = code;
  }
}

function bounded(value, limit) {
  if (typeof value === "string") return value.slice(0, limit);
  if (value === null || value === undefined) return "";
  return String(value).slice(0, limit);
}

function textFromContent(content) {
  // Concatenate visible text blocks only. Thinking/reasoning blocks,
  // images, and tool arguments are never exposed.
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "text" && typeof block.text === "string") {
      parts.push(block.text);
    }
  }
  return parts.join("");
}

function toolsFromContent(content) {
  if (!Array.isArray(content)) return [];
  const tools = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "toolCall" && typeof block.name === "string" && block.name) {
      tools.push(block.name.slice(0, 120));
    }
  }
  return tools.slice(0, MAX_TOOLS_PER_MESSAGE);
}

export function sanitizeMessage(raw, index) {
  if (!raw || typeof raw !== "object") return null;
  const role = bounded(raw.role, 40) || "unknown";
  const text = textFromContent(raw.content ?? raw.text).slice(0, MAX_MESSAGE_TEXT);
  const tools = toolsFromContent(raw.content);
  const created = typeof raw.timestamp === "number" ? raw.timestamp : null;
  const error = typeof raw.errorMessage === "string" && raw.errorMessage
    ? raw.errorMessage.slice(0, 300)
    : null;
  // Completion evidence for assistant messages only, derived from the
  // native stopReason. "stop"/"length" are successful terminal LLM turns;
  // "toolUse" is an intermediate turn; "pending"/"deferred"/"error"/
  // "aborted"/unknown/missing are never completion evidence. Fail closed
  // for unknown future reasons. The raw stopReason itself is not exposed.
  const stopReason = typeof raw.stopReason === "string" ? raw.stopReason : "";
  const terminal = role === "assistant"
    && (stopReason === "stop" || stopReason === "length")
    && created !== null
    && error === null;
  return {
    id: bounded(raw.id, 200) || `msg-${index}`,
    role,
    created,
    completed: terminal ? created : null,
    text,
    tools,
    error,
  };
}

export function sanitizeMessages(raw, limit = 40) {
  const count = Math.max(1, Math.min(Number(limit) || 40, MAX_MESSAGES));
  if (!Array.isArray(raw)) throw new AdapterError("Pi message list was invalid", 502, "runtime_unavailable");
  const rows = raw.slice(-MAX_MESSAGES);
  const result = [];
  for (let index = 0; index < rows.length; index += 1) {
    const message = sanitizeMessage(rows[index], index);
    if (message) result.push(message);
  }
  return result.slice(-count);
}

export function sanitizeModels(raw) {
  if (!Array.isArray(raw)) throw new AdapterError("Pi model list was invalid", 502, "runtime_unavailable");
  const result = [];
  for (const row of raw.slice(0, MAX_MODELS)) {
    if (!row || typeof row !== "object") continue;
    const provider = bounded(row.provider, 120);
    const id = bounded(row.id, 200);
    if (!provider || !id) continue;
    result.push({ provider, id, name: bounded(row.name, 200) || id });
  }
  return result;
}

// Normalize a prompt-async `model` value to {provider, id}, or null when
// absent. Accepts "provider/id" strings and {provider,model|modelId|id}
// objects (including the providerID/modelID aliases).
export function normalizeModelRef(model) {
  if (model === null || model === undefined) return null;
  if (typeof model === "string") {
    const text = model.trim();
    const slash = text.indexOf("/");
    if (slash <= 0 || slash === text.length - 1) {
      throw new AdapterError("Unsupported model selector", 400, "rejected");
    }
    return { provider: text.slice(0, slash).slice(0, 120), id: text.slice(slash + 1).slice(0, 200) };
  }
  if (typeof model === "object") {
    const provider = bounded(model.provider ?? model.providerID ?? model.providerId, 120);
    const id = bounded(model.model ?? model.modelID ?? model.modelId ?? model.id, 200);
    if (!provider || !id) throw new AdapterError("Unsupported model selector", 400, "rejected");
    return { provider, id };
  }
  throw new AdapterError("Unsupported model selector", 400, "rejected");
}

export class PiAdapter {
  constructor({ projectsRoot, agentDir, piUsable = true, piVersion = "",
                createSessionFn = null, listModelsFn = null, onLog = null }) {
    this.projectsRoot = projectsRoot;
    this.agentDir = agentDir;
    this.piUsable = piUsable;
    this.piVersion = piVersion;
    // Injected SDK transport for tests. Production defaults create real
    // in-process AgentSessions against the isolated profile.
    this.createSessionFn = typeof createSessionFn === "function"
      ? createSessionFn
      : (options) => createSdkSession({ agentDir: this.agentDir, ...options });
    this.listModelsFn = typeof listModelsFn === "function"
      ? listModelsFn
      : () => listSdkModels({ agentDir: this.agentDir });
    this.onLog = typeof onLog === "function" ? onLog : null;
    this.sessions = new Map();
    this.instance = randomUUID();
  }

  get sessionCount() {
    return this.sessions.size;
  }

  _entry(sessionId) {
    return this.sessions.get(String(sessionId)) || null;
  }

  // Resolve the requested directory and require exact binding to the stored
  // canonical cwd. Any mismatch fails closed. Disposed sessions are never
  // rebound.
  _boundEntry(sessionId, directory) {
    const entry = this._entry(sessionId);
    if (!entry) return null;
    let canonical;
    try {
      canonical = resolveSessionDir(this.projectsRoot, directory);
    } catch {
      throw new AdapterError("directory does not match the session workspace", 400, "rejected");
    }
    if (canonical !== entry.cwd) {
      throw new AdapterError("directory does not match the session workspace", 400, "rejected");
    }
    if (entry.disposed) {
      throw new AdapterError("Pi session is unavailable", 502, "unavailable");
    }
    return entry;
  }

  // Resolve the Bridge-delivered permission policy into an immutable
  // session snapshot (defense in depth: validated AGAIN here, and the
  // Bridge-supplied revision must equal the recomputed canonical revision).
  // - neither policy nor revision supplied => read-only safe default
  //   (legacy Bridge compatibility only; no trusted extension);
  // - exactly one of policy/revision supplied => fail closed, no session;
  // - supplied policy invalid, v1/v2, or revision malformed => fail closed
  //   (older versions need a coordinated Bridge+adapter upgrade; they are
  //   never silently reinterpreted under v3 semantics);
  // - revision mismatch against the recomputed canonical revision =>
  //   fail closed; a different snapshot revision is never silently accepted.
  // - configured external roots are canonicalized (realpath, existing
  //   directory) here; canonical duplicates fail session creation.
  // v3 has no fixed filesystem denies; ordinary configurable policy applies.
  _snapshotPolicy(options) {
    const raw = options && typeof options === "object" ? options.permission_policy : undefined;
    const suppliedRevision = options && typeof options === "object" ? options.policy_revision : undefined;
    const hasPolicy = raw !== undefined && raw !== null;
    const hasRevision = suppliedRevision !== undefined && suppliedRevision !== null;
    if (!hasPolicy && !hasRevision) {
      const policy = safeDefaultPolicy();
      return {
        policy,
        revision: policyRevision(policy),
        writable: false,
        shellMode: "deny",
        legacy: true,
        effectiveRoots: [],
      };
    }
    if (hasPolicy !== hasRevision) {
      throw new AdapterError("Pi permission policy revision is missing", 400, "invalid_policy");
    }
    if (raw && typeof raw === "object" && (raw.version === 1 || raw.version === 2)) {
      throw new AdapterError(
        `Pi permission policy v${raw.version} is invalid: requires a coordinated Bridge and adapter upgrade`,
        400, "invalid_policy");
    }
    let policy;
    try {
      policy = validatePolicy(raw);
    } catch {
      throw new AdapterError("Pi permission policy is invalid", 400, "invalid_policy");
    }
    if (typeof suppliedRevision !== "string" || !/^[0-9a-fA-F]{64}$/.test(suppliedRevision)) {
      throw new AdapterError("Pi permission policy revision is malformed", 400, "invalid_policy");
    }
    const computed = policyRevision(policy);
    if (suppliedRevision.toLowerCase() !== computed) {
      throw new AdapterError("Pi permission policy revision mismatch", 409, "conflict");
    }
    let effectiveRoots;
    try {
      effectiveRoots = canonicalizeExternalRoots(policy.external_access.roots);
    } catch {
      throw new AdapterError("Pi permission policy external roots are unavailable", 400, "invalid_policy");
    }
    // Store the verified (equal) revision.
    return {
      policy,
      revision: computed,
      writable: policy.write_tools_enabled === true,
      shellMode: policy.shell_mode || "deny",
      legacy: false,
      effectiveRoots,
    };
  }

  // Resolve the Bridge-delivered extension policy into an immutable
  // session snapshot (3C2, extension policy v1, independent of the
  // permission policy):
  // - neither extension_policy nor extension_revision supplied =>
  //   default-empty policy (no third-party extension; 3C1 behavior);
  // - exactly one supplied => fail closed, no session;
  // - invalid policy or malformed revision => fail closed;
  // - revision mismatch against the recomputed canonical revision =>
  //   fail closed;
  // - non-empty enabled list => resolved against the live native
  //   inventory (isolated profile user npm packages); a missing,
  //   invalid, or unresolvable package fails session creation clearly
  //   and is never silently skipped. Inventory unavailable with a
  //   non-empty policy fails rather than persisting an unvalidated
  //   broadened session.
  // Changes apply to NEW sessions only; the snapshot below is immutable.
  _snapshotExtensions(options) {
    const raw = options && typeof options === "object" ? options.extension_policy : undefined;
    const suppliedRevision = options && typeof options === "object" ? options.extension_revision : undefined;
    const hasPolicy = raw !== undefined && raw !== null;
    const hasRevision = suppliedRevision !== undefined && suppliedRevision !== null;
    if (!hasPolicy && !hasRevision) {
      const policy = defaultExtensionPolicy();
      return { policy, revision: extensionRevision(policy), roots: [], snapshot: [] };
    }
    if (hasPolicy !== hasRevision) {
      throw new AdapterError("Pi extension policy revision is missing", 400, "invalid_policy");
    }
    let policy;
    try {
      policy = validateExtensionPolicy(raw);
    } catch {
      throw new AdapterError("Pi extension policy is invalid", 400, "invalid_policy");
    }
    // Canonical order: the revision represents the active set/load order
    // (live inventory/settings order), never arbitrary sender ordering.
    // The Bridge canonicalizes on save; the adapter canonicalizes again
    // here so both sides agree even for a non-canonical sender.
    let inventory = null;
    if (policy.enabled.length) {
      inventory = readExtensionInventory(this.agentDir);
      if (inventory.error && inventory.packages.length === 0) {
        throw new AdapterError(
          "Pi extension inventory is unavailable; refusing an unvalidated policy",
          400, "invalid_policy");
      }
      policy = {
        version: policy.version,
        enabled: canonicalizeEnabledOrder(inventory.packages, policy.enabled),
      };
    }
    if (typeof suppliedRevision !== "string" || !/^[0-9a-fA-F]{64}$/.test(suppliedRevision)) {
      throw new AdapterError("Pi extension policy revision is malformed", 400, "invalid_policy");
    }
    const computed = extensionRevision(policy);
    if (suppliedRevision.toLowerCase() !== computed) {
      throw new AdapterError("Pi extension policy revision mismatch", 409, "conflict");
    }
    if (!policy.enabled.length) {
      return { policy, revision: computed, roots: [], snapshot: [] };
    }
    let resolved;
    try {
      resolved = resolveEnabledExtensionRoots(this.agentDir, policy.enabled);
    } catch (error) {
      throw new AdapterError(
        `Pi extension is unavailable: ${String((error && error.message) || error).slice(0, 160)}`,
        400, "invalid_policy");
    }
    return { policy, revision: computed, roots: resolved.roots, snapshot: resolved.snapshot };
  }

  // Bounded native extension inventory for the isolated profile user npm
  // packages (3C2). No host paths, agentDir, tokens, settings fields,
  // file contents, or dependency lists ever leave this boundary.
  listExtensions() {
    return readExtensionInventory(this.agentDir);
  }

  async createSession(directory, title = "", options = {}) {
    if (!this.piUsable) {
      throw new AdapterError("Pi runtime is unavailable", 502, "unavailable");
    }
    const cwd = resolveSessionDir(this.projectsRoot, directory);
    const snapshot = this._snapshotPolicy(options);
    // 3C2 extension snapshot: validated alongside the permission snapshot
    // (default-empty when the Bridge sends no extension policy). Enabled
    // package roots resolve natively from logical IDs; the Bridge never
    // sends a host path. A disappeared/invalid package fails session
    // creation clearly and is never silently skipped.
    const extSnapshot = this._snapshotExtensions(options);
    // Every managed v3 session loads exactly the package-owned trusted
    // permission extension as an inline factory with this session's
    // immutable policy snapshot closed over -- including read-only
    // sessions, so read/grep/find/ls policy, protected patterns, and
    // external rules are enforced in both modes. write_tools_enabled
    // decides edit/write exposure; shell_mode decides bash exposure (deny
    // removes bash, ask keeps it gated, allow keeps it ungated; no
    // powershell, no command rules). Auto-discovery stays off; only
    // explicitly enabled package roots load. Only the legacy no-policy
    // compatibility path runs without the extension.
    // 3C2: with enabled third-party packages no tools allowlist is passed
    // (the SDK allowlist would hide extension tools) and built-in exposure
    // uses excludeTools instead; the transport activates the intended
    // built-ins explicitly alongside registered extension tools.
    let tools = null;
    let excludeTools = null;
    let extensionPaths = [];
    if (snapshot.legacy) {
      // Legacy no-policy compatibility path: read-only built-ins, no
      // trusted extension -- exactly the old default child loadout.
      tools = [...READONLY_TOOLS];
    } else {
      const trusted = trustedExtensionPath();
      if (!isTrustedExtensionUsable(trusted)) {
        throw new AdapterError("Pi permission session is unavailable", 502, "unavailable");
      }
      const loadout = sdkToolLoadout({
        writable: snapshot.writable,
        shellMode: snapshot.shellMode,
        extensionRoots: extSnapshot.roots,
      });
      tools = loadout.tools;
      excludeTools = loadout.excludeTools;
      extensionPaths = loadout.extensionPaths;
    }
    const holder = {};
    const uiContext = this._makeUiContext(holder);
    let created;
    try {
      created = await this.createSessionFn({
        cwd,
        tools,
        excludeTools,
        extensionPaths,
        policy: snapshot.legacy ? null : snapshot.policy,
        uiContext,
        onEvent: (event) => this._onSdkEvent(holder.entry || null, holder.sessionId || "", event),
        onDiagnostic: (record) => this._sdkDiagnostic(record),
      });
    } catch (error) {
      throw this._asAdapterError(error);
    }
    const session = created && created.session ? created.session : null;
    const sessionId = session && typeof session.sessionId === "string" ? session.sessionId : "";
    if (!session || !sessionId || this.sessions.has(sessionId)) {
      try {
        if (session && typeof session.dispose === "function") session.dispose();
      } catch { /* best effort */ }
      throw new AdapterError("Pi session binding failed", 502, "runtime_unavailable");
    }
    const fingerprint = enforcementFingerprint();
    const entry = {
      session,
      cwd,
      title: bounded(title, TITLE_LIMIT),
      createdAt: Date.now(),
      disposed: false,
      // Immutable permission snapshot for this session. Never mutated
      // after creation; policy changes affect new sessions only.
      permissionPolicy: snapshot.policy,
      policyRevision: snapshot.revision,
      writable: snapshot.writable,
      shellMode: snapshot.shellMode,
      effectiveRoots: snapshot.effectiveRoots,
      // Immutable extension snapshot (3C2). Never mutated after
      // creation; extension policy changes apply to new sessions only.
      // Active sessions never hot-reload extensions.
      extensionPolicy: extSnapshot.policy,
      extensionRevision: extSnapshot.revision,
      extensionSnapshot: extSnapshot.snapshot,
      preflights: new Map(),
      pendingByToolCall: new Map(),
      pendingByPermission: new Map(),
      // Bounded monotonic execution journal for audit (3C1). Retained
      // while the adapter session entry remains owned.
      journal: new ExecutionJournal(),
      enforcementFingerprint: fingerprint.fingerprint,
      // Exact tool loadout granted to the SDK transport (audit/debug via
      // tests; never exposed over HTTP).
      toolLoadout: { tools, excludeTools, extensionPaths: [...extensionPaths] },
    };
    holder.entry = entry;
    holder.sessionId = sessionId;
    this.sessions.set(sessionId, entry);
    return {
      id: sessionId, directory: cwd, title: bounded(title, TITLE_LIMIT),
      policy_revision: snapshot.revision,
      extension_revision: extSnapshot.revision,
      extensions: extSnapshot.snapshot.map((row) => ({ ...row })),
      enforcement_fingerprint: fingerprint.fingerprint,
      fingerprint_modules: [...fingerprint.modules],
      adapter_version: ADAPTER_VERSION,
      pi_version: this.piVersion || "",
    };
  }

  async getSession(directory, sessionId) {
    const entry = this._boundEntry(sessionId, directory);
    if (!entry) return null;
    const state = this._sdkState(entry, String(sessionId));
    return {
      session: { id: String(sessionId), directory: entry.cwd, title: entry.title },
      status: state.isStreaming ? "busy" : "idle",
      state: {
        isStreaming: state.isStreaming,
        messageCount: state.messageCount,
        pendingMessageCount: state.pendingMessageCount,
      },
    };
  }

  async sessionStatus(directory, sessionId) {
    const entry = this._boundEntry(sessionId, directory);
    if (!entry) throw new AdapterError("Session not found", 404, "not_found");
    return this._sdkState(entry, String(sessionId)).isStreaming ? "busy" : "idle";
  }

  // Authoritative in-process state read. The session object is owned by
  // this entry, so no drift check is needed; a disposed entry never
  // reaches here.
  _sdkState(entry, ownedSessionId) {
    let sessionId = "";
    let isStreaming = false;
    let messageCount = null;
    let pendingMessageCount = null;
    try {
      sessionId = entry.session.sessionId;
      isStreaming = Boolean(entry.session.isStreaming);
      const messages = entry.session.messages;
      messageCount = Array.isArray(messages) ? messages.length : null;
      const pending = entry.session.pendingMessageCount;
      pendingMessageCount = typeof pending === "number" ? pending : null;
    } catch (error) {
      throw this._asAdapterError(error);
    }
    if (sessionId && sessionId !== ownedSessionId) {
      entry.disposed = true;
      throw new AdapterError("Pi session is unavailable", 502, "unavailable");
    }
    return { isStreaming, messageCount, pendingMessageCount };
  }

  async promptAsync(directory, sessionId, text, model = null) {
    const entry = this._boundEntry(sessionId, directory);
    if (!entry) throw new AdapterError("Session not found", 404, "not_found");
    const message = bounded(text, 60000);
    if (!message) throw new AdapterError("text is required", 400, "rejected");
    const wanted = normalizeModelRef(model);
    try {
      if (wanted) await this._switchModel(entry.session, wanted);
      await this._promptAccepted(entry.session, message);
    } catch (error) {
      throw this._asAdapterError(error);
    }
    // The accepted prompt means work continues asynchronously in the
    // session; the Bridge polls status/messages/permissions.
    return { accepted: true };
  }

  // Resolve acceptance of one prompt without waiting for the run: the
  // SDK preflightResult hook fires on acceptance (or pre-acceptance
  // rejection) while prompt() itself settles only after the full run.
  // Post-acceptance run failures surface through session events/messages,
  // never through this response.
  _promptAccepted(session, message) {
    return new Promise((resolve, reject) => {
      let done = false;
      const finish = (fn) => (value) => {
        if (!done) {
          done = true;
          fn(value);
        }
      };
      const onAccept = finish(resolve);
      const onReject = finish(reject);
      let run;
      try {
        run = session.prompt(message, {
          source: "rpc",
          preflightResult: (ok) => {
            if (ok) onAccept(true);
            else onReject(new AdapterError("Pi prompt was rejected", 400, "rejected"));
          },
        });
      } catch (error) {
        onReject(error);
        return;
      }
      Promise.resolve(run).then(
        () => {
          // Defensive: a transport that settles without a preflight
          // callback still counts a resolved run as accepted.
          onAccept(true);
        },
        (error) => {
          onReject(error);
        },
      );
      // Post-acceptance settlement must never surface as an unhandled
      // rejection once acceptance already resolved this promise.
      Promise.resolve(run).catch(() => {});
    });
  }

  async _switchModel(session, wanted) {
    let snapshot;
    try {
      snapshot = session.modelRuntime.getAvailableSnapshot();
    } catch (error) {
      throw this._asAdapterError(error);
    }
    const models = sanitizeModels(
      (Array.isArray(snapshot) ? snapshot : []).map((m) => ({
        provider: m && m.provider,
        id: m && m.id,
        name: m && m.name,
      })),
    );
    const matches = models.filter((m) => m.provider === wanted.provider && m.id === wanted.id);
    if (matches.length !== 1) {
      throw new AdapterError("Unsupported model selector", 400, "rejected");
    }
    const target = snapshot.find(
      (m) => m && String(m.provider) === wanted.provider && String(m.id) === wanted.id);
    try {
      await session.setModel(target);
    } catch (error) {
      throw this._asAdapterError(error);
    }
  }

  async messages(directory, sessionId, limit = 40) {
    const entry = this._boundEntry(sessionId, directory);
    if (!entry) throw new AdapterError("Session not found", 404, "not_found");
    let raw;
    try {
      raw = entry.session.messages;
    } catch (error) {
      throw this._asAdapterError(error);
    }
    return sanitizeMessages(raw, limit);
  }

  async listModels(directory) {
    resolveSessionDir(this.projectsRoot, directory);
    if (!this.piUsable) {
      throw new AdapterError("Pi runtime is unavailable", 502, "unavailable");
    }
    // Model/auth inventory is profile-global (isolated agentDir), not
    // session- or workspace-specific, so no session borrow is needed.
    let data;
    try {
      data = await this.listModelsFn();
    } catch (error) {
      throw this._asAdapterError(error);
    }
    return sanitizeModels(data);
  }

  async abortSession(directory, sessionId) {
    const entry = this._boundEntry(sessionId, directory);
    if (!entry) throw new AdapterError("Session not found", 404, "not_found");
    // Abort resolves owned suspended permission selects first so no
    // extension select stays suspended, then aborts the session run.
    // Best effort; never blocks the abort.
    this._resolveOwnedPending(entry, undefined);
    // Normal abort uses session abort and never disposes the session.
    try {
      await entry.session.abort();
    } catch (error) {
      throw this._asAdapterError(error);
    }
    return true;
  }

  // ---------------------------------------------------------- permissions
  // Resolve an entry for permission endpoints. Missing or disposed sessions
  // fail closed as not_found (never an empty success that callers could
  // misread, and never a binding to a dead session).
  _permissionEntry(sessionId, directory) {
    let canonical;
    try {
      canonical = resolveSessionDir(this.projectsRoot, directory);
    } catch {
      throw new AdapterError("directory does not match the session workspace", 400, "rejected");
    }
    const entry = this._entry(sessionId);
    if (!entry || canonical !== entry.cwd) {
      throw new AdapterError("Session not found", 404, "not_found");
    }
    if (entry.disposed) {
      entry.preflights.clear();
      entry.pendingByToolCall.clear();
      entry.pendingByPermission.clear();
      throw new AdapterError("Session not found", 404, "not_found");
    }
    return entry;
  }

  _evaluate(entry, toolName, input) {
    // 3C2: explicitly enabled extension tools are trusted native
    // capabilities, not unknown calls. With at least one enabled
    // package in the immutable session snapshot, any non-managed tool
    // name passes through as an extension capability (credible
    // toolCallId correlation happens in _onToolStart; the trusted
    // extension independently fails closed without one). No permission
    // UI is ever created for them and no deny is synthesized. Without
    // enabled extensions, unknown tools keep the legacy deny.
    if (typeof toolName === "string" && toolName && !isManagedTool(toolName)
        && entry.extensionSnapshot && entry.extensionSnapshot.length) {
      const name = toolName.slice(0, 40);
      return {
        effect: "allow", action: name, tool: name, resource: "", requested: [],
        alwaysPattern: "", grantKey: "",
        reason: "Trusted extension capability",
        code: "extension_tool",
      };
    }
    // v3: no fixed filesystem denies; ordinary configurable policy applies.
    return evaluateToolCall({
      cwd: entry.cwd,
      policy: entry.permissionPolicy,
      toolName,
      input,
      effectiveRoots: entry.effectiveRoots || [],
    });
  }

  // Direct ExtensionUIContext backing for the trusted permission
  // extension. select() suspends the exact tool invocation until Bridge
  // answers once/always/reject; any validation failure returns undefined
  // so the extension blocks the exact call (the in-process equivalent of
  // a cancelled UI response). confirm/input stay fail-closed; notify and
  // status affordances are no-ops outside a TUI.
  _makeUiContext(holder) {
    const uiContext = {
      select: (title, options) => this._onSelect(holder.entry || null, title, options),
      confirm: async () => false,
      input: async () => undefined,
      notify: () => {},
      onTerminalInput: () => () => {},
      setStatus: () => {},
      setWorkingMessage: () => {},
      setWorkingVisible: () => {},
      setWorkingIndicator: () => {},
      setHiddenThinkingLabel: () => {},
      setWidget: () => {},
      setFooter: () => {},
    };
    return uiContext;
  }

  _onSelect(entry, title, options) {
    // Accept only the exact trusted marker/options shape. The marker is
    // opaque: EXACTLY prefix + toolCallId with no trailing text. Anything
    // else is ignored (fire-and-forget UI) by returning undefined, which
    // blocks the exact suspended call without hanging it.
    if (!entry || entry.disposed) return Promise.resolve(undefined);
    if (!Array.isArray(options)) return Promise.resolve(undefined);
    if (this.sessions.get(String(entry.session.sessionId)) !== entry) {
      return Promise.resolve(undefined);
    }
    const toolCallId = parseOpaqueMarker(title);
    if (!toolCallId) return Promise.resolve(undefined);
    if (entry.pendingByToolCall.has(toolCallId)) return Promise.resolve(undefined);
    const preflight = entry.preflights.get(toolCallId);
    if (!preflight) {
      // No exact preflight for this call: never allow.
      return Promise.resolve(undefined);
    }
    const expected = this._expectedUiOptions(entry);
    if (options.length !== expected.length || !expected.every((v, i) => options[i] === v)) {
      return Promise.resolve(undefined);
    }
    // Recompute ask against the immutable snapshot; duplicates, mismatches
    // and non-ask effects fail closed without a pending record.
    const verdict = this._evaluate(entry, preflight.toolName, preflight.input);
    if (verdict.effect !== "ask"
        || verdict.grantKey !== preflight.grantKey
        || verdict.resource !== preflight.resource) {
      return Promise.resolve(undefined);
    }
    if (entry.pendingByPermission.size >= MAX_PENDING_PERMISSIONS) {
      return Promise.resolve(undefined);
    }
    const permissionId = `perm_${randomUUID().replace(/-/g, "").slice(0, 24)}`;
    let resolveSelect = null;
    const suspended = new Promise((resolve) => {
      resolveSelect = resolve;
    });
    const record = {
      id: permissionId,
      sessionId: String(entry.session.sessionId),
      tool: verdict.tool,
      action: verdict.action,
      resource: verdict.resource,
      requested: verdict.requested,
      alwaysPattern: verdict.alwaysPattern,
      toolCallId,
      created: new Date().toISOString(),
      reason: verdict.reason,
      code: verdict.code,
      commandHash: verdict.commandHash || "",
      timeoutMs: verdict.timeoutMs || 0,
      resolve: resolveSelect,
    };
    entry.pendingByToolCall.set(toolCallId, record);
    entry.pendingByPermission.set(permissionId, record);
    return suspended;
  }

  _onSdkEvent(entry, sessionId, event) {
    if (!entry || !event || typeof event !== "object") return;
    if (entry.disposed) return;
    if (this.sessions.get(String(sessionId)) !== entry) return;
    if (event.type === "tool_execution_start" || event.type === "tool_execution_end"
        || event.type === "agent_end") {
      this._sdkDiagnostic({
        stage: "adapter_received",
        sessionId,
        eventType: event.type,
        toolCallId: typeof event.toolCallId === "string" ? event.toolCallId : "",
        pendingToolCount: this._pendingToolCount(entry),
      });
    }
    try {
      if (event.type === "tool_execution_start") {
        const normalized = normalizeToolStart(event);
        if (normalized) this._onToolStart(entry, normalized);
        else this._sdkDiagnostic({ stage: "adapter_event_unusable", sessionId,
          eventType: event.type, toolCallId: "", pendingToolCount: this._pendingToolCount(entry) });
      } else if (event.type === "tool_execution_update") {
        const normalized = normalizeToolUpdate(event);
        if (normalized) this._onToolUpdate(entry, normalized);
      } else if (event.type === "tool_execution_end") {
        const normalized = normalizeToolEnd(event);
        if (normalized) this._onToolEnd(entry, normalized);
        else this._sdkDiagnostic({ stage: "adapter_event_unusable", sessionId,
          eventType: event.type,
          toolCallId: typeof event.toolCallId === "string" ? event.toolCallId.slice(0, 200) : "",
          pendingToolCount: this._pendingToolCount(entry) });
      }
      // Unrelated events (agent_settled, message updates, notify, ...)
      // are ignored.
    } catch {
      // Correlation must never break the session; fail closed per event.
      this._sdkDiagnostic({ stage: "adapter_event_error", sessionId,
        eventType: event.type,
        toolCallId: typeof event.toolCallId === "string" ? event.toolCallId.slice(0, 200) : "",
        pendingToolCount: this._pendingToolCount(entry) });
    }
  }

  _pendingToolCount(entry) {
    try {
      const pending = entry.session.agent?.state?.pendingToolCalls;
      return pending && typeof pending.size === "number" ? pending.size : null;
    } catch {
      return null;
    }
  }

  _sdkDiagnostic(record) {
    if (!this.onLog || !record || typeof record !== "object") return;
    const allowedStages = new Set([
      "extension_dispatch_start", "extension_dispatch_stalled", "extension_dispatch_end",
      "session_subscriber", "adapter_received", "adapter_event_unusable",
      "adapter_event_error", "journal_started", "journal_completed",
      "journal_missing_start",
    ]);
    if (!allowedStages.has(record.stage)) return;
    const fields = {
      stage: record.stage,
      session_id: String(record.sessionId || "").slice(0, 200),
      event_type: String(record.eventType || "").slice(0, 80),
      tool_call_id: String(record.toolCallId || "").slice(0, 200),
      pending_tool_count: Number.isSafeInteger(record.pendingToolCount)
        ? Math.max(0, record.pendingToolCount) : null,
    };
    if (typeof record.journalState === "string") {
      fields.journal_state = record.journalState.slice(0, 20);
    }
    if (Number.isSafeInteger(record.journalUpdateSeq)) {
      fields.journal_update_seq = Math.max(0, record.journalUpdateSeq);
    }
    if (Number.isFinite(record.durationMs)) {
      fields.duration_ms = Math.max(0, Math.floor(record.durationMs));
    }
    const level = record.stage === "extension_dispatch_stalled"
      || record.stage === "adapter_event_error" || record.stage === "journal_missing_start"
      ? "WARNING" : "INFO";
    try { this.onLog(level, "pi-adapter", "sdk_tool_event_trace", fields); } catch { /* no-op */ }
  }

  _onToolStart(entry, message) {
    const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
    const toolName = typeof message.toolName === "string" ? message.toolName : "";
    if (!toolCallId || toolCallId.length > 200 || !toolName) return;
    // Normalized bounded permission metadata from the SDK event layer
    // (never raw tool payloads). Defensive: anything else evaluates as
    // malformed.
    // {path: null} marks a present-but-invalid path so evaluation denies
    // instead of defaulting to the workspace root. Bash carries ONLY the
    // bounded exact command plus verified timeout ({command, timeoutMs});
    // a null bash input marks a malformed/truncated command so shell
    // evaluation fails closed instead of judging a different command.
    const rawInput = message.input;
    const input = rawInput && typeof rawInput === "object" && !Array.isArray(rawInput)
      ? rawInput
      : null;
    const verdict = this._evaluate(entry, toolName, input);
    if (entry.preflights.size >= MAX_PREFLIGHTS) {
      const oldest = entry.preflights.keys().next();
      if (!oldest.done) entry.preflights.delete(oldest.value);
    }
    // Exact preflight for this toolCallId: validated name + args snapshot.
    // For bash the grant scope is the exact command hash + timeout.
    entry.preflights.set(toolCallId, {
      toolName,
      input,
      effect: verdict.effect,
      resource: verdict.resource,
      grantKey: verdict.grantKey,
      alwaysPattern: verdict.alwaysPattern,
      commandHash: verdict.commandHash || "",
      timeoutMs: verdict.timeoutMs || 0,
    });
    // Separate audit journal (bounded evidence, never raw objects).
    try {
      const auditInput = message.auditInput && typeof message.auditInput === "object"
        ? message.auditInput : {};
      const record = entry.journal.start({
        toolCallId, tool: toolName, inputSummary: auditInput,
        permissionEffect: verdict.effect,
      });
      this._sdkDiagnostic({ stage: "journal_started", sessionId: entry.session.sessionId,
        eventType: "tool_execution_start", toolCallId,
        pendingToolCount: this._pendingToolCount(entry),
        journalState: record?.state || "missing",
        journalUpdateSeq: record?.update_seq ?? null });
    } catch { /* journal must never break correlation */ }
  }

  _onToolUpdate(entry, message) {
    const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
    if (!toolCallId) return;
    try {
      const preview = message.auditUpdate && typeof message.auditUpdate === "object"
        ? message.auditUpdate : null;
      if (preview) entry.journal.update({ toolCallId, resultPreview: preview });
    } catch { /* never break the session */ }
  }

  _onToolEnd(entry, message) {
    const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
    if (!toolCallId) return;
    entry.preflights.delete(toolCallId);
    try {
      const auditResult = message.auditResult && typeof message.auditResult === "object"
        ? message.auditResult : { is_error: Boolean(message.isError) };
      const record = entry.journal.end({
        toolCallId,
        resultSummary: auditResult,
        isError: Boolean(message.isError ?? auditResult.is_error),
      });
      this._sdkDiagnostic({
        stage: record ? "journal_completed" : "journal_missing_start",
        sessionId: entry.session.sessionId,
        eventType: "tool_execution_end",
        toolCallId,
        pendingToolCount: this._pendingToolCount(entry),
        journalState: record?.state || "missing",
        journalUpdateSeq: record?.update_seq ?? null,
        durationMs: record?.duration_ms ?? null,
      });
    } catch { /* journal must never break correlation */ }
  }

  // Token-authenticated exact-session execution journal read on the
  // UPDATE cursor (after=<update_seq>). Returns current snapshots whose
  // update_seq > after ordered by update_seq, plus next/head/oldest.
  // Evicted update history reports audit_gap/cursor_too_old, never
  // silent completeness.
  async readExecutions(directory, sessionId, { after = 0, limit = 50 } = {}) {
    const entry = this._permissionEntry(sessionId, directory);
    const result = entry.journal.read({ after, limit });
    return {
      updates: result.updates,
      summaries: result.updates.map((r) => summaryRecord(r)),
      next: result.next,
      head: result.head,
      oldest: result.oldest,
      audit_gap: result.audit_gap,
      cursor_too_old: result.cursor_too_old,
    };
  }

  executionHead(directory, sessionId) {
    const entry = this._permissionEntry(sessionId, directory);
    return entry.journal.head;
  }

  _expectedUiOptions(entry) {
    return entry.permissionPolicy.allow_session_always
      ? [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT]
      : [OPTION_ONCE, OPTION_REJECT];
  }

  _publicPending(record) {
    // Bounded public record only: no raw args, file contents, reasoning,
    // tokens, or environment. For bash the resource/requested carry the
    // exact bounded command + verified timeout detail (ask flow); file
    // tools carry the bounded target. always_pattern stays exact-command
    // scoped for bash (hash + timeout).
    const isBash = String(record.tool) === SHELL_TOOL;
    const resourceLimit = isBash ? 16384 : 400;
    const requestedLimit = isBash ? 16384 : 400;
    // Canonical owner key is session_id (the Bridge normalizer
    // requires it for strict session scoping).
    // The legacy session alias is retained with the identical bounded
    // value so older readers keep working; both keys always agree.
    const out = {
      session: String(record.sessionId).slice(0, 200),
      session_id: String(record.sessionId).slice(0, 200),
      id: String(record.id).slice(0, 200),
      tool: String(record.tool).slice(0, 40),
      action: String(record.action).slice(0, 120),
      resource: String(record.resource).slice(0, resourceLimit),
      requested: (Array.isArray(record.requested) ? record.requested : []).slice(0, 8)
        .map((v) => String(v).slice(0, requestedLimit)),
      always_pattern: String(record.alwaysPattern || "").slice(0, 400),
      tool_call_id: String(record.toolCallId).slice(0, 200),
      created: String(record.created).slice(0, 60),
      metadata: {
        reason: String(record.reason || "").slice(0, 200),
        code: String(record.code || "").slice(0, 80),
      },
    };
    if (isBash) {
      if (record.commandHash) out.command_sha256 = String(record.commandHash).slice(0, 64);
      if (record.timeoutMs) out.timeout_ms = Number(record.timeoutMs) || 0;
    }
    return out;
  }

  async listPermissions(directory, sessionId) {
    const entry = this._permissionEntry(sessionId, directory);
    return [...entry.pendingByPermission.values()].map((record) => this._publicPending(record));
  }

  async respondPermission(directory, sessionId, permissionId, response) {
    if (response !== "once" && response !== "always" && response !== "reject") {
      throw new AdapterError("response must be once, always or reject", 400, "rejected");
    }
    const entry = this._permissionEntry(sessionId, directory);
    const pid = String(permissionId || "");
    const record = entry.pendingByPermission.get(pid);
    if (!record || record.sessionId !== String(sessionId)) {
      throw new AdapterError("Permission not found", 404, "not_found");
    }
    if (response === "always" && !entry.permissionPolicy.allow_session_always) {
      throw new AdapterError("Always-allow is disabled by policy", 400, "rejected");
    }
    // The pending record is only honored while the EXACT original
    // preflight still exists and re-evaluates identically. A missing or
    // changed preflight fails closed: once/always can never approve
    // without it. A rejected stale wait still resumes the suspended call
    // as rejected so nothing hangs.
    const preflight = entry.preflights.get(record.toolCallId);
    const verdict = preflight ? this._evaluate(entry, preflight.toolName, preflight.input) : null;
    const fresh = Boolean(
      preflight && verdict
      && verdict.effect === "ask"
      && verdict.grantKey === preflight.grantKey
      && verdict.resource === record.resource);
    if (!fresh) {
      this._takePending(entry, record);
      try {
        record.resolve(undefined);
      } catch { /* best effort */ }
      if (response === "reject") {
        return { ok: true, decision: "reject" };
      }
      throw new AdapterError("Permission scope changed; failing closed", 409, "conflict");
    }
    let value;
    if (response === "once") value = OPTION_ONCE;
    else if (response === "always") value = OPTION_ALWAYS;
    else value = undefined;
    this._takePending(entry, record);
    // Link the permission decision to the execution journal by toolCallId
    // when positively known; never guess session-grant decisions (only
    // the exact call's once/always/reject is recorded here; the trusted
    // extension owns in-memory exact-grant short-circuits separately).
    try {
      entry.journal.setPermissionDecision(record.toolCallId, response);
    } catch { /* best effort */ }
    try {
      record.resolve(value);
    } catch { /* best effort */ }
    return { ok: true, decision: response };
  }

  _takePending(entry, record) {
    entry.pendingByToolCall.delete(record.toolCallId);
    entry.pendingByPermission.delete(record.id);
  }

  // Resolve every owned suspended select with the given value (undefined
  // cancels). Used by abort/shutdown; abort never reports an approval.
  _resolveOwnedPending(entry, value) {
    const pendings = [...entry.pendingByToolCall.values()];
    entry.pendingByToolCall.clear();
    entry.pendingByPermission.clear();
    for (const record of pendings) {
      try {
        record.resolve(value);
      } catch { /* best effort */ }
    }
  }

  _asAdapterError(error) {
    if (error instanceof AdapterError) return error;
    if (error && error.code === "rejected") {
      return new AdapterError(String(error.message || "rejected"), 400, "rejected");
    }
    if (error instanceof Error) {
      const message = String(error.message || "Pi runtime is unavailable").slice(0, 300);
      const status = typeof error.status === "number" ? error.status : 502;
      const code = typeof error.code === "string" ? error.code : "runtime_unavailable";
      return new AdapterError(message, status, code);
    }
    return new AdapterError("Pi runtime is unavailable", 502, "runtime_unavailable");
  }

  // Dispose every owned session. Disposal is synchronous in the SDK;
  // journals are marked interrupted and suspended selects resume as
  // rejected so nothing hangs.
  async shutdown() {
    const entries = [...this.sessions.values()];
    this.sessions.clear();
    for (const entry of entries) {
      entry.disposed = true;
      try {
        entry.journal.markInterrupted();
      } catch { /* best effort */ }
      this._resolveOwnedPending(entry, undefined);
      try {
        entry.preflights.clear();
      } catch { /* best effort */ }
      try {
        const result = entry.session.dispose();
        if (result && typeof result.then === "function") await result;
      } catch { /* best effort */ }
    }
  }
}
