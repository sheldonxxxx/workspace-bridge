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
      this.sessions.set("native-session", { sessionFile });
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
    const listed = await (await fetch(`${base}/v1/models`, { headers })).json();
    assert.equal(listed.models[0].selector, "provider/model");
    const profile = (await (await fetch(`${base}/v1/profiles`, { headers })).json())
      .profiles[0];
    const response = await fetch(`${base}/v1/conversations`, {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ workspaceId: "ws-one", directory,
        securityProfile: { id: profile.id, revision: profile.revision } }),
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).workspaceId, "ws-one");
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
