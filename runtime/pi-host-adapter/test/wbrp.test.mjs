import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { PiRuntimeProtocol } from "../wbrp.mjs";
import { createPiAdapterServer } from "../server.mjs";
import { PiAdapter } from "../adapter.mjs";
import { safeDefaultPolicy } from "../policy.mjs";

function fixture(root) {
  const agentDir = path.join(root, "agent");
  const projectsRoot = path.join(root, "projects");
  const directory = path.join(projectsRoot, "work");
  fs.mkdirSync(agentDir, { recursive: true });
  fs.mkdirSync(directory, { recursive: true });
  const sessionFile = path.join(agentDir, "native-session.jsonl");
  fs.writeFileSync(sessionFile, "session\n");
  const adapter = {
    agentDir, projectsRoot, piVersion: "test", sessions: new Map(),
    listModelsFn: async () => [{ provider: "provider", id: "model", name: "Model" }],
    async createSession(cwd, title, options) {
      assert.equal(cwd, directory);
      this.lastOptions = options;
      if (options.expectedSessionId) assert.equal(options.expectedSessionId, "native-session");
      // Store the immutable owned snapshot like the real PiAdapter so
      // rebind rollback can capture it (never infer from profile map).
      this.sessions.set("native-session", { sessionFile,
        permissionPolicy: options.permission_policy ? JSON.parse(JSON.stringify(options.permission_policy)) : undefined,
        policyRevision: options.policy_revision,
        disposed: false });
      return { id: "native-session", directory };
    },
    async sessionStatus() { return this.busy ? "busy" : "idle"; },
    async promptAsync() { this.busy = true; return { accepted: true }; },
    async listPermissions() { return this.permission ? [{ id: "permission-1", tool: "edit",
      action: "write", resource: "file.txt" }] : []; },
    async respondPermission(cwd, sessionId, permissionId, answer) {
      assert.equal(permissionId, "permission-1");
      assert.equal(answer, "reject");
      this.permission = false;
    },
    executionHead() { return 0; },
    async readExecutions() { return { updates: [], next: 0, head: 0 }; },
    async messages() { return [{ role: "assistant", text: "Done", completed: 1 }]; },
    async abortSession() { this.busy = false; },
  };
  return { adapter, directory };
}

test("Pi protocol owns a durable conversation, exact choice and restart recovery", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-wbrp-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    assert.equal((await protocol.models()).models[0].selector, "provider/model");
    const profile = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do the task" }], model: "provider/model",
    });
    assert.equal(run.phase, "active");
    adapter.permission = true;
    const pending = await protocol.interactions(run.id);
    assert.equal(pending.interactions[0].id, "permission-1");
    await protocol.resolve("permission-1", { choiceId: "reject" });
    adapter.busy = false;
    assert.equal((await protocol.run(run.id)).outcome, "succeeded");
    const restarted = fixture(root).adapter;
    const recovered = new PiRuntimeProtocol(restarted);
    assert.equal((await recovered.conversation(conversation.id)).status, "idle");
    assert.equal((await recovered.run(run.id)).outcome, "succeeded");
    assert.equal(restarted.sessions.has("native-session"), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi custom profiles persist external file controls and reject stale edits", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-profiles-"));
  try {
    const { adapter, directory } = fixture(root);
    const external = path.join(root, "shared");
    fs.mkdirSync(external);
    const protocol = new PiRuntimeProtocol(adapter);
    const config = { ...safeDefaultPolicy(), write_tools_enabled: true,
      shell_mode: "ask", external_access: {
        default_mode: "deny", roots: [{ path: external, mode: "ask" }],
      } };
    const saved = protocol.saveProfile({ id: "reviewed-shared", config });
    assert.equal(saved.config.external_access.roots[0].path, external);
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: saved.id, revision: saved.revision } });
    assert.equal(adapter.lastOptions.permission_policy.external_access.roots[0].mode, "ask");
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Review" }], model: "provider/model",
    });
    assert.throws(() => protocol.saveProfile({ id: saved.id,
      expectedRevision: saved.revision, config: { ...config, shell_mode: "deny" } }),
    /Profile has an active run/);
    adapter.busy = false;
    assert.equal((await protocol.run(run.id)).phase, "terminal");
    assert.equal((new PiRuntimeProtocol(adapter)).profileList().profiles
      .find((profile) => profile.id === saved.id).revision, saved.revision);
    assert.throws(() => protocol.saveProfile({ id: saved.id, config }),
      /Security profile changed/);
    const updated = protocol.saveProfile({ id: saved.id, expectedRevision: saved.revision,
      config: { ...config, shell_mode: "deny" } });
    assert.notEqual(updated.revision, saved.revision);
    await assert.rejects(() => protocol.conversation(conversation.id), /Security profile changed/);
    assert.deepEqual(protocol.deleteProfile(saved.id), { deleted: saved.id });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi protocol routes are private and speak the shared HTTP contract", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-wbrp-http-"));
  const { adapter, directory } = fixture(root);
  const server = createPiAdapterServer({ adapter, token: "secret",
    adapterVersion: "test", instance: "test-instance" });
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const health = await (await fetch(`${base}/health`)).json();
    assert.equal(health.runtime, "pi");
    assert.equal(health.protocol, 1);
    assert.equal(health.locked, false);
    assert.ok(!JSON.stringify(health).includes(directory));
    assert.equal((await fetch(`${base}/v1/descriptor`)).status, 401);
    const headers = { "X-Runtime-Token": "secret" };
    const descriptor = await (await fetch(`${base}/v1/descriptor`, { headers })).json();
    assert.equal(descriptor.runtime.id, "pi");
    assert.equal(descriptor.features.securityRebind, 1);
    const listed = await (await fetch(`${base}/v1/models`, { headers })).json();
    assert.equal(listed.models[0].selector, "provider/model");
    const profiles = (await (await fetch(`${base}/v1/profiles`, { headers })).json()).profiles;
    const profile = profiles[0];
    const other = profiles[1];
    const response = await fetch(`${base}/v1/conversations`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ workspaceId: "ws-one", directory,
        securityProfile: { id: profile.id, revision: profile.revision } }),
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).workspaceId, "ws-one");
    // Rebind via HTTP preserves the same conversation under the new profile.
    const convId = (await (await fetch(`${base}/v1/conversations`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ workspaceId: "ws-one", directory,
        securityProfile: { id: profile.id, revision: profile.revision } }),
    })).json()).id;
    const rebindRes = await fetch(`${base}/v1/conversations/${convId}/security`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ securityBinding: { source: "profile",
        profile: { id: other.id, revision: other.revision } } }),
    });
    assert.equal(rebindRes.status, 200);
    const rebound = await rebindRes.json();
    assert.equal(rebound.id, convId);
    assert.deepEqual(rebound.securityBinding, { source: "profile",
      profile: { id: other.id, revision: other.revision } });
    const bad = await fetch(`${base}/v1/conversations/${convId}/security`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ securityBinding: { source: "profile",
        profile: { id: other.id } } }),
    });
    assert.equal(bad.status, 400);
    for (const legacyPath of ["/models", "/sessions", "/extensions"]) {
      assert.equal((await fetch(`${base}${legacyPath}`, { headers })).status, 404);
    }
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an unused real Pi SDK conversation recovers without replay", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-wbrp-sdk-"));
  const projects = path.join(root, "projects");
  const workspace = path.join(projects, "work");
  const agentDir = path.join(root, "agent");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(agentDir);
  const make = () => new PiAdapter({ projectsRoot: fs.realpathSync(projects),
    agentDir: fs.realpathSync(agentDir) });
  const native = make();
  try {
    const protocol = new PiRuntimeProtocol(native);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({ workspaceId: "ws",
      directory: fs.realpathSync(workspace),
      securityProfile: { id: profile.id, revision: profile.revision } });
    assert.equal(fs.existsSync(native.sessions.get(conversation.nativeId).sessionFile), false);
    await native.shutdown();
    const restarted = make();
    try {
      const reopened = await new PiRuntimeProtocol(restarted).conversation(conversation.id);
      assert.equal(reopened.status, "idle");
      assert.notEqual(reopened.nativeId, conversation.nativeId);
    } finally {
      await restarted.shutdown();
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi rebind preserves history across profile change and next run succeeds", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    assert.equal(protocol.descriptor().features.securityRebind, 1);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision },
    });
    const firstSession = conversation.nativeId;
    const firstFile = adapter.sessions.get(firstSession)?.sessionFile;
    assert.ok(firstFile && fs.existsSync(firstFile));
    // Substantive history: one completed run writes the session file path.
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do the task" }], model: "provider/model",
    });
    adapter.busy = false;
    assert.equal((await protocol.run(run.id)).outcome, "succeeded");
    // Restriction: read-only -> workspace-write-reviewed is escalation; also test restriction below.
    const rebound = await protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    });
    assert.equal(rebound.id, conversation.id);
    assert.equal(rebound.nativeId, firstSession);
    assert.deepEqual(rebound.securityBinding, { source: "profile",
      profile: { id: b.id, revision: b.revision } });
    assert.equal(adapter.sessions.has(firstSession), true);
    assert.ok(adapter.lastOptions.permission_policy);
    // Next run succeeds under the new policy with the same history.
    const second = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Follow-up" }], model: "provider/model",
    });
    assert.equal(second.conversationId, conversation.id);
    adapter.busy = false;
    assert.equal((await protocol.run(second.id)).outcome, "succeeded");
    // Restriction back to read-only also works on the same conversation.
    const back = await protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: a.id, revision: a.revision },
    });
    assert.equal(back.nativeId, firstSession);
    assert.deepEqual(back.securityBinding.profile, { id: a.id, revision: a.revision });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi rebind rejects busy and pending permission without losing history", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-busy-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision },
    });
    const active = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Long task" }], model: "provider/model",
    });
    assert.equal(active.phase, "active");
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /busy/i);
    assert.equal(adapter.sessions.has(conversation.nativeId), true);
    adapter.busy = false;
    assert.equal((await protocol.run(active.id)).outcome, "succeeded");
    // Pending permission makes the transition unsafe.
    adapter.permission = true;
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /pending|security_rebind_unavailable|unavailable/i);
    adapter.permission = false;
    const rebound = await protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    });
    assert.equal(rebound.securityBinding.profile.id, b.id);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi rebind rolls back to the old policy on failure and never runs ambiguous", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-rollback-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do the task" }], model: "provider/model",
    });
    adapter.busy = false;
    await protocol.run(run.id);
    const originalCreate = adapter.createSession.bind(adapter);
    let calls = 0;
    adapter.createSession = async (cwd, title, options) => {
      calls += 1;
      if (calls === 1) throw new Error("native reopen failed");
      return originalCreate(cwd, title, options);
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /restored|unavailable|failed/i);
    // Rollback restored the old session; the old binding still works.
    const current = await protocol.conversation(conversation.id);
    assert.deepEqual(current.securityBinding.profile, { id: a.id, revision: a.revision });
    adapter.busy = false;
    const next = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Still old policy" }], model: "provider/model",
    });
    assert.equal(next.conversationId, conversation.id);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi rebind empty conversation keeps ID and restart hydrates with new profile", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-empty-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision },
    });
    // Truly empty: no runs and no session file.
    const row = protocol.conversations.get(conversation.id);
    try { fs.rmSync(row.sessionFile, { force: true }); } catch {}
    adapter.sessions.delete(row.sessionId);
    const rebound = await protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    });
    assert.equal(rebound.id, conversation.id);
    assert.deepEqual(rebound.securityBinding.profile, { id: b.id, revision: b.revision });
    // Restart hydration uses the new profile.
    const restartedAdapter = fixture(root).adapter;
    const recovered = new PiRuntimeProtocol(restartedAdapter);
    // The recovered instance shares the same persisted state file.
    const hyd = await recovered.conversation(conversation.id);
    assert.deepEqual(hyd.securityBinding.profile, { id: b.id, revision: b.revision });
    // Direct reads without rebind remain fail-closed after a profile edit.
    const saved = protocol.saveProfile({ id: "custom-a", config: { ...a.config } });
    const conv2 = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: saved.id, revision: saved.revision } });
    const updated = protocol.saveProfile({ id: saved.id, expectedRevision: saved.revision,
      config: { ...saved.config, shell_mode: "ask" } });
    assert.notEqual(updated.revision, saved.revision);
    await assert.rejects(() => protocol.conversation(conv2.id), /Security profile changed/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi same-ID revision failure never restores with the new definition", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-sameid-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const base = safeDefaultPolicy();
    const configA = { ...base, shell_mode: "deny" };
    const savedA = protocol.saveProfile({ id: "same-rev-test", config: configA });
    const rev1 = savedA.revision;
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: savedA.id, revision: rev1 } });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Substantive" }], model: "provider/model",
    });
    adapter.busy = false;
    await protocol.run(run.id);
    const configB = { ...base, shell_mode: "ask" };
    const savedB = protocol.saveProfile({ id: savedA.id, expectedRevision: rev1, config: configB });
    const rev2 = savedB.revision;
    assert.notEqual(rev2, rev1);
    // Force target reopen to fail AFTER the old session is disposed.
    const originalCreate = adapter.createSession.bind(adapter);
    let calls = 0;
    adapter.createSession = async (cwd, title, options) => {
      calls += 1;
      if (calls === 1) throw new Error("forced target reopen failure");
      return originalCreate(cwd, title, options);
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: savedA.id, revision: rev2 },
    }), /restored|unavailable|failed|mismatch/i);
    // Exact old snapshot must have been used for any claimed restore: the
    // last successful create must carry the OLD policy (deny), never the
    // new definition (ask) masquerading as revision1.
    assert.equal(adapter.lastOptions.permission_policy.shell_mode, "deny");
    assert.notEqual(adapter.lastOptions.permission_policy.shell_mode, "ask");
    // Row must NOT be committed to target rev2 on failure. Direct map read
    // (no hydrate) proves old revision retained; direct conversation() would
    // correctly fail closed with profile_mismatch while live is rev2.
    const stored = protocol.conversations.get(conversation.id);
    if (stored) {
      assert.equal(stored.revision, rev1);
      assert.equal(stored.profile, savedA.id);
    } else {
      // Invalidation is also acceptable: no ambiguous session remains.
      assert.equal(adapter.sessions.has(conversation.nativeId), false);
    }
    // Retry after clearing the forced failure must still be able to rebind
    // with preserved history (proves no wrong-policy session was left).
    adapter.createSession = originalCreate;
    const retry = await protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: savedA.id, revision: rev2 },
    });
    assert.deepEqual(retry.securityBinding.profile, { id: savedA.id, revision: rev2 });
    assert.equal(retry.nativeId, conversation.nativeId);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi post-reopen validation failure leaves old metadata and no ambiguous session", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-postval-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision } });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Substantive" }], model: "provider/model",
    });
    adapter.busy = false;
    await protocol.run(run.id);
    // Target create succeeds, but post-reopen validation sees busy once.
    const origStatus = adapter.sessionStatus.bind(adapter);
    let statusCalls = 0;
    adapter.sessionStatus = async (...args) => {
      statusCalls += 1;
      if (statusCalls === 1) return "busy";
      return origStatus(...args);
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /restored|unavailable|busy|failed/i);
    adapter.sessionStatus = origStatus;
    // Row metadata must NOT be committed to target.
    const row = protocol.conversations.get(conversation.id);
    // Either exact rollback preserved old binding, or conversation invalidated.
    if (row) {
      assert.equal(row.profile, a.id);
      assert.equal(row.revision, a.revision);
      // No ambiguous target session remains usable: the owned session (if any)
      // must validate idle with no pending permission under old binding.
      const status = await adapter.sessionStatus(row.directory, row.sessionId);
      assert.equal(status, "idle");
      const pending = await adapter.listPermissions(row.directory, row.sessionId);
      assert.equal(pending.length, 0);
      const current = await protocol.conversation(conversation.id);
      assert.deepEqual(current.securityBinding.profile, { id: a.id, revision: a.revision });
    } else {
      // Invalidated: subsequent reads fail closed.
      await assert.rejects(() => protocol.conversation(conversation.id), /not_found|unavailable/i);
      // No usable ambiguous session remains.
      assert.equal(adapter.sessions.has(conversation.nativeId), false);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi pre-marker save failure causes zero native mutation", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-premarker-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision } });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Substantive" }], model: "provider/model",
    });
    adapter.busy = false;
    await protocol.run(run.id);
    const rowBefore = protocol.conversations.get(conversation.id);
    const sessionsBefore = adapter.sessions.has(rowBefore.sessionId);
    assert.equal(sessionsBefore, true);
    let creates = 0;
    const origCreate = adapter.createSession.bind(adapter);
    adapter.createSession = async (...args) => { creates += 1; return origCreate(...args); };
    const origSave = protocol._save.bind(protocol);
    let saves = 0;
    protocol._save = () => {
      saves += 1;
      throw new Error("injected pre-marker save failure");
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /unavailable/i);
    assert.equal(creates, 0);
    assert.equal(adapter.sessions.has(rowBefore.sessionId), true);
    const rowAfter = protocol.conversations.get(conversation.id);
    assert.equal(rowAfter.securityRebindPending, undefined);
    assert.equal(rowAfter.profile, a.id);
    protocol._save = origSave;
    adapter.createSession = origCreate;
    const current = await protocol.conversation(conversation.id);
    assert.deepEqual(current.securityBinding.profile, { id: a.id, revision: a.revision });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi final-save failure after target success stays fail-closed across restart", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-finalsave-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision } });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Substantive" }], model: "provider/model",
    });
    adapter.busy = false;
    const terminal = await protocol.run(run.id);
    assert.equal(terminal.outcome, "succeeded");
    const origSave = protocol._save.bind(protocol);
    let saves = 0;
    protocol._save = () => {
      saves += 1;
      // Allow the pre-marker save (first), fail the final marker-clear save.
      if (saves >= 2) throw new Error("injected final save failure");
      return origSave();
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /unavailable/i);
    // Current process is fail-closed.
    await assert.rejects(() => protocol.conversation(conversation.id), /unavailable|not found|not_found/i);
    // Durable state still holds the pending marker: restart refuses ownership.
    const restartedAdapter = fixture(root).adapter;
    const recovered = new PiRuntimeProtocol(restartedAdapter);
    assert.equal(recovered.conversations.has(conversation.id), false);
    await assert.rejects(() => recovered.conversation(conversation.id), /not found|not_found/i);
    // Terminal runs survive reload/read where designed.
    const reloadedRun = await recovered.run(run.id);
    assert.equal(reloadedRun.outcome, "succeeded");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi final-save failure after exact rollback stays fail-closed across restart", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-rebind-rollbacksave-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const a = protocol.profileList().profiles.find((row) => row.id === "read-only");
    const b = protocol.profileList().profiles.find((row) => row.id === "workspace-write-reviewed");
    const conversation = await protocol.createConversation({ workspaceId: "ws-one", directory,
      securityProfile: { id: a.id, revision: a.revision } });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Substantive" }], model: "provider/model",
    });
    adapter.busy = false;
    await protocol.run(run.id);
    // Force target reopen to fail so rollback runs; fail the rollback's
    // final marker-clear save (pre-marker save + rollback session creates
    // succeed, clear fails).
    const origCreate = adapter.createSession.bind(adapter);
    let creates = 0;
    adapter.createSession = async (...args) => {
      creates += 1;
      if (creates === 1) throw new Error("forced target reopen failure");
      return origCreate(...args);
    };
    const origSave = protocol._save.bind(protocol);
    let saves = 0;
    protocol._save = () => {
      saves += 1;
      if (saves >= 2) throw new Error("injected rollback-clear save failure");
      return origSave();
    };
    await assert.rejects(() => protocol.rebindConversation(conversation.id, {
      source: "profile", profile: { id: b.id, revision: b.revision },
    }), /unavailable/i);
    await assert.rejects(() => protocol.conversation(conversation.id), /unavailable|not found|not_found/i);
    const restartedAdapter = fixture(root).adapter;
    const recovered = new PiRuntimeProtocol(restartedAdapter);
    assert.equal(recovered.conversations.has(conversation.id), false);
    const reloadedRun = await recovered.run(run.id);
    assert.equal(reloadedRun.outcome, "succeeded");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
