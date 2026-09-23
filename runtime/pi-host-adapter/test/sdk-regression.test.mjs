// SDK migration regressions: the failure class the migration removes
// plus parity anchors that must hold on the new transport.
//
// - A tool event payload well beyond the old 1 MiB RPC line ceiling is
//   summarized to bounded evidence while the session stays usable.
// - prompt -> many tool events -> completion keeps one session usable.
// - getSession/sessionStatus/messages/model discovery/setModel/abort
//   behave as the Bridge expects.
// - Execution journal stays complete and compatible, including
//   extension-tool evidence and permission decisions.
// - Dispose/shutdown clears ownership without killing anything else.
// - Session identity is authoritative, stable, and directory-bound.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision, validatePolicy } from "../policy.mjs";
import { OPTION_ALWAYS, OPTION_ONCE, OPTION_REJECT } from "../trusted-permission-extension.mjs";
import { askPermission, createFakeTransport } from "./fake-sdk.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-sdk-reg-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  fs.writeFileSync(path.join(app, "notes.txt"), "hello\n");
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  return { tmp, root, app: fs.realpathSync(app) };
}

function writablePolicy() {
  return validatePolicy({
    version: 3,
    write_tools_enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**"],
    protected_template_exceptions: [],
    allow_session_always: true,
    external_access: { default_mode: "deny", roots: [] },
    shell_mode: "deny",
  });
}

function makeAdapter(projects, transport, opts = {}) {
  return new PiAdapter({
    projectsRoot: projects.root,
    agentDir: path.join(projects.tmp, "agent-dir"),
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true,
    piVersion: "0.87.0",
    ...opts,
  });
}

async function writableSession(adapter, projects, transport) {
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "reg", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  return { session, entry: adapter.sessions.get(session.id) };
}

test("payload beyond the old 1 MiB ceiling never destroys the session", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const { session, entry } = await writableSession(adapter, projects, transport);
  // 2 MiB tool result: twice the removed MAX_LINE_BYTES limit.
  const big = "z".repeat(2 * 1024 * 1024);
  entry.session.emit({ type: "tool_execution_start", toolCallId: "big-1",
    toolName: "read", args: { path: "notes.txt" } });
  entry.session.emit({ type: "tool_execution_update", toolCallId: "big-1",
    toolName: "read", args: { path: "notes.txt" },
    partialResult: { content: [{ type: "text", text: big }] } });
  entry.session.emit({ type: "tool_execution_end", toolCallId: "big-1",
    toolName: "read", result: { content: [{ type: "text", text: big }] }, isError: false });
  await new Promise((resolve) => setImmediate(resolve));
  // The session is alive and fully usable: status, messages, prompt, and
  // a follow-up tool flow all work after the oversized event.
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "idle");
  assert.deepEqual(await adapter.promptAsync(projects.app, session.id, "continue"), { accepted: true });
  const journal = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 10 });
  const record = journal.updates.find((u) => u.tool_call_id === "big-1");
  assert.ok(record);
  assert.equal(record.state, "completed");
  assert.ok(JSON.stringify(record).length < 100 * 1024);
  await adapter.shutdown();
});

test("prompt fans out to many tool events then completes on one session", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const { session, entry } = await writableSession(adapter, projects, transport);
  await adapter.promptAsync(projects.app, session.id, "review everything");
  entry.session.setStreaming(true);
  for (let i = 0; i < 25; i += 1) {
    const id = `call-${i}`;
    entry.session.emit({ type: "tool_execution_start", toolCallId: id,
      toolName: "read", args: { path: `file-${i}.txt` } });
    entry.session.emit({ type: "tool_execution_end", toolCallId: id,
      toolName: "read", result: { content: [{ type: "text", text: `body ${i}` }] },
      isError: false });
  }
  // One ask in the middle suspends and resumes while the rest flow.
  const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-ask", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  assert.equal(listed.length, 1);
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "once");
  assert.equal(await selectPromise, OPTION_ONCE);
  entry.session.setStreaming(false);
  entry.session.setMessages([
    { role: "user", content: "review everything", timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "done" }],
      timestamp: 2, stopReason: "stop" },
  ]);
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "idle");
  const messages = await adapter.messages(projects.app, session.id, 10);
  assert.equal(messages[messages.length - 1].completed, 2);
  const journal = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 100 });
  assert.equal(journal.audit_gap, false);
  assert.ok(journal.updates.length >= 26);
  const decision = journal.updates.find((u) => u.tool_call_id === "call-ask");
  assert.equal(decision.permission_decision, "once");
  await adapter.shutdown();
});

test("model discovery lists the profile inventory and setModel validates exactly", async () => {
  const projects = makeProjects();
  const models = [
    { provider: "acme", id: "a-1", name: "Acme One" },
    { provider: "opencode-go", id: "big-model", name: "Big Model" },
  ];
  const transport = createFakeTransport({ models });
  const adapter = makeAdapter(projects, transport);
  const { session, entry } = await writableSession(adapter, projects, transport);
  const listed = await adapter.listModels(projects.app);
  assert.deepEqual(listed, [
    { provider: "acme", id: "a-1", name: "Acme One" },
    { provider: "opencode-go", id: "big-model", name: "Big Model" },
  ]);
  // Exact provider/id selector switches the session model, including
  // opencode-go/* provider identities.
  await adapter.promptAsync(projects.app, session.id, "hi", "opencode-go/big-model");
  assert.deepEqual(entry.session.setModelCalls, [models[1]]);
  await assert.rejects(
    adapter.promptAsync(projects.app, session.id, "hi", "acme/missing"), /Unsupported model/);
  await adapter.shutdown();
});

test("abort keeps the session usable and shutdown ends ownership", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const { session, entry } = await writableSession(adapter, projects, transport);
  entry.session.setStreaming(true);
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "busy");
  assert.equal(await adapter.abortSession(projects.app, session.id), true);
  entry.session.setStreaming(false);
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "idle");
  assert.deepEqual(await adapter.promptAsync(projects.app, session.id, "again"), { accepted: true });
  await adapter.shutdown();
  assert.equal(entry.session.disposeCalls, 1);
  assert.equal(adapter.sessionCount, 0);
});

test("session identity is authoritative, stable, and directory-bound", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const first = await adapter.createSession(projects.app, "one");
  const second = await adapter.createSession(projects.app, "two");
  assert.notEqual(first.id, second.id);
  // The Bridge session owns the exact created SDK session id.
  assert.equal(adapter.sessions.get(first.id).session.sessionId, first.id);
  assert.equal(adapter.sessions.get(second.id).session.sessionId, second.id);
  // Identity survives traffic on the session.
  await adapter.promptAsync(projects.app, first.id, "hi");
  assert.equal((await adapter.getSession(projects.app, first.id)).session.id, first.id);
  await adapter.shutdown();
});
