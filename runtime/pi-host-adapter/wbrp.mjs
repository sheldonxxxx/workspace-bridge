// Pi's Runtime Protocol facade. Ownership and run snapshots live on the Pi
// host; Bridge sees only runtime-neutral records. Security profiles belong
// to the Bridge: every run carries its profile's rules, and the Pi session
// is pinned to them until a later run brings a different revision.
import fs from "node:fs";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";

import { AdapterError, sanitizeModels } from "./adapter.mjs";
import { ADAPTER_VERSION } from "./config.mjs";
import { publicRecord } from "./executions.mjs";
import { fromWire, validatePolicy } from "./policy.mjs";
import { piRelease } from "./release.mjs";

const now = () => new Date().toISOString();
const id = (prefix) => prefix + randomUUID();
const bounded = (value, limit = 200) => String(value ?? "").slice(0, limit);

const INPUT_REQUEST_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;

const RUN_USAGE_FIELDS = new Set(["inputTokens", "cachedInputTokens",
  "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens",
  "totalTokens"]);

function sanitizeRunUsage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = {};
  for (const [key, counter] of Object.entries(value)) {
    if (!RUN_USAGE_FIELDS.has(key)) return null;
    if (typeof counter !== "number" || !Number.isSafeInteger(counter)
        || counter < 0) {
      return null;
    }
    result[key] = counter;
  }
  return Object.keys(result).length ? result : null;
}

function collectRunUsage(adapter, directory, sessionId, base) {
  try {
    if (!adapter || typeof adapter.assistantUsageSince !== "function") return null;
    const start = Number.isSafeInteger(base) && base >= 0 ? base : 0;
    return sanitizeRunUsage(adapter.assistantUsageSince(directory, sessionId, start));
  } catch {
    return null;
  }
}

function runMessageBaseline(adapter, directory, sessionId) {
  try {
    if (!adapter || typeof adapter.sessionMessageCount !== "function") return 0;
    const count = adapter.sessionMessageCount(directory, sessionId);
    return Number.isSafeInteger(count) && count >= 0 ? count : 0;
  } catch {
    return 0;
  }
}

// Bounded sanitized terminal error for idle runs without a successful
// assistant completion. Uses only the sanitized assistant message
// (allowlisted failureKind + credential-redacted bounded error text).
// Never exposes raw provider payloads, prompts, reasoning, tool args,
// environment, credentials, or an unrestricted raw stopReason.
// Falls back to the generic message only when no safe detail exists.
const TERMINAL_KINDS = new Set(["error", "aborted", "incomplete"]);
export function terminalFailureError(last) {
  const fallback = "Pi run ended without a completed assistant message";
  if (!last || typeof last !== "object") return fallback;
  const kind = TERMINAL_KINDS.has(last.failureKind) ? last.failureKind : null;
  const detail = typeof last.error === "string" && last.error
    ? bounded(last.error, 200)
    : "";
  if (!kind && !detail) return fallback;
  const label = kind || "incomplete";
  if (detail) {
    return bounded(
      `Pi run ended (${label}) without a completed assistant message: ${detail}`,
      300);
  }
  return bounded(
    `Pi run ended (${label}) without a completed assistant message`, 300);
}

// The run's profile from its security block. Pi has no configuration of
// its own to fall back to, so a native run is refused.
function runSecurity(body) {
  if (body?.security?.source === "native") {
    throw new AdapterError("Pi runs only under a Bridge security profile", 400,
      "security_unavailable");
  }
  try {
    return fromWire(body?.security);
  } catch (error) {
    throw new AdapterError(bounded(error?.message || "Security profile is invalid"), 400,
      "invalid_profile");
  }
}

function bindingOf(profile) {
  return { source: "profile", profile: { id: profile.id, revision: profile.revision } };
}

export class PiRuntimeProtocol {
  constructor(adapter) {
    this.adapter = adapter;
    this.instanceId = id("pi_instance_");
    this.stateFile = path.join(adapter.agentDir, "wbrp-state.json");
    this.conversations = new Map();
    this.runs = new Map();
    this.activities = new Map();
    this.pending = new Map();
    this.inputSubmitting = new Map();
    this.cancelling = new Set();
    // Events are latency hints with a process-local cursor; snapshots stay
    // the source of truth. The stdio server sets onEvent to push them.
    this.cursor = 0;
    this.onEvent = null;
    this._load();
    adapter.onSessionChange = (sessionId, kind) => this._sessionChanged(sessionId, kind);
  }

  _emit(type, run) {
    if (typeof this.onEvent !== "function") return;
    this.cursor += 1;
    try {
      this.onEvent({ instanceId: this.instanceId, cursor: this.cursor, type,
        conversationId: run.conversationId, runId: run.id });
    } catch { /* a lost hint is recovered by the snapshot sweep */ }
  }

  _sessionChanged(sessionId, kind) {
    const type = kind === "interaction" ? "interaction.pending"
      : kind === "activity" ? "activity.updated" : "run.updated";
    for (const run of this.runs.values()) {
      if (run.nativeId === sessionId && (run.phase === "starting" || run.phase === "active")) {
        this._emit(type, run);
      }
    }
  }

  _load() {
    if (!fs.existsSync(this.stateFile)) return;
    const data = JSON.parse(fs.readFileSync(this.stateFile, "utf8"));
    if (data.version !== 1 || !Array.isArray(data.conversations)
        || !Array.isArray(data.runs) || !Array.isArray(data.activities)) {
      throw new Error("Pi protocol state is incompatible");
    }
    for (const row of data.conversations) {
      // A profile rebind that never finished left its session unproven;
      // such a conversation is not owned again after restart.
      if (row && row.securityRebindPending) continue;
      this.conversations.set(row.id, row);
    }
    for (const row of data.activities) this.activities.set(row.id, row);
    for (const row of data.runs) {
      if (!Array.isArray(row.inputMessages)) row.inputMessages = [];
      // Only metadata is durable. An unacknowledged submission cannot be
      // replayed after restart, and its outcome is unknowable.
      for (const item of row.inputMessages) {
        if (item.status === "queued") item.status = "unknown";
      }
      if (row.phase === "starting" || row.phase === "active") {
        row.phase = "terminal";
        row.activeState = null;
        row.outcome = "interrupted";
        row.error = "adapter_restarted";
        row.updatedAt = now();
        if (row.auditBinding && typeof this.adapter.recoverExecutions === "function") {
          try {
            const conversation = this.conversations.get(row.conversationId);
            if (!conversation || row.auditBinding.sessionId !== row.nativeId
                || row.auditBinding.directory !== conversation.directory
                || !Number.isSafeInteger(row.executionFloor) || row.executionFloor < 0
                || !Number.isSafeInteger(row.executionCursor) || row.executionCursor < row.executionFloor) {
              throw new Error("Execution audit binding changed");
            }
            const snapshot = this.adapter.recoverExecutions(row.auditBinding);
            this._importExecutions(row, {
              updates: snapshot.records.filter((record) => record.update_seq > row.executionCursor)
                .map((record) => publicRecord(record)),
              audit_gap: row.executionCursor < snapshot.evictedThrough,
            });
            row.executionCursor = snapshot.updateSeq;
          } catch {
            this._importExecutions(row, { updates: [], audit_gap: true });
            row.error = "execution_audit_unavailable";
          }
        }
      }
      // Restart retains already-consumed run-scoped usage; malformed
      // persisted usage is dropped so reconciliation never serves it.
      if (row.usage !== undefined) {
        const sanitized = sanitizeRunUsage(row.usage);
        if (sanitized) row.usage = sanitized;
        else delete row.usage;
      }
      if (!Number.isSafeInteger(row.usageBase) || row.usageBase < 0) {
        row.usageBase = 0;
      }
      this.runs.set(row.id, row);
    }
    this._save();
  }

  _save() {
    const data = JSON.stringify({ version: 1,
      conversations: [...this.conversations.values()],
      runs: [...this.runs.values()], activities: [...this.activities.values()] });
    const temp = `${this.stateFile}.${process.pid}.tmp`;
    fs.writeFileSync(temp, data, { mode: 0o600, flag: "w" });
    fs.renameSync(temp, this.stateFile);
  }

  descriptor() {
    if (this.adapter.piUsable === false) {
      throw new AdapterError("Pi runtime is unavailable", 503, "unavailable");
    }
    const value = { protocol: { major: 1, minor: 0 }, runtime: {
      id: "pi", displayName: "Pi", adapterVersion: ADAPTER_VERSION,
      nativeVersion: bounded(this.adapter.piVersion, 80), instanceId: this.instanceId,
    }, features: { models: 1, conversations: 1, runs: 1,
      activities: 1, interactions: 1,
      events: 1, steering: 1, queueing: 1, inputControls: 1 } };
    try {
      value.release = piRelease();
    } catch {
      // Release identity is additive; a local hashing failure must not
      // break the Runtime Protocol descriptor.
    }
    return value;
  }

  async models() {
    // The isolated Pi profile owns this inventory, independent of a session.
    const rows = sanitizeModels(await this.adapter.listModelsFn());
    return { models: rows.map((row) => ({ selector: `${row.provider}/${row.id}`,
      displayName: row.name, inputModalities: ["text"],
      reasoningOptions: row.reasoningOptions || [] })) };
  }

  _conversation(conversationId) {
    const row = this.conversations.get(conversationId);
    if (!row) throw new AdapterError("Conversation not found", 404, "not_found");
    return row;
  }

  _run(runId) {
    const row = this.runs.get(runId);
    if (!row) throw new AdapterError("Run not found", 404, "not_found");
    return row;
  }

  // Load the conversation's Pi session under `profile` (default: the rules
  // it last ran with). A loaded session pinned to another revision is
  // re-opened from its session file, which needs it idle with nothing
  // waiting on approval.
  async _hydrate(row, profile = null) {
    if (!row.rules) {
      throw new AdapterError("This conversation predates Bridge security profiles; start a new one",
        409, "security_source_changed");
    }
    const target = profile || { id: row.profile, revision: row.revision,
      rules: validatePolicy(row.rules) };
    const options = { permission_policy: target.rules, policy_revision: target.revision };
    const loaded = this.adapter.sessions.get(row.sessionId);
    if (loaded && !loaded.disposed) {
      if (loaded.policyRevision === target.revision) return;
      if (await this.adapter.sessionStatus(row.directory, row.sessionId) !== "idle"
          || (await this.adapter.listPermissions(row.directory, row.sessionId)).length) {
        throw new AdapterError("Conversation is busy", 409, "conversation_busy");
      }
      await this._disposeAdapterSession(row.sessionId);
    }
    if (!row.sessionFile || !fs.existsSync(row.sessionFile)) {
      // Pi writes its session file only after a substantive exchange. An
      // unused conversation has no history to recover, so a new empty
      // native session can safely take its place.
      if ([...this.runs.values()].some((run) => run.conversationId === row.id)) {
        throw new AdapterError("Owned Pi session is unavailable", 502, "unavailable");
      }
      const fresh = await this.adapter.createSession(row.directory,
        "Recovered empty Bridge conversation", options);
      row.sessionId = fresh.id;
      row.sessionFile = this.adapter.sessions.get(fresh.id)?.sessionFile || "";
    } else {
      await this.adapter.createSession(row.directory, "Recovered Bridge conversation", {
        ...options, resumeFile: row.sessionFile, expectedSessionId: row.sessionId,
      });
    }
    if (profile) {
      row.profile = profile.id;
      row.revision = profile.revision;
      row.rules = profile.rules;
    }
    this._save();
  }

  async createConversation(body) {
    const workspaceId = body.workspaceId;
    if (typeof workspaceId !== "string" || !workspaceId || workspaceId.length > 100) {
      throw new AdapterError("Invalid workspace", 400, "invalid_arguments");
    }
    const profile = runSecurity(body);
    const session = await this.adapter.createSession(body.directory, "Bridge conversation", {
      permission_policy: profile.rules, policy_revision: profile.revision,
    });
    const sessionFile = this.adapter.sessions.get(session.id)?.sessionFile;
    if (!sessionFile) throw new AdapterError("Pi session file is unavailable", 502, "unavailable");
    const row = { id: id("conv_"), workspaceId, directory: session.directory,
      sessionId: session.id, sessionFile, profile: profile.id,
      revision: profile.revision, rules: profile.rules, createdAt: now() };
    this.conversations.set(row.id, row);
    this._save();
    return this._publicConversation(row, "idle");
  }

  _publicConversation(row, status) {
    return { id: row.id, runtime: "pi", nativeId: row.sessionId,
      workspaceId: row.workspaceId, status,
      securityBinding: bindingOf({ id: row.profile, revision: row.revision }) };
  }

  async conversation(conversationId) {
    const row = this._conversation(conversationId);
    await this._hydrate(row);
    const status = await this.adapter.sessionStatus(row.directory, row.sessionId);
    return this._publicConversation(row, status === "idle" ? "idle" : "active");
  }

  async _disposeAdapterSession(sessionId) {
    try {
      const entry = this.adapter?.sessions?.get(sessionId);
      if (entry) {
        try {
          const result = entry.session?.dispose?.();
          if (result && typeof result.then === "function") await result;
        } catch {}
        try { entry.disposed = true; } catch {}
      }
    } catch {}
    try { this.adapter?.sessions?.delete(sessionId); } catch {}
  }

  async startRun(conversationId, body) {
    const row = this._conversation(conversationId);
    const input = body.input;
    if (!Array.isArray(input) || input.length !== 1
        || input[0]?.type !== "text" || typeof input[0].text !== "string"
        || !input[0].text || input[0].text.length > 60000) {
      throw new AdapterError("Pi requires one text input", 400, "invalid_arguments");
    }
    const clientRunId = body.clientRunId;
    if (clientRunId !== undefined && (typeof clientRunId !== "string"
        || !clientRunId || clientRunId.length > 200)) {
      throw new AdapterError("Invalid client run id", 400, "invalid_arguments");
    }
    const thinkingLevel = body.reasoning ?? null;
    if (thinkingLevel !== null && (typeof thinkingLevel !== "string"
        || !["off", "minimal", "low", "medium", "high", "xhigh", "max"]
          .includes(thinkingLevel))) {
      throw new AdapterError("Invalid thinking level", 400, "invalid_arguments");
    }
    const profile = runSecurity(body);
    const inputHash = createHash("sha256")
      .update(JSON.stringify({ input, model: body.model || null, reasoning: thinkingLevel })).digest("hex");
    if (clientRunId) {
      const previous = [...this.runs.values()].find((run) =>
        run.conversationId === conversationId && run.clientRunId === clientRunId);
      if (previous) {
        if (previous.inputHash !== inputHash) {
          throw new AdapterError("Client run id has different input", 409,
            "idempotency_conflict");
        }
        return this._publicRun(previous);
      }
    }
    if ([...this.runs.values()].some((run) => run.conversationId === conversationId
        && (run.phase === "starting" || run.phase === "active"))) {
      throw new AdapterError("Conversation is busy", 409, "conversation_busy");
    }
    // A continuation runs under the profile it brings, even when that
    // differs from the conversation's previous run.
    await this._hydrate(row, profile);
    if (await this.adapter.sessionStatus(row.directory, row.sessionId) !== "idle") {
      throw new AdapterError("Conversation is busy", 409, "conversation_busy");
    }
    // A continuation run starts a fresh accounting boundary even when it
    // reuses the same AgentSession: only model calls at or after this
    // baseline belong to the new Bridge run.
    const usageBase = runMessageBaseline(this.adapter, row.directory, row.sessionId);
    const executionCursor = this.adapter.executionHead(row.directory, row.sessionId);
    const auditBinding = typeof this.adapter.executionBinding === "function"
      ? this.adapter.executionBinding(row.directory, row.sessionId) : null;
    const run = { id: id("run_"), conversationId, nativeId: row.sessionId,
      clientRunId: clientRunId || null, inputHash,
      inputMessages: [],
      phase: "starting", activeState: null, outcome: null, result: "", error: "",
      model: bounded(body.model, 260), reasoning: thinkingLevel,
      securityBinding: bindingOf(profile),
      executionCursor, executionFloor: executionCursor, auditBinding, usageBase,
      createdAt: now(), updatedAt: now() };
    this.runs.set(run.id, run);
    this._save();
    try {
      await this.adapter.promptAsync(row.directory, row.sessionId, input[0].text,
        body.model || null, thinkingLevel);
      run.phase = "active";
      run.activeState = "running";
    } catch (error) {
      run.phase = "terminal";
      run.outcome = "failed";
      run.error = bounded(error?.message, 300);
      throw error;
    } finally {
      run.updatedAt = now();
      this._save();
      this._emit(run.phase === "terminal" ? "run.completed" : "run.started", run);
    }
    return this._publicRun(run);
  }

  _publicRun(run) {
    const { executionCursor, executionFloor, auditBinding, model, inputHash, usageBase, ...publicRun } = run;
    publicRun.inputMessages = (run.inputMessages || []).map(({ requestId, mode, status }) =>
      ({ requestId, mode, status }));
    if (run.usage !== undefined) {
      const sanitized = sanitizeRunUsage(run.usage);
      if (sanitized) publicRun.usage = sanitized;
    }
    return publicRun;
  }

  async submitInput(runId, mode, body) {
    const run = this._run(runId);
    if (!["steer", "queue"].includes(mode)
        || !body || body.expectedRunId !== runId
        || typeof body.requestId !== "string"
        || !INPUT_REQUEST_ID_RE.test(body.requestId)
        || !Array.isArray(body.input) || body.input.length !== 1
        || body.input[0]?.type !== "text"
        || typeof body.input[0].text !== "string"
        || !body.input[0].text || body.input[0].text.length > 8000) {
      throw new AdapterError("Invalid run input", 400, "invalid_arguments");
    }
    const hash = createHash("sha256").update(JSON.stringify(body.input)).digest("hex");
    const messages = run.inputMessages || (run.inputMessages = []);
    const previous = messages.find((item) => item.requestId === body.requestId);
    if (previous) {
      if (previous.mode !== mode || previous.inputHash !== hash) {
        throw new AdapterError("Request id has different input", 409, "idempotency_conflict");
      }
      return this._publicRun(run);
    }
    if (run.phase !== "active" || this.cancelling.has(runId)) {
      throw new AdapterError("Run is not active", 409, "run_not_active");
    }
    if (messages.length >= 50) {
      throw new AdapterError("Run input history is full", 409, "input_limit");
    }
    const row = this._conversation(run.conversationId);
    const submitting = this.inputSubmitting.get(runId) || 0;
    if (submitting + (this.adapter.pendingInputCount?.(row.directory, row.sessionId) || 0) >= 20) {
      throw new AdapterError("Run input queue is full", 409, "queue_full");
    }
    const item = { requestId: body.requestId, mode, inputHash: hash, status: "queued" };
    messages.push(item);
    this.inputSubmitting.set(runId, submitting + 1);
    try {
      this._save();
    } catch (error) {
      messages.pop();
      if (submitting) this.inputSubmitting.set(runId, submitting);
      else this.inputSubmitting.delete(runId);
      throw error;
    }
    const onStatus = (status) => {
      if (item.status !== "queued") return;
      item.status = status;
      run.updatedAt = now();
      this._save();
    };
    try {
      const result = await this.adapter.submitInput(row.directory, row.sessionId,
        body.input[0].text, mode,
        () => run.phase === "active" && !this.cancelling.has(runId), onStatus);
      if (!result?.accepted && item.status === "queued") item.status = "unknown";
    } catch (error) {
      if (item.status === "queued") item.status = "unknown";
      throw error;
    } finally {
      const remaining = (this.inputSubmitting.get(runId) || 1) - 1;
      if (remaining) this.inputSubmitting.set(runId, remaining);
      else this.inputSubmitting.delete(runId);
      run.updatedAt = now();
      this._save();
    }
    return this._publicRun(run);
  }

  async findRun(conversationId, clientRunId) {
    this._conversation(conversationId);
    const run = [...this.runs.values()].find((item) =>
      item.conversationId === conversationId && item.clientRunId === clientRunId);
    if (!run) throw new AdapterError("Run not found", 404, "not_found");
    return this.run(run.id);
  }

  _importExecutions(run, result) {
    if (result.audit_gap || result.cursor_too_old) {
      const auditId = `act_${createHash("sha256").update(`${run.id}:audit-gap`)
        .digest("hex").slice(0, 24)}`;
      this.activities.set(auditId, { id: auditId, runId: run.id,
        nativeId: "audit-gap", kind: "audit_gap", status: "failed",
        input: { summary: "Pi execution journal has a missing span" },
        result: { auditGap: true }, createdAt: now(), updatedAt: now() });
    }
    for (const item of result.updates) {
      // A later update to an older call never transfers that call to a
      // continuation run. Ownership is its immutable start cursor.
      if (Number.isSafeInteger(run.executionFloor) && item.start_seq <= run.executionFloor) continue;
      const stable = createHash("sha256").update(`${run.id}:${item.tool_call_id}`)
        .digest("hex").slice(0, 24);
      const activityId = `act_${stable}`;
      const kind = item.tool === "bash" ? "command"
        : ["edit", "write"].includes(item.tool) ? "file_change" : "tool_call";
      this.activities.set(activityId, { id: activityId, runId: run.id,
        nativeId: bounded(item.tool_call_id, 200), kind,
        status: item.state === "completed" ? (item.is_error ? "failed" : "completed")
          : item.state === "interrupted" ? "interrupted" : "running",
        input: { summary: bounded(item.tool, 80),
          details: item.input_summary || {},
          permissionEffect: bounded(item.permission_effect, 40),
          permissionDecision: bounded(item.permission_decision, 40) },
        result: item.result_summary || {},
        createdAt: item.started_at || now(), updatedAt: now() });
    }
  }

  async _refresh(run) {
    if (run.phase !== "active") return;
    const row = this._conversation(run.conversationId);
    await this._hydrate(row);
    const pending = await this.adapter.listPermissions(row.directory, row.sessionId);
    const status = await this.adapter.sessionStatus(row.directory, row.sessionId);
    run.activeState = pending.length ? "waiting_interaction" : "running";
    let cursor = run.executionCursor;
    for (let page = 0; page < 20; page += 1) {
      const result = await this.adapter.readExecutions(row.directory, row.sessionId,
        { after: cursor, limit: 100 });
      this._importExecutions(run, result);
      cursor = result.next;
      if (cursor >= result.head || !result.updates.length) break;
    }
    run.executionCursor = cursor;
    // Aggregate native provider usage for exactly this Bridge run. The
    // aggregate replaces the prior snapshot; absent usage preserves the
    // existing snapshot so already-consumed tokens stay visible.
    const partial = collectRunUsage(this.adapter, row.directory, row.sessionId,
      run.usageBase);
    if (partial) run.usage = partial;
    // Terminal classification runs only on truly idle sessions. The
    // adapter's idle already implies not retrying (logical busy is
    // isStreaming || isRetrying), so a transient 429 assistant error
    // during native retry backoff stays active/running here and is never
    // terminal-failed. No Bridge-level prompt/run replay is introduced.
    if (status === "idle" && !pending.length
        && !this.inputSubmitting.get(run.id)
        && !(this.adapter.pendingInputCount?.(row.directory, row.sessionId) || 0)
        && !this.cancelling.has(run.id)) {
      const messages = await this.adapter.messages(row.directory, row.sessionId, 20);
      // messages() is asynchronous; an input or cancellation can enter in
      // that gap. Confirm idle again before committing a terminal outcome.
      const settledStatus = await this.adapter.sessionStatus(row.directory, row.sessionId);
      if (run.phase !== "active" || this.cancelling.has(run.id)
          || this.inputSubmitting.get(run.id)
          || (this.adapter.pendingInputCount?.(row.directory, row.sessionId) || 0)
          || settledStatus !== "idle") {
        run.updatedAt = now();
        this._save();
        return;
      }
      const last = [...messages].reverse().find((message) => message.role === "assistant");
      this.adapter.finalizeInputTickets?.(row.directory, row.sessionId);
      for (const item of run.inputMessages || []) {
        if (item.status === "queued") item.status = "unknown";
      }
      run.phase = "terminal";
      run.activeState = null;
      run.outcome = last?.completed ? "succeeded" : "failed";
      run.result = bounded(last?.text, 20000);
      if (!last?.completed) run.error = terminalFailureError(last);
      const terminal = collectRunUsage(this.adapter, row.directory, row.sessionId,
        run.usageBase);
      if (terminal) run.usage = terminal;
      run.updatedAt = now();
      this._save();
      this._emit("run.completed", run);
      return;
    }
    run.updatedAt = now();
    this._save();
  }

  async run(runId) {
    const run = this._run(runId);
    await this._refresh(run);
    return this._publicRun(run);
  }

  async cancel(runId) {
    const run = this._run(runId);
    if (run.phase === "terminal") return this._publicRun(run);
    this.cancelling.add(runId);
    const row = this._conversation(run.conversationId);
    try {
      await this.adapter.abortSession(row.directory, row.sessionId);
    } finally {
      this.cancelling.delete(runId);
    }
    // Preserve already-consumed native usage on cancelled runs.
    const consumed = collectRunUsage(this.adapter, row.directory, row.sessionId,
      run.usageBase);
    if (consumed) run.usage = consumed;
    run.phase = "terminal";
    run.activeState = null;
    run.outcome = "cancelled";
    for (const item of run.inputMessages || []) {
      if (item.status === "queued") item.status = "unknown";
    }
    run.updatedAt = now();
    this._save();
    this._emit("run.completed", run);
    return this._publicRun(run);
  }

  async interactions(runId) {
    const run = this._run(runId);
    if (run.phase !== "active") return { interactions: [] };
    const row = this._conversation(run.conversationId);
    const pending = await this.adapter.listPermissions(row.directory, row.sessionId);
    const interactions = pending.map((request) => {
      const choices = [
        { id: "once", label: "Allow once", semantic: "approve" },
        { id: "always", label: "Allow for this session", semantic: "approve" },
        { id: "reject", label: "Reject", semantic: "deny" },
      ];
      this.pending.set(request.id, { runId, row });
      return { id: request.id, runId, kind: "choice", state: "pending",
        title: `${bounded(request.tool, 40)}: ${bounded(request.action, 120)}`,
        choices, resource: bounded(request.resource, 400) };
    });
    return { interactions };
  }

  async resolve(interactionId, body) {
    const pending = this.pending.get(interactionId);
    if (!pending || this._run(pending.runId).phase !== "active") {
      throw new AdapterError("Interaction is stale", 409, "interaction_stale");
    }
    if (!["once", "always", "reject"].includes(body.choiceId)) {
      throw new AdapterError("Choice is invalid", 400, "invalid_arguments");
    }
    await this.adapter.respondPermission(pending.row.directory,
      pending.row.sessionId, interactionId, body.choiceId);
    this.pending.delete(interactionId);
    return { id: interactionId, runId: pending.runId, state: "resolved" };
  }

  async activityList(runId) {
    await this.run(runId);
    return { activities: [...this.activities.values()].filter((row) => row.runId === runId) };
  }

  activity(activityId) {
    const row = this.activities.get(activityId);
    if (!row) throw new AdapterError("Activity not found", 404, "not_found");
    return row;
  }
}
