// Pi's Runtime Protocol v1 facade. Ownership, security profiles and run
// snapshots live on the Pi host; Bridge sees only runtime-neutral records.
import fs from "node:fs";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";

import { AdapterError, sanitizeModels } from "./adapter.mjs";
import { enforcementFingerprint } from "./fingerprint.mjs";
import { policyRevision, safeDefaultPolicy, validatePolicy } from "./policy.mjs";
import { SDK_VERSION } from "./sdk-transport.mjs";

const now = () => new Date().toISOString();
const id = (prefix) => prefix + randomUUID();
const bounded = (value, limit = 200) => String(value ?? "").slice(0, limit);

const PROFILE_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/;

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

function bindProfile(policy) {
  const policyRev = policyRevision(policy);
  const revision = createHash("sha256")
    .update(`${policyRev}:${enforcementFingerprint().fingerprint}:${SDK_VERSION}`).digest("hex");
  return { policy, policyRev, revision };
}

function defaultProfiles() {
  const readonly = safeDefaultPolicy();
  const writable = { ...safeDefaultPolicy(), write_tools_enabled: true, shell_mode: "ask" };
  return new Map([
    ["read-only", bindProfile(readonly)],
    ["workspace-write-reviewed", bindProfile(writable)],
  ]);
}

export class PiRuntimeProtocol {
  constructor(adapter) {
    this.adapter = adapter;
    this.instanceId = id("pi_instance_");
    this.stateFile = path.join(adapter.agentDir, "wbrp-state.json");
    this.profilesFile = path.join(adapter.agentDir, "wbrp-profiles.json");
    this.profiles = defaultProfiles();
    this._loadProfiles();
    this.conversations = new Map();
    this.runs = new Map();
    this.activities = new Map();
    this.pending = new Map();
    this._load();
  }

  _load() {
    if (!fs.existsSync(this.stateFile)) return;
    const data = JSON.parse(fs.readFileSync(this.stateFile, "utf8"));
    if (data.version !== 1 || !Array.isArray(data.conversations)
        || !Array.isArray(data.runs) || !Array.isArray(data.activities)) {
      throw new Error("Pi protocol state is incompatible");
    }
    for (const row of data.conversations) this.conversations.set(row.id, row);
    for (const row of data.runs) {
      if (row.phase === "starting" || row.phase === "active") {
        row.phase = "terminal";
        row.activeState = null;
        row.outcome = "interrupted";
        row.error = "adapter_restarted";
        row.updatedAt = now();
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
    for (const row of data.activities) this.activities.set(row.id, row);
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

  _loadProfiles() {
    if (!fs.existsSync(this.profilesFile)) return;
    const data = JSON.parse(fs.readFileSync(this.profilesFile, "utf8"));
    if (data.version !== 1 || !Array.isArray(data.profiles) || data.profiles.length > 98) {
      throw new Error("Pi security profiles are incompatible");
    }
    for (const entry of data.profiles) {
      if (!PROFILE_ID_RE.test(entry.id) || this.profiles.has(entry.id)) {
        throw new Error("Pi security profile ID is invalid");
      }
      const policy = validatePolicy(entry.config);
      this.profiles.set(entry.id, bindProfile(policy));
    }
  }

  _saveProfiles() {
    const profiles = [...this.profiles].filter(([profileId]) =>
      profileId !== "read-only" && profileId !== "workspace-write-reviewed")
      .map(([profileId, value]) => ({ id: profileId, config: value.policy }));
    const temp = `${this.profilesFile}.${process.pid}.tmp`;
    fs.writeFileSync(temp, JSON.stringify({ version: 1, profiles }), { mode: 0o600, flag: "w" });
    fs.renameSync(temp, this.profilesFile);
  }

  descriptor() {
    if (this.adapter.piUsable === false) {
      throw new AdapterError("Pi runtime is unavailable", 503, "unavailable");
    }
    return { protocol: { major: 1, minor: 0 }, runtime: {
      id: "pi", displayName: "Pi", adapterVersion: "1.0.0",
      nativeVersion: bounded(this.adapter.piVersion, 80), instanceId: this.instanceId,
    }, features: { models: 1, conversations: 1, runs: 1,
      activities: 1, interactions: 1 } };
  }

  async models() {
    // The isolated Pi profile owns this inventory, independent of a session.
    const rows = sanitizeModels(await this.adapter.listModelsFn());
    return { models: rows.map((row) => ({ selector: `${row.provider}/${row.id}`,
      displayName: row.name, inputModalities: ["text"],
      reasoningOptions: row.reasoningOptions || [] })) };
  }

  profileList() {
    return { profiles: [...this.profiles].map(([profile, value]) => ({
      id: profile, revision: value.revision,
      config: value.policy,
      mutable: profile !== "read-only" && profile !== "workspace-write-reviewed",
      enforcement: ["trusted-permission-extension", "isolated-agent-profile"],
    })) };
  }

  saveProfile(body) {
    const profileId = body?.id;
    if (typeof profileId !== "string" || !PROFILE_ID_RE.test(profileId)
        || profileId === "read-only" || profileId === "workspace-write-reviewed") {
      throw new AdapterError("Choose a new profile ID using lowercase letters, numbers, _ or -",
        400, "invalid_arguments");
    }
    let policy;
    try { policy = validatePolicy(body.config); }
    catch (error) { throw new AdapterError(error.message, 400, "invalid_arguments"); }
    const previous = this.profiles.get(profileId);
    if ((previous && previous.revision !== body.expectedRevision)
        || (!previous && body.expectedRevision != null)) {
      throw new AdapterError("Security profile changed; reload it", 409, "profile_mismatch");
    }
    if (previous && [...this.runs.values()].some((run) =>
      (run.phase === "starting" || run.phase === "active")
      && this.conversations.get(run.conversationId)?.profile === profileId)) {
      throw new AdapterError("Profile has an active run", 409, "conflict");
    }
    if (!previous && this.profiles.size >= 100) {
      throw new AdapterError("Too many security profiles", 400, "invalid_arguments");
    }
    this.profiles.set(profileId, bindProfile(policy));
    try { this._saveProfiles(); }
    catch (error) {
      if (previous) this.profiles.set(profileId, previous);
      else this.profiles.delete(profileId);
      throw error;
    }
    const value = this.profiles.get(profileId);
    return { id: profileId, revision: value.revision, config: value.policy, mutable: true };
  }

  deleteProfile(profileId) {
    if (profileId === "read-only" || profileId === "workspace-write-reviewed") {
      throw new AdapterError("Built-in profiles cannot be deleted", 409, "conflict");
    }
    const previous = this.profiles.get(profileId);
    if (!previous) throw new AdapterError("Security profile not found", 404, "not_found");
    if ([...this.runs.values()].some((run) =>
      (run.phase === "starting" || run.phase === "active")
      && this.conversations.get(run.conversationId)?.profile === profileId)) {
      throw new AdapterError("Profile has an active run", 409, "conflict");
    }
    this.profiles.delete(profileId);
    try { this._saveProfiles(); }
    catch (error) { this.profiles.set(profileId, previous); throw error; }
    return { deleted: profileId };
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

  async _hydrate(row) {
    const profile = this.profiles.get(row.profile);
    if (!profile || profile.revision !== row.revision) {
      throw new AdapterError("Security profile changed", 409, "profile_mismatch");
    }
    if (this.adapter.sessions.has(row.sessionId)) return;
    if (!row.sessionFile || !fs.existsSync(row.sessionFile)) {
      // Pi writes its session file only after a substantive exchange. An
      // unused conversation has no history to recover, so a new empty
      // native session can safely take its place under the same binding.
      if ([...this.runs.values()].some((run) => run.conversationId === row.id)) {
        throw new AdapterError("Owned Pi session is unavailable", 502, "unavailable");
      }
      const fresh = await this.adapter.createSession(row.directory,
        "Recovered empty Bridge conversation", {
          permission_policy: profile.policy, policy_revision: profile.policyRev,
        });
      row.sessionId = fresh.id;
      row.sessionFile = this.adapter.sessions.get(fresh.id)?.sessionFile || "";
      this._save();
      return;
    }
    await this.adapter.createSession(row.directory, "Recovered Bridge conversation", {
      permission_policy: profile.policy, policy_revision: profile.policyRev,
      resumeFile: row.sessionFile, expectedSessionId: row.sessionId,
    });
  }

  async createConversation(body) {
    const workspaceId = body.workspaceId;
    const requested = body.securityProfile || {};
    const profile = this.profiles.get(requested.id);
    if (typeof workspaceId !== "string" || !workspaceId || workspaceId.length > 100
        || !profile || requested.revision !== profile.revision) {
      throw new AdapterError("Invalid workspace or security profile", 400, "invalid_arguments");
    }
    const session = await this.adapter.createSession(body.directory, "Bridge conversation", {
      permission_policy: profile.policy, policy_revision: profile.policyRev,
    });
    const sessionFile = this.adapter.sessions.get(session.id)?.sessionFile;
    if (!sessionFile) throw new AdapterError("Pi session file is unavailable", 502, "unavailable");
    const row = { id: id("conv_"), workspaceId, directory: session.directory,
      sessionId: session.id, sessionFile, profile: requested.id,
      revision: profile.revision, createdAt: now() };
    this.conversations.set(row.id, row);
    this._save();
    return this._publicConversation(row, "idle");
  }

  _publicConversation(row, status) {
    return { id: row.id, runtime: "pi", nativeId: row.sessionId,
      workspaceId: row.workspaceId, status,
      securityProfile: { id: row.profile, revision: row.revision } };
  }

  async conversation(conversationId) {
    const row = this._conversation(conversationId);
    await this._hydrate(row);
    const status = await this.adapter.sessionStatus(row.directory, row.sessionId);
    return this._publicConversation(row, status === "idle" ? "idle" : "active");
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
    await this._hydrate(row);
    if (await this.adapter.sessionStatus(row.directory, row.sessionId) !== "idle") {
      throw new AdapterError("Conversation is busy", 409, "conversation_busy");
    }
    if (this.profiles.get(row.profile)?.revision !== row.revision) {
      throw new AdapterError("Security profile changed", 409, "profile_mismatch");
    }
    // A continuation run starts a fresh accounting boundary even when it
    // reuses the same AgentSession: only model calls at or after this
    // baseline belong to the new Bridge run.
    const usageBase = runMessageBaseline(this.adapter, row.directory, row.sessionId);
    let executionCursor = 0;
    try {
      executionCursor = this.adapter.executionHead(row.directory, row.sessionId);
    } catch {
      executionCursor = 0;
    }
    const run = { id: id("run_"), conversationId, nativeId: row.sessionId,
      clientRunId: clientRunId || null, inputHash,
      phase: "starting", activeState: null, outcome: null, result: "", error: "",
      model: bounded(body.model, 260), reasoning: thinkingLevel,
      executionCursor, usageBase,
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
    }
    return this._publicRun(run);
  }

  _publicRun(run) {
    const { executionCursor, model, inputHash, usageBase, ...publicRun } = run;
    if (run.usage !== undefined) {
      const sanitized = sanitizeRunUsage(run.usage);
      if (sanitized) publicRun.usage = sanitized;
    }
    return publicRun;
  }

  async findRun(conversationId, clientRunId) {
    this._conversation(conversationId);
    const run = [...this.runs.values()].find((item) =>
      item.conversationId === conversationId && item.clientRunId === clientRunId);
    if (!run) throw new AdapterError("Run not found", 404, "not_found");
    return this.run(run.id);
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
      if (result.audit_gap || result.cursor_too_old) {
        const auditId = `act_${createHash("sha256").update(`${run.id}:audit-gap`)
          .digest("hex").slice(0, 24)}`;
        this.activities.set(auditId, { id: auditId, runId: run.id,
          nativeId: "audit-gap", kind: "audit_gap", status: "failed",
          input: { summary: "Pi execution journal has a missing span" },
          result: { auditGap: true }, createdAt: now(), updatedAt: now() });
      }
      for (const item of result.updates) {
        const stable = createHash("sha256").update(`${run.id}:${item.tool_call_id}`)
          .digest("hex").slice(0, 24);
        const activityId = `act_${stable}`;
        const kind = item.tool === "bash" ? "command"
          : ["edit", "write"].includes(item.tool) ? "file_change" : "tool_call";
        const activity = { id: activityId, runId: run.id,
          nativeId: bounded(item.tool_call_id, 200), kind,
          status: item.state === "completed" ? (item.is_error ? "failed" : "completed")
            : item.state === "interrupted" ? "interrupted" : "running",
          input: { summary: bounded(item.tool, 80),
            details: item.input_summary || {},
            permissionEffect: bounded(item.permission_effect, 40),
            permissionDecision: bounded(item.permission_decision, 40) },
          result: item.result_summary || {},
          createdAt: item.created || now(), updatedAt: now() };
        this.activities.set(activityId, activity);
      }
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
    if (status === "idle" && !pending.length) {
      const messages = await this.adapter.messages(row.directory, row.sessionId, 20);
      const last = [...messages].reverse().find((message) => message.role === "assistant");
      run.phase = "terminal";
      run.activeState = null;
      run.outcome = last?.completed ? "succeeded" : "failed";
      run.result = bounded(last?.text, 20000);
      if (!last?.completed) run.error = terminalFailureError(last);
      const terminal = collectRunUsage(this.adapter, row.directory, row.sessionId,
        run.usageBase);
      if (terminal) run.usage = terminal;
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
    const row = this._conversation(run.conversationId);
    await this.adapter.abortSession(row.directory, row.sessionId);
    // Preserve already-consumed native usage on cancelled runs.
    const consumed = collectRunUsage(this.adapter, row.directory, row.sessionId,
      run.usageBase);
    if (consumed) run.usage = consumed;
    run.phase = "terminal";
    run.activeState = null;
    run.outcome = "cancelled";
    run.updatedAt = now();
    this._save();
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
        { id: "reject", label: "Reject", semantic: "deny" },
      ];
      if (this.profiles.get(row.profile)?.policy.allow_session_always) {
        choices.splice(1, 0,
          { id: "always", label: "Allow for this session", semantic: "approve" });
      }
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
