import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { PiRuntimeProtocol } from "../wbrp.mjs";
import { PiAdapter } from "../adapter.mjs";
import { ExecutionStore } from "../execution-store.mjs";
import { policyRevision, safeDefaultPolicy } from "../policy.mjs";
import { durableFixture, boundToolHook, flush } from "./durable-fixture.mjs";

const crashChild = fileURLToPath(new URL("./durable-crash-child.mjs", import.meta.url));

function crashAt(root, boundary) {
  const child = spawnSync(process.execPath, [crashChild, root, boundary], {
    encoding: "utf8", timeout: 15000,
  });
  assert.equal(child.signal, "SIGKILL", `crash child failed: ${child.stderr}`);
  return JSON.parse(fs.readFileSync(path.join(root, "crash-metadata.json"), "utf8"));
}

for (const [boundary, state, effects, decision] of [
  ["before_effect", "interrupted", 0, ""],
  ["after_effect", "interrupted", 1, ""],
  ["after_completion", "completed", 1, ""],
  ["waiting_approval", "interrupted", 0, ""],
  ["after_approval", "interrupted", 0, "always"],
  ["failed_approval", "interrupted", 0, ""],
  ["failed_completion", "interrupted", 1, ""],
  ["failed_intent", null, 0, ""],
]) {
  test(`SIGKILL at ${boundary}: recover tool evidence without replay`, async (t) => {
    const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-crash-")));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const metadata = crashAt(root, boundary);
    const fixture = durableFixture(root);
    t.after(() => fixture.adapter.shutdown());
    const protocol = new PiRuntimeProtocol(fixture.adapter);
    const run = await protocol.run(metadata.runId);
    assert.equal(run.phase, "terminal");
    assert.equal(run.outcome, "interrupted");
    assert.equal(run.error, "adapter_restarted");
    assert.ok(!Object.hasOwn(run, "auditBinding"));
    assert.ok(!Object.hasOwn(run, "executionFloor"));
    const records = (await protocol.activityList(run.id)).activities;
    assert.equal(records.length, state ? 1 : 0);
    if (state) {
      assert.equal(records[0].nativeId, "write-once");
      assert.equal(records[0].status, state);
      assert.equal(records[0].input.permissionDecision, decision);
      assert.ok(!JSON.stringify(records).includes("PRIVATE-WRITE-CONTENT"));
      assert.match(records[0].input.details.content_sha256, /^[a-f0-9]{64}$/);
    }
    assert.deepEqual(await protocol.interactions(run.id), { interactions: [] });
    if (metadata.interactionId) {
      await assert.rejects(() => protocol.resolve(metadata.interactionId, { choiceId: "always" }),
        { code: "interaction_stale" });
    }
    const retried = await protocol.startRun(metadata.conversationId, {
      input: metadata.input, clientRunId: metadata.clientRunId,
    });
    assert.equal(retried.id, run.id);
    assert.equal(retried.outcome, "interrupted");
    assert.equal(fixture.adapter.sessionCount, 0, "evidence recovery must not hydrate or prompt a session");
    assert.equal(fixture.transport.sessions.length, 0);
    const effectsFile = path.join(root, "effects.log");
    const effectCount = () => fs.existsSync(effectsFile)
      ? fs.readFileSync(effectsFile, "utf8").trim().split("\n").length : 0;
    assert.equal(effectCount(), effects);
    const reopened = new PiRuntimeProtocol(durableFixture(root).adapter);
    assert.deepEqual((await reopened.activityList(run.id)).activities, records);
    assert.equal(effectCount(), effects);
    assert.ok(!JSON.stringify(metadata.logs).includes("raw-secret-path"));
  });
}

test("restart retains approval evidence but requires a new approval for the same target", async (t) => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-grant-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const metadata = crashAt(root, "after_approval");
  const fixture = durableFixture(root);
  t.after(() => fixture.adapter.shutdown());
  const protocol = new PiRuntimeProtocol(fixture.adapter);
  assert.equal((await protocol.conversation(metadata.conversationId)).status, "idle");
  const hook = boundToolHook(fixture.transport);
  const invocation = hook({ toolCallId: "new-call", toolName: "write",
    input: { path: "notes.txt", content: "new content" } });
  await flush();
  const pending = await fixture.adapter.listPermissions(fixture.directory, metadata.sessionId);
  assert.equal(pending.length, 1, "persisted always decision must not restore a grant");
  await fixture.adapter.respondPermission(fixture.directory, metadata.sessionId, pending[0].id, "reject");
  assert.equal((await invocation).block, true);
});

test("real SDK tool hook awaits durable intent and blocks on storage failure without a provider", async (t) => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-sdk-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = durableFixture(root);
  const adapter = new PiAdapter({ projectsRoot: fixture.adapter.projectsRoot, agentDir: fixture.agentDir });
  t.after(() => adapter.shutdown());
  const policy = { ...safeDefaultPolicy(), write_tools_enabled: true,
    tools: { ...safeDefaultPolicy().tools, write: "allow" } };
  const session = await adapter.createSession(fixture.directory, "real-sdk-gate", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  const invoke = (id) => entry.session.agent.beforeToolCall({
    toolCall: { id, name: "write" }, args: { path: "notes.txt", content: "private content" },
  });
  assert.equal(await invoke("real-hook"), undefined);
  const stored = new ExecutionStore({ agentDir: fixture.agentDir,
    binding: adapter.executionBinding(fixture.directory, session.id) }).load();
  assert.equal(stored.records[0].tool_call_id, "real-hook");
  assert.ok(!JSON.stringify(stored).includes("private content"));
  entry.journal.onCommit = () => { throw new Error("private failure"); };
  const blocked = await invoke("failed-hook");
  assert.equal(blocked.block, true);
  assert.equal(blocked.reason, "Execution audit unavailable; failing closed");
  assert.equal(fs.existsSync(path.join(fixture.directory, "notes.txt")), false);
  await assert.rejects(() => adapter.readExecutions(fixture.directory, session.id),
    { code: "execution_audit_unavailable" });
});

test("the intent gate correlates redacted commands and full extension names, then blocks a missing journal", async (t) => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-gate-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = durableFixture(root);
  t.after(() => fixture.adapter.shutdown());
  const policy = { ...safeDefaultPolicy(), shell_mode: "allow" };
  const created = await fixture.adapter.createSession(fixture.directory, "gate", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const session = fixture.transport.lastSession();
  const hook = boundToolHook(fixture.transport);
  for (const [id, toolName, input] of [
    ["command", "bash", { command: "curl https://user:pass@example.test/?token=PRIVATE-TOKEN" }],
    ["extension", `extension_${"x".repeat(110)}`, { target: "item", credential: "PRIVATE-ARGUMENT" }],
  ]) {
    session.emit({ type: "tool_execution_start", toolCallId: id, toolName, args: input });
    assert.equal(await hook({ toolCallId: id, toolName, input }), undefined);
  }
  const records = (await fixture.adapter.readExecutions(fixture.directory, created.id)).updates;
  assert.equal(records[1].tool.length, 120);
  assert.ok(!JSON.stringify(records).includes("PRIVATE-TOKEN"));
  assert.ok(!JSON.stringify(records).includes("PRIVATE-ARGUMENT"));
  assert.match(records[0].input_summary.command_sha256, /^[a-f0-9]{64}$/);
  const store = new ExecutionStore({ agentDir: fixture.agentDir,
    binding: fixture.adapter.executionBinding(fixture.directory, created.id) });
  fs.unlinkSync(store.file);
  const blocked = await hook({ toolCallId: "missing-file", toolName: "read", input: { path: "notes.txt" } });
  assert.equal(blocked.block, true);
  await assert.rejects(() => fixture.adapter.readExecutions(fixture.directory, created.id),
    { code: "execution_audit_unavailable" });
  assert.equal(fs.existsSync(store.file), false);
});

for (const failure of ["missing", "corrupt", "session_binding", "security_binding"]) {
  test(`restart reports an audit gap for ${failure} evidence without adopting a session`, async (t) => {
    const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-gap-")));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const metadata = crashAt(root, "after_effect");
    const fixture = durableFixture(root);
    const stateFile = path.join(fixture.agentDir, "wbrp-state.json");
    const saved = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    const row = saved.runs.find((run) => run.id === metadata.runId);
    const store = new ExecutionStore({ agentDir: fixture.agentDir, binding: row.auditBinding });
    if (failure === "missing") fs.unlinkSync(store.file);
    if (failure === "corrupt") fs.appendFileSync(store.file, "{\"invalid\":true}\n");
    if (failure === "session_binding") row.auditBinding.sessionId = "unrelated-session";
    if (failure === "security_binding") row.auditBinding.policyRevision = "different-policy";
    fs.writeFileSync(stateFile, JSON.stringify(saved));
    const protocol = new PiRuntimeProtocol(fixture.adapter);
    const recovered = await protocol.run(metadata.runId);
    assert.equal(recovered.outcome, "interrupted");
    assert.equal(recovered.error, "execution_audit_unavailable");
    const activities = (await protocol.activityList(metadata.runId)).activities;
    assert.equal(activities.length, 1);
    assert.equal(activities[0].kind, "audit_gap");
    assert.equal(fixture.adapter.sessionCount, 0);
    assert.equal(fixture.transport.sessions.length, 0);
    assert.equal(fs.readFileSync(path.join(root, "effects.log"), "utf8"), "one-effect\n");
  });
}

test("restart recovery uses the run start floor and excludes older tool calls", async (t) => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-durable-floor-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = durableFixture(root);
  const protocol = new PiRuntimeProtocol(fixture.adapter);
  const conversation = await protocol.createConversation({ workspaceId: "workspace-test",
    directory: fixture.directory, securityProfile: { id: "read-only",
      revision: protocol.profiles.get("read-only").revision } });
  const hook = boundToolHook(fixture.transport);
  assert.equal(await hook({ toolCallId: "older-call", toolName: "read", input: { path: "old.txt" } }), undefined);
  const run = await protocol.startRun(conversation.id, {
    input: [{ type: "text", text: "Read the new file" }], clientRunId: "next-run",
  });
  assert.equal(await hook({ toolCallId: "owned-call", toolName: "read", input: { path: "new.txt" } }), undefined);
  fixture.adapter.shutdown();
  const recoveredFixture = durableFixture(root);
  const recovered = new PiRuntimeProtocol(recoveredFixture.adapter);
  const activities = (await recovered.activityList(run.id)).activities;
  assert.deepEqual(activities.map((activity) => activity.nativeId), ["owned-call"]);
  assert.equal(activities[0].status, "interrupted");
  assert.equal(recoveredFixture.adapter.sessionCount, 0);
});
