// Pi's Runtime Protocol v1 facade. Ownership, security profiles and run
// snapshots live on the Pi host; Bridge sees only runtime-neutral records.
import fs from "node:fs";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";

import { AdapterError, sanitizeModels } from "./adapter.mjs";
import { enforcementFingerprint } from "./fingerprint.mjs";
import { policyRevision, safeDefaultPolicy, validatePolicy } from "./policy.mjs";
import { piRelease } from "./release.mjs";
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
    for (const row of data.conversations) {
      // Durable rebind-pending marker: an incomplete profile transition must
      // never re-own its session after restart. Omit pending conversations
      // while retaining terminal runs (terminal _refresh does not require
      // the conversation). Do not clear the marker into an ownable row.
      if (row && row.securityRebindPending) continue;
      this.conversations.set(row.id, row);
    }
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
    const value = { protocol: { major: 1, minor: 0 }, runtime: {
      id: "pi", displayName: "Pi", adapterVersion: "1.0.0",
      nativeVersion: bounded(this.adapter.piVersion, 80), instanceId: this.instanceId,
    }, features: { models: 1, conversations: 1, runs: 1,
      activities: 1, interactions: 1, securityRebind: 1 } };
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

  _assertNotRebindPending(row) {
    if (row && row.securityRebindPending) {
      throw new AdapterError("Conversation is unavailable", 502, "unavailable");
    }
  }

  _setRebindPendingOrThrow(row) {
    // Durable PRE-MUTATION marker persisted BEFORE disposing/reopening the
    // native session. Contains no profile config, paths, prompts, or secrets.
    // If this save fails, abort before touching native state with a bounded
    // error (never raw paths/secrets).
    row.securityRebindPending = true;
    try {
      this._save();
    } catch {
      try { delete row.securityRebindPending; } catch {}
      throw new AdapterError("Conversation is unavailable", 502, "unavailable");
    }
  }

  _clearRebindPendingAndSave(row) {
    delete row.securityRebindPending;
    this._save();
  }

  async _hydrate(row) {
    this._assertNotRebindPending(row);
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
    const binding = { id: row.profile, revision: row.revision };
    return { id: row.id, runtime: "pi", nativeId: row.sessionId,
      workspaceId: row.workspaceId, status,
      securityProfile: binding,
      securityBinding: { source: "profile", profile: binding } };
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

  _capturedOldSnapshot(entry, row) {
    // Capture the immutable owned permission snapshot actually used by the
    // loaded AgentSession. Never infer it from the current profile map,
    // which may already contain the NEW same-ID definition.
    if (!entry || entry.disposed) return null;
    try {
      const policy = JSON.parse(JSON.stringify(entry.permissionPolicy));
      const policyRev = entry.policyRevision;
      if (!policy || typeof policy !== "object" || typeof policyRev !== "string"
          || !policyRev) return null;
      const snapshot = { permissionPolicy: policy, policyRevision: policyRev };
      if (entry.extensionPolicy !== undefined) {
        try { snapshot.extensionPolicy = JSON.parse(JSON.stringify(entry.extensionPolicy)); } catch {}
        snapshot.extensionRevision = entry.extensionRevision;
      }
      snapshot._rowProfile = row.profile;
      snapshot._rowRevision = row.revision;
      return snapshot;
    } catch {
      return null;
    }
  }

  _capturedSnapshotProvesRow(captured, row) {
    // Prove recreating from the captured snapshot corresponds to the exact
    // old WBRP profile revision under current enforcement identity. If the
    // enforcement fingerprint/SDK identity rotated, the same policy yields a
    // different full revision and rollback cannot be claimed.
    if (!captured || !captured.permissionPolicy
        || typeof captured.policyRevision !== "string") return false;
    try {
      if (policyRevision(captured.permissionPolicy) !== captured.policyRevision) return false;
      if (bindProfile(captured.permissionPolicy).revision !== row.revision) return false;
      return true;
    } catch {
      return false;
    }
  }

  _catalogProvesRow(row) {
    // For UNLOADED conversations there is no owned snapshot to capture.
    // Only claim the previous binding if the current catalog still exactly
    // represents row.profile + row.revision.
    try {
      const cur = this.profiles.get(row.profile);
      return !!cur && cur.revision === row.revision;
    } catch {
      return false;
    }
  }

  async _rollbackToCapturedOrCatalog({ row, captured, oldSessionId, oldSessionFile, oldDirectory }) {
    // Best-effort exact rollback. Returns true only when the previous
    // binding is proven restored and idle with no pending permission.
    // Never mutates row.profile/row.revision (row still holds old binding).
    if (captured) {
      if (!this._capturedSnapshotProvesRow(captured, row)) return false;
      const opts = {
        permission_policy: captured.permissionPolicy,
        policy_revision: captured.policyRevision,
      };
      if (captured.extensionPolicy !== undefined) {
        opts.extension_policy = captured.extensionPolicy;
        if (captured.extensionRevision !== undefined) opts.extension_revision = captured.extensionRevision;
      }
      try {
        if (oldSessionFile && fs.existsSync(oldSessionFile)) {
          opts.resumeFile = oldSessionFile;
          opts.expectedSessionId = oldSessionId;
        }
        await this.adapter.createSession(oldDirectory, "Recovered Bridge conversation", opts);
      } catch {
        return false;
      }
      try {
        const status = await this.adapter.sessionStatus(oldDirectory, oldSessionId);
        const pending = await this.adapter.listPermissions(oldDirectory, oldSessionId);
        return status === "idle" && !pending.length;
      } catch {
        await this._disposeAdapterSession(oldSessionId);
        return false;
      }
    }
    // Unloaded: only restore from catalog when it still proves the old row.
    if (!this._catalogProvesRow(row)) return false;
    const cur = this.profiles.get(row.profile);
    if (!cur) return false;
    try {
      if (oldSessionFile && fs.existsSync(oldSessionFile)) {
        await this.adapter.createSession(oldDirectory, "Recovered Bridge conversation", {
          permission_policy: cur.policy, policy_revision: cur.policyRev,
          resumeFile: oldSessionFile, expectedSessionId: oldSessionId,
        });
      } else {
        const restored = await this.adapter.createSession(oldDirectory,
          "Recovered Bridge conversation", {
            permission_policy: cur.policy, policy_revision: cur.policyRev,
          });
        row.sessionId = restored.id;
        row.sessionFile = this.adapter.sessions.get(restored.id)?.sessionFile || "";
        this._save();
      }
    } catch {
      return false;
    }
    try {
      const status = await this.adapter.sessionStatus(row.directory, row.sessionId);
      // Empty-restore has no prior pending state; substantive restore must be idle+unblocked.
      if (status !== "idle") return false;
      try {
        const pending = await this.adapter.listPermissions(row.directory, row.sessionId);
        if (pending.length) return false;
      } catch {
        return false;
      }
      return true;
    } catch {
      await this._disposeAdapterSession(row.sessionId);
      return false;
    }
  }

  async _invalidateAfterAmbiguousRebind(row, sessionIdToDispose) {
    // Remove/invalidate the WBRP conversation and ensure no ambiguous
    // AgentSession remains usable. Historical terminal runs stay in the
    // runs map (terminal _refresh does not require the conversation).
    // A durable rebind-pending marker was persisted BEFORE native mutation,
    // so even if this final persistence fails, restart remains fail-closed.
    // Still attempt it; return bounded errors only, never raw paths/secrets.
    if (sessionIdToDispose) await this._disposeAdapterSession(sessionIdToDispose);
    try { this.conversations.delete(row.id); } catch {}
    try { this._save(); } catch {}
  }

  _failClosedAfterFinalSaveFailure(row, sessionIdToDispose) {
    // Final marker-clear save failed after a proven state change. Durable
    // state still holds the pre-mutation pending marker, so restart is
    // fail-closed. Make the current process fail-closed too.
    return (async () => {
      if (sessionIdToDispose) await this._disposeAdapterSession(sessionIdToDispose);
      try { this.conversations.delete(row.id); } catch {}
      throw new AdapterError("Conversation is unavailable", 502, "unavailable");
    })();
  }

  async rebindConversation(conversationId, securityBinding) {
    // Idle-boundary security rebind for a named-profile change.
    // Preserves the same WBRP conversation ID and persisted history.
    // No prompt is replayed and no blank replacement is created.
    // Row metadata is committed to target only after complete
    // post-reopen validation. Any ambiguous failure invalidates the
    // conversation rather than claiming an unproven rollback.
    // Special case: a truly empty conversation with no history/runs may
    // take a new empty native session under the new profile while keeping
    // the same WBRP conversation ID.
    const target = securityBinding?.profile;
    const targetId = target?.id;
    const requestedRevision = target?.revision;
    if (securityBinding?.source !== "profile"
        || typeof targetId !== "string" || !targetId || targetId.length > 100
        || typeof requestedRevision !== "string" || !requestedRevision
        || requestedRevision.length > 100) {
      throw new AdapterError("Invalid security rebind binding", 400, "invalid_arguments");
    }
    const row = this._conversation(conversationId);
    this._assertNotRebindPending(row);
    const live = this.profiles.get(targetId);
    if (!live || live.revision !== requestedRevision) {
      throw new AdapterError("Requested security revision is not current", 409, "binding_mismatch");
    }
    if ([...this.runs.values()].some((run) => run.conversationId === row.id
        && (run.phase === "starting" || run.phase === "active"))) {
      throw new AdapterError("Conversation is busy", 409, "conversation_busy");
    }
    if (row.profile === targetId && row.revision === live.revision) {
      await this._hydrate(row);
      const status = await this.adapter.sessionStatus(row.directory, row.sessionId);
      if (status !== "idle") {
        throw new AdapterError("Conversation is busy", 409, "conversation_busy");
      }
      const pending = await this.adapter.listPermissions(row.directory, row.sessionId);
      if (pending.length) {
        throw new AdapterError("Conversation has a pending permission", 409,
          "security_rebind_unavailable");
      }
      return this._publicConversation(row, "idle");
    }
    const oldSessionId = row.sessionId;
    const oldSessionFile = row.sessionFile;
    const oldDirectory = row.directory;
    const loadedEntry = this.adapter?.sessions?.get(oldSessionId) || null;
    const loaded = !!loadedEntry && !loadedEntry.disposed;
    // Capture the immutable owned snapshot BEFORE disposing. Do not infer
    // it from the current profile map (unsafe for same-ID rev1->rev2).
    const captured = loaded ? this._capturedOldSnapshot(loadedEntry, row) : null;
    if (loaded) {
      let status;
      try {
        status = await this.adapter.sessionStatus(oldDirectory, oldSessionId);
      } catch {
        throw new AdapterError("Owned Pi session is unavailable", 502, "unavailable");
      }
      if (status !== "idle") {
        throw new AdapterError("Conversation is busy", 409, "conversation_busy");
      }
      let pending;
      try {
        pending = await this.adapter.listPermissions(oldDirectory, oldSessionId);
      } catch {
        throw new AdapterError("Owned Pi session is unavailable", 502, "unavailable");
      }
      if (pending.length) {
        throw new AdapterError("Conversation has a pending permission", 409,
          "security_rebind_unavailable");
      }
    }
    const hasRuns = [...this.runs.values()].some((run) => run.conversationId === row.id);
    const fileExists = Boolean(oldSessionFile) && fs.existsSync(oldSessionFile);
    if (!hasRuns && !fileExists) {
      // Truly empty conversation: no history and no session file. Replacing
      // the native empty session is still a native mutation: persist the
      // durable pre-marker BEFORE disposing/creating. Abort on save failure
      // with zero native mutation.
      this._setRebindPendingOrThrow(row);
      if (loaded) await this._disposeAdapterSession(oldSessionId);
      let fresh;
      try {
        fresh = await this.adapter.createSession(oldDirectory,
          "Rebound empty Bridge conversation", {
            permission_policy: live.policy, policy_revision: live.policyRev,
          });
      } catch (error) {
        const restored = await this._rollbackToCapturedOrCatalog({
          row, captured, oldSessionId, oldSessionFile, oldDirectory });
        if (restored) {
          try {
            this._clearRebindPendingAndSave(row);
          } catch {
            await this._failClosedAfterFinalSaveFailure(row, row.sessionId);
          }
          throw new AdapterError("Security rebind failed and the previous binding was restored",
            502, "security_rebind_unavailable");
        }
        await this._invalidateAfterAmbiguousRebind(row, row.sessionId);
        // If rollback created a session but could not prove it, it was
        // already disposed by the rollback helper/invalidator.
        throw new AdapterError("Security rebind failed and the conversation is unavailable",
          502, "unavailable");
      }
      // Validate the fresh empty session before committing row metadata.
      try {
        const status = await this.adapter.sessionStatus(oldDirectory, fresh.id);
        if (status !== "idle") throw new Error("not idle");
      } catch {
        await this._disposeAdapterSession(fresh.id);
        const restored = await this._rollbackToCapturedOrCatalog({
          row, captured, oldSessionId, oldSessionFile, oldDirectory });
        if (restored) {
          try {
            this._clearRebindPendingAndSave(row);
          } catch {
            await this._failClosedAfterFinalSaveFailure(row, row.sessionId);
          }
          throw new AdapterError("Security rebind failed and the previous binding was restored",
            502, "security_rebind_unavailable");
        }
        await this._invalidateAfterAmbiguousRebind(row, row.sessionId);
        throw new AdapterError("Security rebind failed and the conversation is unavailable",
          502, "unavailable");
      }
      row.sessionId = fresh.id;
      row.sessionFile = this.adapter.sessions.get(fresh.id)?.sessionFile || "";
      row.profile = targetId;
      row.revision = live.revision;
      try {
        this._clearRebindPendingAndSave(row);
      } catch {
        await this._failClosedAfterFinalSaveFailure(row, fresh.id);
      }
      return this._publicConversation(row, "idle");
    }
    // Substantive persisted session: persist the durable pre-marker BEFORE
    // disposing/reopening the native session. Abort on marker-save failure
    // with zero native mutation. Row metadata stays on old binding until
    // post-reopen validation passes.
    this._setRebindPendingOrThrow(row);
    if (loaded) await this._disposeAdapterSession(oldSessionId);
    let targetOpened = false;
    let targetError = null;
    try {
      await this.adapter.createSession(oldDirectory, "Rebound Bridge conversation", {
        permission_policy: live.policy, policy_revision: live.policyRev,
        resumeFile: oldSessionFile, expectedSessionId: oldSessionId,
      });
      targetOpened = true;
    } catch (error) {
      targetOpened = false;
      targetError = error;
    }
    if (!targetOpened) {
      const restored = await this._rollbackToCapturedOrCatalog({
        row, captured, oldSessionId, oldSessionFile, oldDirectory });
      if (restored) {
        try {
          this._clearRebindPendingAndSave(row);
        } catch {
          await this._failClosedAfterFinalSaveFailure(row, oldSessionId);
        }
        throw new AdapterError("Security rebind failed and the previous binding was restored",
          502, "security_rebind_unavailable");
      }
      await this._invalidateAfterAmbiguousRebind(row, oldSessionId);
      if (targetError instanceof AdapterError) throw targetError;
      throw new AdapterError("Security rebind failed and the conversation is unavailable",
        502, "unavailable");
    }
    // Target reopened: validate BEFORE mutating row.profile/row.revision.
    try {
      const status = await this.adapter.sessionStatus(oldDirectory, oldSessionId);
      if (status !== "idle") throw new Error("post-reopen not idle");
      const pending = await this.adapter.listPermissions(oldDirectory, oldSessionId);
      if (pending.length) throw new Error("post-reopen pending");
      // Expected identity/history is already enforced by createSession
      // (expectedSessionId + resumeFile); idle + no-pending completes proof.
    } catch {
      // Ambiguous target session must not remain usable.
      await this._disposeAdapterSession(oldSessionId);
      const restored = await this._rollbackToCapturedOrCatalog({
        row, captured, oldSessionId, oldSessionFile, oldDirectory });
      if (restored) {
        try {
          this._clearRebindPendingAndSave(row);
        } catch {
          await this._failClosedAfterFinalSaveFailure(row, oldSessionId);
        }
        throw new AdapterError("Security rebind failed and the previous binding was restored",
          502, "security_rebind_unavailable");
      }
      await this._invalidateAfterAmbiguousRebind(row, oldSessionId);
      throw new AdapterError("Security rebind failed and the conversation is unavailable",
        502, "unavailable");
    }
    row.profile = targetId;
    row.revision = live.revision;
    try {
      const reopened = this.adapter.sessions.get(oldSessionId);
      if (reopened?.sessionFile) row.sessionFile = reopened.sessionFile;
    } catch {}
    try {
      this._clearRebindPendingAndSave(row);
    } catch {
      await this._failClosedAfterFinalSaveFailure(row, oldSessionId);
    }
    return this._publicConversation(row, "idle");
  }


  async startRun(conversationId, body) {
    const row = this._conversation(conversationId);
    this._assertNotRebindPending(row);
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
