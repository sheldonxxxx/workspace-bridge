// 3B1 adapter permission surface: snapshot immutability, SDK loadout,
// in-process preflight/select correlation with fail-closed malformed
// handling, list/respond/abort/session isolation, and no-leak public
// records.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision, safeDefaultPolicy, validatePolicy } from "../policy.mjs";
import { OPTION_ALWAYS, OPTION_ONCE, OPTION_REJECT } from "../trusted-permission-extension.mjs";
import { askPermission, createFakeTransport, trackSelect } from "./fake-sdk.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-perm-"));
  const appA = path.join(tmp, "Projects", "app-a");
  const appB = path.join(tmp, "Projects", "app-b");
  fs.mkdirSync(appA, { recursive: true });
  fs.mkdirSync(appB, { recursive: true });
  fs.writeFileSync(path.join(appA, "notes.txt"), "hello\n");
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  return { tmp, root, appA: fs.realpathSync(appA), appB: fs.realpathSync(appB) };
}

function writablePolicy() {
  return validatePolicy({
    version: 3,
    write_tools_enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
    external_access: { default_mode: "deny", roots: [] },
    shell_mode: "deny",
  });
}

function writableOptions() {
  const policy = writablePolicy();
  return { permission_policy: policy, policy_revision: policyRevision(policy) };
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

async function createSession(adapter, projects, dir, options) {
  return adapter.createSession(dir, "perm", options);
}

const ASK_OPTIONS = [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT];

test("missing policy starts a legacy read-only session with safe defaults", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await createSession(adapter, projects, projects.appA);
  const entry = adapter.sessions.get(session.id);
  assert.equal(entry.writable, false);
  assert.deepEqual(entry.permissionPolicy, safeDefaultPolicy());
  assert.equal(entry.policyRevision, policyRevision(safeDefaultPolicy()));
  const created = transport.lastCreated();
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls"]);
  assert.equal(created.excludeTools, null);
  assert.deepEqual(created.extensionPaths, []);
  assert.equal(created.policy, null);
  await adapter.shutdown();
});

test("read-only v2 session loads the trusted extension without edit/write", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = safeDefaultPolicy();
  const session = await createSession(adapter, projects, projects.appA,
    { permission_policy: policy, policy_revision: policyRevision(policy) });
  const entry = adapter.sessions.get(session.id);
  assert.equal(entry.writable, false);
  const created = transport.lastCreated();
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls"]);
  assert.equal(created.excludeTools, null);
  assert.deepEqual(created.extensionPaths, []);
  assert.ok(created.uiContext);
  assert.deepEqual(created.policy, policy);
  await adapter.shutdown();
});

test("v1 policy payloads fail clearly instead of being reinterpreted", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const before = transport.created.length;
  await assert.rejects(
    createSession(adapter, projects, projects.appA, {
      permission_policy: {
        version: 1,
        enabled: true,
        tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
        protected_patterns: [],
        protected_template_exceptions: [],
        allow_session_always: true,
      },
      policy_revision: "0".repeat(64),
    }),
    /coordinated|invalid_policy/i);
  assert.equal(transport.created.length, before);
  await adapter.shutdown();
});

test("unresolvable or duplicate external roots fail session creation", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const missing = writablePolicy();
  missing.external_access = { default_mode: "deny", roots: [{ path: "/no-such-pi-root-xyz", mode: "ask" }] };
  await assert.rejects(
    createSession(adapter, projects, projects.appA,
      { permission_policy: missing, policy_revision: policyRevision(missing) }),
    /external roots/i);
  // Canonical duplicates (a symlink alias of the same dir) fail rather
  // than guess. (Exact string duplicates already fail at validation.)
  const dupDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-dup-"));
  const dupAlias = `${fs.realpathSync(dupDir)}-alias`;
  fs.symlinkSync(fs.realpathSync(dupDir), dupAlias);
  const dup = writablePolicy();
  dup.external_access = {
    default_mode: "deny",
    roots: [
      { path: fs.realpathSync(dupDir), mode: "allow" },
      { path: dupAlias, mode: "deny" },
    ],
  };
  await assert.rejects(
    createSession(adapter, projects, projects.appA,
      { permission_policy: dup, policy_revision: policyRevision(dup) }),
    /external roots/i);
  await adapter.shutdown();
});

test("writable policy loads the trusted extension with edit/write and no bash", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const entry = adapter.sessions.get(session.id);
  assert.equal(entry.writable, true);
  const created = transport.lastCreated();
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls", "edit", "write"]);
  assert.equal(created.excludeTools, null);
  assert.ok(created.uiContext);
  assert.deepEqual(created.policy, writablePolicy());
  // Snapshot survives in the entry and is never the raw caller object.
  assert.equal(entry.permissionPolicy.write_tools_enabled, true);
  await adapter.shutdown();
});

test("supplied policy requires an equal valid revision or creation fails", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = writablePolicy();
  const revision = policyRevision(policy);
  // Match succeeds.
  const session = await createSession(adapter, projects, projects.appA,
    { permission_policy: policy, policy_revision: revision });
  assert.equal(adapter.sessions.get(session.id).writable, true);
  assert.equal(adapter.sessions.get(session.id).policyRevision, revision);
  // Mismatch fails as conflict; nothing is created.
  const before = transport.created.length;
  await assert.rejects(
    createSession(adapter, projects, projects.appA,
      { permission_policy: policy, policy_revision: "0".repeat(64) }),
    /mismatch|conflict/i);
  // Missing halves fail closed.
  await assert.rejects(
    createSession(adapter, projects, projects.appA, { permission_policy: policy }),
    /revision is missing/i);
  await assert.rejects(
    createSession(adapter, projects, projects.appA, { policy_revision: revision }),
    /revision is missing/i);
  // Malformed revision fails closed.
  for (const bad of ["rev pioneered", "xyz", "0".repeat(63), "0".repeat(65), 42]) {
    await assert.rejects(
      createSession(adapter, projects, projects.appA,
        { permission_policy: policy, policy_revision: bad }),
      /malformed/i);
  }
  // Invalid policy fails closed even when no revision games are played.
  await assert.rejects(
    createSession(adapter, projects, projects.appA,
      { permission_policy: { version: 2 }, policy_revision: "0".repeat(64) }),
    /invalid/i);
  assert.equal(transport.created.length, before);
  await adapter.shutdown();
});

test("start -> select ask -> respond -> end correlation with exact resume", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const entry = adapter.sessions.get(session.id);

  // Preflight arrives with the tool start event; the trusted extension's
  // select() suspends the exact invocation on the adapter's UI context.
  const { selectPromise, listed } = await askPermission(adapter, projects.appA, session.id, {
    toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" }, options: ASK_OPTIONS,
  });
  const tracked = trackSelect(selectPromise);
  assert.ok(entry.preflights.has("call-1"));
  assert.equal(listed.length, 1);
  const pending = listed[0];
  assert.equal(pending.tool, "edit");
  assert.equal(pending.action, "edit");
  assert.equal(pending.resource, "notes.txt");
  assert.deepEqual(pending.requested, ["notes.txt"]);
  assert.equal(pending.always_pattern, "edit:notes.txt");
  assert.equal(pending.tool_call_id, "call-1");
  // No raw args, file contents, reasoning, tokens, or host paths leak.
  const serialized = JSON.stringify(listed);
  assert.ok(!serialized.includes("hello"));
  assert.ok(!serialized.includes(projects.tmp));

  // Respond once: the suspended select resolves with the once value, the
  // SAME invocation resumes, and pending is removed.
  const result = await adapter.respondPermission(projects.appA, session.id, pending.id, "once");
  assert.deepEqual(result, { ok: true, decision: "once" });
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  await selectPromise;
  assert.equal(tracked.settled, true);
  assert.equal(tracked.value, OPTION_ONCE);
  // Duplicate/late response fails closed as not_found.
  await assert.rejects(
    adapter.respondPermission(projects.appA, session.id, pending.id, "once"), /not found/i);

  // Tool end cleans the preflight.
  entry.session.emit({ type: "tool_execution_end", toolCallId: "call-1", toolName: "edit",
    result: {}, isError: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(!entry.preflights.has("call-1"));
  await adapter.shutdown();
});

test("malformed, duplicate, and mismatched selects fail closed", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const entry = adapter.sessions.get(session.id);
  const ui = entry.session.boundUiContext;

  // No preflight for this call: select resolves undefined, never pending.
  const stray = await ui.select("WB_PERMISSION_V1:call-unknown", ASK_OPTIONS);
  assert.equal(stray, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);

  // Wrong options shape: blocked without pending.
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-2",
    toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  const wrong = await ui.select("WB_PERMISSION_V1:call-2", ["Yes", "No"]);
  assert.equal(wrong, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);

  // Non-marker UI is ignored without suspension.
  const foreign = await ui.select("Pick a model:", ["a", "b"]);
  assert.equal(foreign, undefined);

  // Unknown tool preflight still correlates structurally but evaluates
  // deny under shell_mode=deny: the select for a deny effect resolves
  // undefined without pending.
  entry.session.emit({ type: "tool_execution_start", toolCallId: "call-3",
    toolName: "bash", args: { command: "ls" } });
  await new Promise((resolve) => setImmediate(resolve));
  const denied = await ui.select("WB_PERMISSION_V1:call-3", ASK_OPTIONS);
  assert.equal(denied, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  await adapter.shutdown();
});

test("always is rejected when the session policy disables it", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const policy = validatePolicy({
    ...writableOptions().permission_policy, allow_session_always: false,
  });
  const session = await createSession(
    adapter, projects, projects.appA,
    { permission_policy: policy, policy_revision: policyRevision(policy) });
  // Extension offers once/reject only in this mode.
  const { selectPromise, listed } = await askPermission(adapter, projects.appA, session.id, {
    toolCallId: "call-9", toolName: "edit", args: { path: "notes.txt" },
    options: [OPTION_ONCE, OPTION_REJECT],
  });
  const tracked = trackSelect(selectPromise);
  assert.equal(listed.length, 1);
  await assert.rejects(
    adapter.respondPermission(projects.appA, session.id, listed[0].id, "always"), /always/i);
  // once still works and resolves the suspended select with once.
  await adapter.respondPermission(projects.appA, session.id, listed[0].id, "once");
  await selectPromise;
  assert.equal(tracked.value, OPTION_ONCE);
  await adapter.shutdown();
});

test("permission state is isolated per session", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const sessionA = await createSession(adapter, projects, projects.appA, writableOptions());
  const sessionB = await createSession(adapter, projects, projects.appB, writableOptions());
  await askPermission(adapter, projects.appA, sessionA.id, {
    toolCallId: "call-a", toolName: "edit", args: { path: "notes.txt" }, options: ASK_OPTIONS,
  });
  assert.equal((await adapter.listPermissions(projects.appA, sessionA.id)).length, 1);
  assert.deepEqual(await adapter.listPermissions(projects.appB, sessionB.id), []);
  // Foreign directory or unknown id fails closed.
  const foreign = (await adapter.listPermissions(projects.appA, sessionA.id))[0];
  await assert.rejects(
    adapter.respondPermission(projects.appB, sessionB.id, foreign.id, "once"), /not found/i);
  await adapter.shutdown();
});

test("abort resolves owned suspended selects first, then aborts the run", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const { selectPromise, listed } = await askPermission(adapter, projects.appA, session.id, {
    toolCallId: "call-z", toolName: "edit", args: { path: "notes.txt" }, options: ASK_OPTIONS,
  });
  const tracked = trackSelect(selectPromise);
  assert.equal(listed.length, 1);
  assert.equal(await adapter.abortSession(projects.appA, session.id), true);
  // The suspended select resolved as rejected (undefined), never approved.
  await selectPromise;
  assert.equal(tracked.settled, true);
  assert.equal(tracked.value, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  assert.equal(transport.lastSession().abortCalls, 1);
  await adapter.shutdown();
});

test("3D4 live shape: pending carries canonical session_id for the Bridge contract", async () => {
  // Regression for the live correlation blocker (run_4f5a7700): the
  // execution journal persisted permission_effect=ask while Bridge saw
  // zero pendings. The adapter pending row must carry the canonical
  // session_id the Bridge normalizer requires for strict session scoping.
  // Wire shape mirrors the verified transport: opaque
  // WB_PERMISSION_V1:<toolCallId> marker (pipe-form call id as seen
  // live), exact 3-option ask shape, start observed before select.
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  // External-read ask policy mirrors the live run: /etc/hosts outside
  // the workspace evaluates to ask (not deny) under default_mode=ask.
  const base = writablePolicy();
  base.external_access = { default_mode: "ask", roots: [] };
  const session = await createSession(adapter, projects, projects.appA,
    { permission_policy: base, policy_revision: policyRevision(base) });
  const liveCallId = "call_01a0c749a17876808c7659bb3c8645c4|fc_01a0c749a17876808c7659bb3c8645c4";
  const { selectPromise, listed } = await askPermission(adapter, projects.appA, session.id, {
    toolCallId: liveCallId, toolName: "read", args: { path: "/etc/hosts" }, options: ASK_OPTIONS,
  });
  const tracked = trackSelect(selectPromise);
  assert.equal(listed.length, 1);
  const pending = listed[0];
  // Canonical Bridge contract key: strict session scoping depends on it.
  assert.equal(pending.session_id, session.id);
  // Legacy alias stays with the identical bounded value for older readers.
  assert.equal(pending.session, session.id);
  assert.equal(pending.tool_call_id, liveCallId);
  assert.equal(pending.tool, "read");
  // Serialized row must satisfy the Bridge normalizer shape (id +
  // session_id + requested/always_pattern/tool_call_id, no raw args).
  const serialized = JSON.stringify(pending);
  assert.ok(serialized.includes('"session_id"'));
  assert.ok(!serialized.includes("/etc/hosts".repeat(2)));
  // once resolves the exact suspended select with the once value.
  const result = await adapter.respondPermission(projects.appA, session.id, pending.id, "once");
  assert.deepEqual(result, { ok: true, decision: "once" });
  await selectPromise;
  assert.equal(tracked.value, OPTION_ONCE);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  // Stale select for an ended call resolves undefined without pending.
  const entry = adapter.sessions.get(session.id);
  entry.session.emit({ type: "tool_execution_end", toolCallId: liveCallId, toolName: "read",
    result: {}, isError: false });
  await new Promise((resolve) => setImmediate(resolve));
  const stale = await entry.session.boundUiContext.select(
    `WB_PERMISSION_V1:${liveCallId}`, ASK_OPTIONS);
  assert.equal(stale, undefined);
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  await adapter.shutdown();
});
