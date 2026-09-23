// End-to-end permission correlation in-process: the REAL trusted
// permission extension factory (parameterized, per-session policy)
// drives tool_call interception against the adapter's bound UI context.
// A simulated agent emits tool_execution_start, the extension's ask path
// suspends on select(), the test answers via the Bridge HTTP-equivalent
// respondPermission, and the SAME suspended invocation resumes with the
// decision -- once, always (grant short-circuit), and reject. No
// provider, no spawn, no JSONL.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision, validatePolicy } from "../policy.mjs";
import {
  createTrustedPermissionExtension,
} from "../trusted-permission-extension.mjs";
import { createFakeTransport, trackSelect } from "./fake-sdk.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-perm-sdk-"));
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

async function setup() {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({
    projectsRoot: projects.root,
    agentDir: path.join(projects.tmp, "agent-dir"),
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true,
    piVersion: "0.87.0",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "sdk-ask", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  // Bind the REAL parameterized trusted factory to a capturing pi stub,
  // with the adapter's bound UI context as the extension ctx.
  let toolCallHandler = null;
  const factory = createTrustedPermissionExtension({ policy, cwd: entry.cwd });
  factory({ on: (event, fn) => { if (event === "tool_call") toolCallHandler = fn; } });
  assert.ok(toolCallHandler);
  const ctx = { ui: entry.session.boundUiContext };
  return { projects, adapter, session, entry, toolCallHandler, ctx };
}

function emitStart(entry, toolCallId, toolName, args) {
  entry.session.emit({ type: "tool_execution_start", toolCallId, toolName, args });
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("real factory ask suspends and resumes the same invocation on once", async () => {
  const { projects, adapter, session, entry, toolCallHandler, ctx } = await setup();
  emitStart(entry, "call-ui-1", "edit", { path: "notes.txt" });
  await settle();
  // The extension suspends the exact call on the adapter UI context.
  const suspended = toolCallHandler(
    { toolName: "edit", toolCallId: "call-ui-1", input: { path: "notes.txt" } }, ctx);
  const tracked = trackSelect(suspended);
  let pending = null;
  for (let i = 0; i < 200 && !pending; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 10));
    const listed = await adapter.listPermissions(projects.app, session.id);
    if (listed.length) pending = listed[0];
  }
  assert.ok(pending, "expected a pending permission from the real factory ask");
  assert.equal(pending.tool, "edit");
  assert.equal(pending.resource, "notes.txt");
  assert.equal(pending.tool_call_id, "call-ui-1");

  const answered = await adapter.respondPermission(projects.app, session.id, pending.id, "once");
  assert.deepEqual(answered, { ok: true, decision: "once" });
  // The SAME suspended invocation resumes with the once decision: the
  // extension allows the exact call (undefined = proceed, no retry). A
  // reject/unknown choice would have returned {block:true} instead.
  const outcome = await suspended;
  assert.equal(outcome, undefined);
  assert.equal(tracked.settled, true);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Journal links the decision to the exact tool call.
  const journal = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 10 });
  const record = journal.updates.find((u) => u.tool_call_id === "call-ui-1");
  assert.ok(record);
  assert.equal(record.permission_decision, "once");
  assert.equal(record.permission_effect, "ask");
  await adapter.shutdown();
});

test("real factory always grants the exact target for the session", async () => {
  const { projects, adapter, session, entry, toolCallHandler, ctx } = await setup();
  emitStart(entry, "call-a1", "edit", { path: "notes.txt" });
  await settle();
  const first = toolCallHandler(
    { toolName: "edit", toolCallId: "call-a1", input: { path: "notes.txt" } }, ctx);
  await settle();
  const listed = await adapter.listPermissions(projects.app, session.id);
  assert.equal(listed.length, 1);
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "always");
  assert.equal(await first, undefined);
  // A second identical call short-circuits on the exact session grant:
  // no new pending, same invocation proceeds without suspension.
  emitStart(entry, "call-a2", "edit", { path: "notes.txt" });
  await settle();
  const second = await toolCallHandler(
    { toolName: "edit", toolCallId: "call-a2", input: { path: "notes.txt" } }, ctx);
  assert.equal(second, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  await adapter.shutdown();
});

test("real factory reject blocks the exact call without retry", async () => {
  const { projects, adapter, session, entry, toolCallHandler, ctx } = await setup();
  emitStart(entry, "call-r1", "edit", { path: "notes.txt" });
  await settle();
  const suspended = toolCallHandler(
    { toolName: "edit", toolCallId: "call-r1", input: { path: "notes.txt" } }, ctx);
  await settle();
  const listed = await adapter.listPermissions(projects.app, session.id);
  assert.equal(listed.length, 1);
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "reject");
  const outcome = await suspended;
  assert.deepEqual(outcome, { block: true, reason: "Rejected" });
  await adapter.shutdown();
});

test("real factory deny never suspends and real factory allow never asks", async () => {
  const { adapter, session, entry, toolCallHandler, ctx } = await setup();
  // Deny: shell is denied by policy -- blocked without any pending.
  emitStart(entry, "call-d1", "bash", { command: "ls", timeoutMs: 30000 });
  await settle();
  const denied = await toolCallHandler(
    { toolName: "bash", toolCallId: "call-d1", input: { command: "ls", timeoutMs: 30000 } }, ctx);
  assert.equal(denied.block, true);
  // Allow: read is allowed -- proceeds without any pending.
  emitStart(entry, "call-ok", "read", { path: "notes.txt" });
  await settle();
  const allowed = await toolCallHandler(
    { toolName: "read", toolCallId: "call-ok", input: { path: "notes.txt" } }, ctx);
  assert.equal(allowed, undefined);
  await adapter.shutdown();
  void session;
});
