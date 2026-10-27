import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { PiRuntimeProtocol } from "../wbrp.mjs";
import { PassThrough } from "node:stream";

import { serveStdio } from "../stdio.mjs";
import { PiAdapter } from "../adapter.mjs";
import { BUILTINS, safeDefaultPolicy } from "../policy.mjs";
import { securityBlock } from "./security-block.mjs";

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
    created: 0,
    async createSession(cwd, title, options) {
      assert.equal(cwd, directory);
      if (this.failCreate) throw new Error("native session failed");
      this.created += 1;
      this.lastOptions = options;
      if (options.expectedSessionId) assert.equal(options.expectedSessionId, "native-session");
      // Pin the session to its rules like the real PiAdapter.
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

const READ_ONLY = securityBlock();
const REVIEWED = securityBlock(BUILTINS.reviewed, "reviewed");
const BINDING = (block) => ({ source: "profile",
  profile: { id: block.profile.id, revision: block.profile.revision } });

test("Pi protocol owns a durable conversation, exact choice and restart recovery", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-wbrp-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    assert.equal((await protocol.models()).models[0].selector, "provider/model");
    const conversation = await protocol.createConversation({
      workspaceId: "ws-one", directory, security: READ_ONLY,
    });
    assert.deepEqual(conversation.securityBinding, BINDING(READ_ONLY));
    assert.deepEqual(adapter.lastOptions, { permission_policy: READ_ONLY.profile.rules,
      policy_revision: READ_ONLY.profile.revision });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do the task" }], model: "provider/model", security: READ_ONLY,
    });
    assert.equal(run.phase, "active");
    assert.deepEqual(run.securityBinding, BINDING(READ_ONLY));
    adapter.permission = true;
    const pending = await protocol.interactions(run.id);
    assert.equal(pending.interactions[0].id, "permission-1");
    assert.deepEqual(pending.interactions[0].choices.map((choice) => choice.id),
      ["once", "always", "reject"]);
    await protocol.resolve("permission-1", { choiceId: "reject" });
    adapter.busy = false;
    assert.equal((await protocol.run(run.id)).outcome, "succeeded");
    const restarted = fixture(root).adapter;
    const recovered = new PiRuntimeProtocol(restarted);
    assert.equal((await recovered.conversation(conversation.id)).status, "idle");
    assert.equal((await recovered.run(run.id)).outcome, "succeeded");
    assert.equal(restarted.sessions.has("native-session"), true);
    assert.equal(restarted.lastOptions.policy_revision, READ_ONLY.profile.revision);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi refuses native runs and profile blocks that do not match their rules", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-security-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    await assert.rejects(() => protocol.createConversation({ workspaceId: "ws", directory,
      security: { source: "native" } }), { code: "security_unavailable" });
    const tampered = structuredClone(READ_ONLY);
    tampered.profile.rules.shell = "allow";
    await assert.rejects(() => protocol.createConversation({ workspaceId: "ws", directory,
      security: tampered }), { code: "invalid_profile" });
    await assert.rejects(() => protocol.createConversation({ workspaceId: "ws", directory }),
      { code: "invalid_profile" });
    assert.equal(adapter.created, 0);
    const conversation = await protocol.createConversation({ workspaceId: "ws", directory,
      security: READ_ONLY });
    await assert.rejects(() => protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Go" }] }), { code: "invalid_profile" });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a continuation under a new revision re-opens the idle session from its file", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-revision-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const conversation = await protocol.createConversation({ workspaceId: "ws", directory,
      security: READ_ONLY });
    const first = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Look" }], security: READ_ONLY });
    adapter.busy = false;
    await protocol.run(first.id);
    const before = adapter.created;
    // The same revision keeps the loaded session.
    const same = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Again" }], security: READ_ONLY });
    assert.equal(adapter.created, before);
    adapter.busy = false;
    await protocol.run(same.id);
    const second = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Now edit" }], security: REVIEWED });
    assert.equal(adapter.created, before + 1);
    assert.equal(adapter.lastOptions.resumeFile, path.join(root, "agent", "native-session.jsonl"));
    assert.equal(adapter.lastOptions.policy_revision, REVIEWED.profile.revision);
    assert.deepEqual(second.securityBinding, BINDING(REVIEWED));
    assert.deepEqual((await protocol.conversation(conversation.id)).securityBinding,
      BINDING(REVIEWED));
    // A restart hydrates with the rules of the last run.
    adapter.busy = false;
    await protocol.run(second.id);
    const restarted = fixture(root).adapter;
    await new PiRuntimeProtocol(restarted).conversation(conversation.id);
    assert.deepEqual(restarted.lastOptions.permission_policy, BUILTINS.reviewed);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a revision change waits for an idle session with no pending approval", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-revision-busy-"));
  try {
    const { adapter, directory } = fixture(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const conversation = await protocol.createConversation({ workspaceId: "ws", directory,
      security: READ_ONLY });
    const before = adapter.created;
    adapter.busy = true;
    await assert.rejects(() => protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Edit" }], security: REVIEWED }), { code: "conversation_busy" });
    adapter.busy = false;
    adapter.permission = true;
    await assert.rejects(() => protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Edit" }], security: REVIEWED }), { code: "conversation_busy" });
    assert.equal(adapter.created, before);
    assert.equal(adapter.sessions.get("native-session").policyRevision, READ_ONLY.profile.revision);
    // A failed re-open leaves the conversation on its previous rules.
    adapter.permission = false;
    adapter.failCreate = true;
    await assert.rejects(() => protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Edit" }], security: REVIEWED }));
    adapter.failCreate = false;
    assert.deepEqual((await protocol.conversation(conversation.id)).securityBinding,
      BINDING(READ_ONLY));
    assert.equal(adapter.lastOptions.policy_revision, READ_ONLY.profile.revision);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a conversation from before Bridge profiles does not run again", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-legacy-"));
  try {
    const { adapter } = fixture(root);
    const directory = path.join(root, "projects", "work");
    fs.writeFileSync(path.join(root, "agent", "wbrp-state.json"), JSON.stringify({ version: 1,
      conversations: [{ id: "conv_old", workspaceId: "ws", directory, sessionId: "native-session",
        sessionFile: path.join(root, "agent", "native-session.jsonl"), profile: "read-only",
        revision: "a".repeat(64), createdAt: "2026-01-01T00:00:00.000Z" },
      { id: "conv_pending", workspaceId: "ws", directory, sessionId: "other",
        securityRebindPending: true }],
      runs: [], activities: [] }));
    const protocol = new PiRuntimeProtocol(adapter);
    await assert.rejects(() => protocol.startRun("conv_old", {
      input: [{ type: "text", text: "Go" }], security: READ_ONLY }),
    { code: "security_source_changed" });
    await assert.rejects(() => protocol.conversation("conv_pending"), { code: "not_found" });
    assert.equal(adapter.created, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi protocol speaks the shared contract over the stdio plugin transport", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-wbrp-stdio-"));
  const { adapter, directory } = fixture(root);
  const input = new PassThrough();
  const replies = new Map();
  const waiters = [];
  serveStdio({ input, buildAdapter: () => adapter, exit: () => {},
    write: (line) => {
      const message = JSON.parse(line);
      replies.set(message.id, message);
      for (const wake of waiters.splice(0)) wake();
    } });
  let next = 0;
  const call = async (method, params = {}) => {
    const id = ++next;
    input.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    while (!replies.has(id)) await new Promise((resolve) => waiters.push(resolve));
    return replies.get(id);
  };
  try {
    assert.equal((await call("initialize", { protocol: 2 })).result.protocol, 2);
    const descriptor = (await call("descriptor")).result;
    assert.equal(descriptor.runtime.id, "pi");
    assert.equal(descriptor.features.securityRebind, undefined);
    assert.equal((await call("models")).result.models[0].selector, "provider/model");
    const created = (await call("conversations.create", { body: { workspaceId: "ws-one", directory,
      security: READ_ONLY } })).result;
    assert.equal(created.workspaceId, "ws-one");
    assert.deepEqual(created.securityBinding, BINDING(READ_ONLY));
    const bad = await call("conversations.create", { body: { workspaceId: "ws-one", directory,
      security: { source: "profile", profile: { id: "read-only" } } } });
    assert.equal(bad.error.data.status, 400);
    for (const legacy of ["sessions", "extensions", "health", "profiles.list",
      "conversations.rebind"]) {
      assert.equal((await call(legacy)).error.data.code, "not_found");
    }
  } finally {
    input.end();
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
    const conversation = await protocol.createConversation({ workspaceId: "ws",
      directory: fs.realpathSync(workspace), security: READ_ONLY });
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
