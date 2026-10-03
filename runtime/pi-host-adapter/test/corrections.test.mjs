// Audit corrections: HTTP policy transport, revision binding, opaque
// marker grammar, missing-preflight approval refusal, minimal preflight
// retention, and infallible in-process select resolution.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { parseOpaqueMarker } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision, safeDefaultPolicy, validatePolicy } from "../policy.mjs";
import { OPTION_ALWAYS, OPTION_ONCE, OPTION_REJECT } from "../trusted-permission-extension.mjs";
import { askPermission, createFakeTransport, trackSelect } from "./fake-sdk.mjs";

const TOKEN = "shared-private-token";
const AUTH = { "X-Runtime-Token": TOKEN };

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-corr-"));
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

function makeAdapter(projects, transport) {
  return new PiAdapter({
    projectsRoot: projects.root,
    agentDir: path.join(projects.tmp, "agent-dir"),
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true,
    piVersion: "0.87.0",
  });
}


// ------------------------------------------------------- 4. opaque marker
test("opaque marker grammar accepts only prefix + bare toolCallId", () => {
  assert.equal(parseOpaqueMarker("WB_PERMISSION_V1:call-1"), "call-1");
  assert.equal(parseOpaqueMarker("WB_PERMISSION_V1:550e8400-e29b-41d4-a716-446655440000"),
    "550e8400-e29b-41d4-a716-446655440000");
  for (const bad of ["", "Pick one:", "WB_PERMISSION_V1:", "WB_PERMISSION_V1:call-1 edit notes.txt",
      "WB_PERMISSION_V1:call-1 ", "WB_PERMISSION_V1: call-1", "WB_PERMISSION_V1:call 1",
      "WB_PERMISSION_V1:" + "x".repeat(201), "wb_permission_v1:call-1", "WB_PERMISSION_V2:call-1"]) {
    assert.equal(parseOpaqueMarker(bad), "", bad.slice(0, 40));
  }
});

test("suffixed or malformed marker titles fail closed without pending", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "m", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  const ui = entry.session.boundUiContext;
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-1", toolName: "edit",
    args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  // Old suffixed shape is rejected: resolves undefined, never pending.
  const old = await ui.select("WB_PERMISSION_V1:call-1 edit notes.txt",
    [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT]);
  assert.equal(old, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Empty id is rejected the same way.
  const empty = await ui.select("WB_PERMISSION_V1:",
    [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT]);
  assert.equal(empty, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Foreign (non-marker) UI is ignored without suspension.
  const foreign = await ui.select("Pick a model:", ["a", "b"]);
  assert.equal(foreign, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Preflight metadata (not title text) still identifies the pending call.
  const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  assert.equal(listed.length, 1);
  assert.equal(listed[0].resource, "notes.txt");
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "once");
  assert.equal(await selectPromise, OPTION_ONCE);
  await adapter.shutdown();
});

// --------------------------------------- 3. missing preflight at approval
test("once/always without the exact preflight cannot approve", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "s", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  async function askOnce(callId) {
    const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
      toolCallId: callId, toolName: "edit", args: { path: "notes.txt" },
      options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
    });
    assert.equal(listed.length, 1);
    return { selectPromise, tracked: trackSelect(selectPromise), pending: listed[0] };
  }
  const first = await askOnce("call-1");
  // Evict the preflight (tool end raced, map overflow): once and always
  // must conflict and must NOT resolve an approval value.
  entry.preflights.delete("call-1");
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, first.pending.id, "once"), /scope changed/i);
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, first.pending.id, "always"),
    /not found|scope changed/i);
  await first.selectPromise;
  assert.equal(first.tracked.settled, true);
  assert.equal(first.tracked.value, undefined);
  assert.ok(first.tracked.value !== OPTION_ONCE && first.tracked.value !== OPTION_ALWAYS);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Duplicate/late request stays rejected.
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, first.pending.id, "once"), /not found/i);
  // Stale reject still resumes the suspended call as rejected.
  const second = await askOnce("call-2");
  entry.preflights.delete("call-2");
  const rejected = await adapter.respondPermission(projects.app, session.id, second.pending.id, "reject");
  assert.deepEqual(rejected, { ok: true, decision: "reject" });
  await second.selectPromise;
  assert.equal(second.tracked.value, undefined);
  await adapter.shutdown();
});

// ------------------------- stale resolution always settles in-process
test("stale responds clear pending and settle the suspended select", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "st", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  const tracked = trackSelect(selectPromise);
  entry.preflights.delete("call-1");
  // In-process resolution cannot fail: stale once conflicts but still
  // settles the suspended call as rejected and clears pending.
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, listed[0].id, "once"),
    /scope changed/i);
  await selectPromise;
  assert.equal(tracked.settled, true);
  assert.equal(tracked.value, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  await adapter.shutdown();
});

test("malformed selects never suspend and leave the session usable", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "fc", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  const ui = entry.session.boundUiContext;
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-1", toolName: "edit",
    args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  // A malformed trusted-shaped ask resolves undefined immediately: no
  // untracked suspension, no session death.
  const malformed = await ui.select("WB_PERMISSION_V1:call-1 edit notes.txt",
    [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT]);
  assert.equal(malformed, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // The session stays usable: a well-formed ask still suspends and
  // resolves afterwards.
  const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  assert.equal(listed.length, 1);
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "once");
  assert.equal(await selectPromise, OPTION_ONCE);
  await adapter.shutdown();
});

// ------------------------------------------------------- 5. minimal data
test("preflight and public state retain only bounded path identity", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "d", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const entry = adapter.sessions.get(session.id);
  const secret = "sk-live-secret-" + "y".repeat(20000);
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-w", toolName: "write",
    args: { path: "notes.txt", content: secret } });
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-e", toolName: "edit",
    args: { path: "notes.txt", edits: [{ oldText: secret, newText: secret }] } });
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-g", toolName: "grep",
    args: { pattern: secret, path: "notes.txt", unrelated: { nested: secret } } });
  await new Promise((resolve) => setImmediate(resolve));
  const stored = JSON.stringify([...entry.preflights.entries()]);
  assert.ok(!stored.includes("sk-live-secret"));
  assert.ok(stored.length < 2000);
  assert.deepEqual(entry.preflights.get("call-w").input, { path: "notes.txt" });
  assert.deepEqual(entry.preflights.get("call-e").input, { path: "notes.txt" });
  assert.deepEqual(entry.preflights.get("call-g").input, { path: "notes.txt" });
  const { listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-w", toolName: "write", args: { path: "notes.txt", content: secret },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  assert.ok(!JSON.stringify(listed).includes("sk-live-secret"));
  await adapter.shutdown();
});

// ------------------------------------------------------- 6. settled resolve
test("respond always settles the suspended select in-process", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "u", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const { selectPromise, listed } = await askPermission(adapter, projects.app, session.id, {
    toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  assert.equal(listed.length, 1);
  // In-process resolution is infallible: the suspended select always
  // settles and pending is always removed on response.
  await adapter.respondPermission(projects.app, session.id, listed[0].id, "once");
  assert.equal(await selectPromise, OPTION_ONCE);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  await adapter.shutdown();
});
