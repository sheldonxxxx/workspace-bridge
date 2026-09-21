// 3B1 adapter permission surface: snapshot immutability, spawn contract,
// RPC preflight/UI correlation with fail-closed malformed handling,
// list/respond/abort/session isolation, and no-leak public records.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision, safeDefaultPolicy, validatePolicy } from "../policy.mjs";
import { OPTION_ALWAYS, OPTION_ONCE, OPTION_REJECT } from "../trusted-permission-extension.mjs";
import { createFakeSpawn, respondState } from "./helpers.mjs";

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
    version: 1,
    enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
  });
}

function writableOptions() {
  const policy = writablePolicy();
  return { permission_policy: policy, policy_revision: policyRevision(policy) };
}

function autoSpawn() {
  const bag = createFakeSpawn();
  let n = 0;
  function spawnFn(binary, args, opts) {
    const child = bag.spawnFn(binary, args, opts);
    child.autoExitCode = 0;
    n += 1;
    const sid = `ses-perm-${n}`;
    child.on("stdin", (line) => {
      for (const raw of String(line).split("\n").filter(Boolean)) {
        let req;
        try {
          req = JSON.parse(raw);
        } catch {
          continue;
        }
        // extension_ui_response lines are recorded, never answered.
        if (req && req.type === "extension_ui_response") continue;
        setImmediate(() => {
          if (!req || typeof req !== "object") return;
          if (req.type === "get_state") respondState(child, req.id, { sessionId: sid });
          else if (req.type === "abort") {
            child.respond({ id: req.id, type: "response", command: "abort", success: true });
          }
        });
      }
    });
    return child;
  }
  return { ...bag, spawnFn };
}

function makeAdapter(projects, spawnBag, opts = {}) {
  return new PiAdapter({
    projectsRoot: projects.root,
    piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"),
    timeoutMs: 2000,
    spawnFn: spawnBag.spawnFn,
    piUsable: true,
    piVersion: "0.86.1",
    ...opts,
  });
}

async function createSession(adapter, projects, dir, options) {
  const promise = adapter.createSession(dir, "perm", options);
  await Promise.resolve();
  return promise;
}

function emit(child, message) {
  child.respond(message);
}

test("missing policy starts a read-only session with safe defaults", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await createSession(adapter, projects, projects.appA);
  const entry = adapter.sessions.get(session.id);
  assert.equal(entry.writable, false);
  assert.deepEqual(entry.permissionPolicy, safeDefaultPolicy());
  assert.equal(entry.policyRevision, policyRevision(safeDefaultPolicy()));
  const call = bag.calls[0];
  assert.deepEqual(call.args, ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
  assert.ok(!call.args.includes("-e"));
  assert.ok(!call.args.join(" ").includes("bash"));
  await adapter.shutdown({ graceMs: 0 });
});

test("enabled policy loads the trusted extension with edit/write and no bash", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const entry = adapter.sessions.get(session.id);
  assert.equal(entry.writable, true);
  const call = bag.calls[0];
  assert.ok(call.args.includes("read,grep,find,ls,edit,write"));
  assert.ok(call.args.includes("--no-approve") && call.args.includes("--no-extensions"));
  assert.ok(!call.args.join(" ").includes("bash"));
  const extIndex = call.args.indexOf("-e");
  assert.ok(extIndex >= 0);
  const extPath = call.args[extIndex + 1];
  assert.ok(extPath.endsWith("trusted-permission-extension.mjs"));
  // Snapshot survives in the entry and is never the raw caller object.
  assert.equal(entry.permissionPolicy.enabled, true);
  // Policy travels to the child via environment, never in logs.
  assert.ok(call.opts.env.WB_PI_POLICY_JSON.includes('"enabled":true'));
  await adapter.shutdown({ graceMs: 0 });
});

test("supplied policy requires an equal valid revision or creation fails", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const policy = writablePolicy();
  const revision = policyRevision(policy);
  // Match succeeds.
  const session = await createSession(adapter, projects, projects.appA,
    { permission_policy: policy, policy_revision: revision });
  assert.equal(adapter.sessions.get(session.id).writable, true);
  assert.equal(adapter.sessions.get(session.id).policyRevision, revision);
  // Mismatch fails as conflict; nothing is spawned.
  const before = bag.calls.length;
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
      { permission_policy: { version: 1, enabled: true }, policy_revision: "0".repeat(64) }),
    /invalid/i);
  assert.equal(bag.calls.length, before);
  await adapter.shutdown({ graceMs: 0 });
});

test("start -> UI ask -> respond -> end correlation with exact resume", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const child = bag.children[0];
  const entry = adapter.sessions.get(session.id);

  // Preflight: the exact suspended invocation.
  emit(child, { type: "tool_execution_start", toolCallId: "call-1", toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(entry.preflights.has("call-1"));

  // UI ask from the trusted extension with the exact marker/options.
  emit(child, {
    type: "extension_ui_request", id: "ui-1", method: "select",
    title: "WB_PERMISSION_V1:call-1",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(projects.appA, session.id);
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
  assert.ok(!serialized.includes("WB_PI_POLICY_JSON"));

  // Respond once: the exact UI request is answered, pending is removed.
  const result = await adapter.respondPermission(projects.appA, session.id, pending.id, "once");
  assert.deepEqual(result, { ok: true, decision: "once" });
  const written = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.equal(written.length, 1);
  assert.deepEqual(written[0], { type: "extension_ui_response", id: "ui-1", value: OPTION_ONCE });
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  // Duplicate/late response fails closed as not_found.
  await assert.rejects(
    adapter.respondPermission(projects.appA, session.id, pending.id, "once"), /not found/i);

  // Tool end cleans the preflight.
  emit(child, { type: "tool_execution_end", toolCallId: "call-1", toolName: "edit", result: {}, isError: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(!entry.preflights.has("call-1"));
  await adapter.shutdown({ graceMs: 0 });
});

test("malformed, duplicate, and mismatched UI requests fail closed", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const child = bag.children[0];

  // No preflight for this call: cancelled, never allowed.
  emit(child, {
    type: "extension_ui_request", id: "ui-bad", method: "select",
    title: "WB_PERMISSION_V1:call-unknown",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  let uiWrites = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.equal(uiWrites.length, 1);
  assert.deepEqual(uiWrites[0], { type: "extension_ui_response", id: "ui-bad", cancelled: true });

  // Wrong options shape: cancelled, never allowed.
  emit(child, { type: "tool_execution_start", toolCallId: "call-2", toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  emit(child, {
    type: "extension_ui_request", id: "ui-wrong", method: "select",
    title: "WB_PERMISSION_V1:call-2",
    options: ["Yes", "No"],
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  uiWrites = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.ok(uiWrites.some((r) => r.id === "ui-wrong" && r.cancelled === true));

  // Non-marker UI (notify) is ignored without a write.
  const before = child.requests().length;
  emit(child, { type: "extension_ui_request", id: "ui-n", method: "notify", message: "hi" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(child.requests().length, before);

  // Unknown tool preflight still correlates structurally but evaluates deny:
  // the UI ask for a deny effect is cancelled fail-closed.
  emit(child, { type: "tool_execution_start", toolCallId: "call-3", toolName: "bash", args: { command: "ls" } });
  await new Promise((resolve) => setImmediate(resolve));
  emit(child, {
    type: "extension_ui_request", id: "ui-deny", method: "select",
    title: "WB_PERMISSION_V1:call-3",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  await adapter.shutdown({ graceMs: 0 });
});

test("always is rejected when the session policy disables it", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const policy = validatePolicy({
    ...writableOptions().permission_policy, allow_session_always: false,
  });
  const session = await createSession(
    adapter, projects, projects.appA,
    { permission_policy: policy, policy_revision: policyRevision(policy) });
  const child = bag.children[0];
  emit(child, { type: "tool_execution_start", toolCallId: "call-9", toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  // Extension offers once/reject only in this mode.
  emit(child, {
    type: "extension_ui_request", id: "ui-9", method: "select",
    title: "WB_PERMISSION_V1:call-9",
    options: [OPTION_ONCE, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(projects.appA, session.id);
  assert.equal(listed.length, 1);
  await assert.rejects(
    adapter.respondPermission(projects.appA, session.id, listed[0].id, "always"), /always/i);
  // once still works and answers with the once value.
  await adapter.respondPermission(projects.appA, session.id, listed[0].id, "once");
  const written = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.deepEqual(written[0], { type: "extension_ui_response", id: "ui-9", value: OPTION_ONCE });
  await adapter.shutdown({ graceMs: 0 });
});

test("permission state is isolated per session", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const sessionA = await createSession(adapter, projects, projects.appA, writableOptions());
  const sessionB = await createSession(adapter, projects, projects.appB, writableOptions());
  const childA = bag.children[0];
  emit(childA, { type: "tool_execution_start", toolCallId: "call-a", toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  emit(childA, {
    type: "extension_ui_request", id: "ui-a", method: "select",
    title: "WB_PERMISSION_V1:call-a",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await adapter.listPermissions(projects.appA, sessionA.id)).length, 1);
  assert.deepEqual(await adapter.listPermissions(projects.appB, sessionB.id), []);
  // Foreign directory or unknown id fails closed.
  const foreign = (await adapter.listPermissions(projects.appA, sessionA.id))[0];
  await assert.rejects(
    adapter.respondPermission(projects.appB, sessionB.id, foreign.id, "once"), /not found/i);
  await adapter.shutdown({ graceMs: 0 });
});

test("abort resolves owned pending UI first, then aborts Pi", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await createSession(adapter, projects, projects.appA, writableOptions());
  const child = bag.children[0];
  emit(child, { type: "tool_execution_start", toolCallId: "call-z", toolName: "edit", args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  emit(child, {
    type: "extension_ui_request", id: "ui-z", method: "select",
    title: "WB_PERMISSION_V1:call-z",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT],
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await adapter.listPermissions(projects.appA, session.id)).length, 1);
  assert.equal(await adapter.abortSession(projects.appA, session.id), true);
  const writes = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.ok(writes.some((r) => r.id === "ui-z" && r.cancelled === true));
  assert.deepEqual(await adapter.listPermissions(projects.appA, session.id), []);
  await adapter.shutdown({ graceMs: 0 });
});
