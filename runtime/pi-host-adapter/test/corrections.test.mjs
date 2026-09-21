// Audit corrections: HTTP policy transport, revision binding, opaque
// marker grammar, missing-preflight approval refusal, minimal preflight
// retention, and confirmed UI writes.
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
import { createPiAdapterServer } from "../server.mjs";
import { createFakeSpawn, respondState } from "./helpers.mjs";

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
    version: 1,
    enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**"],
    protected_template_exceptions: [],
    allow_session_always: true,
  });
}

function autoSpawn() {
  const bag = createFakeSpawn();
  let n = 0;
  function spawnFn(binary, args, opts) {
    const child = bag.spawnFn(binary, args, opts);
    child.autoExitCode = 0;
    n += 1;
    const sid = `ses-corr-${n}`;
    child.on("stdin", (line) => {
      for (const raw of String(line).split("\n").filter(Boolean)) {
        let req;
        try {
          req = JSON.parse(raw);
        } catch {
          continue;
        }
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

function post(base, urlPath, body, headers = {}) {
  return fetch(base + urlPath, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body || {}),
  });
}

// ------------------------------------------------------- 1. HTTP transport
test("server POST /sessions forwards exactly policy + revision", async () => {
  const projects = makeProjects();
  const seen = [];
  const fake = {
    projectsRoot: projects.root,
    sessionCount: 0,
    createSession: async (directory, title, options) => {
      seen.push({ directory, title, options });
      return { id: "ses_1", directory, title };
    },
  };
  const server = createPiAdapterServer({
    adapter: fake, token: TOKEN, adapterVersion: "0.2.0", instance: "inst-1",
    piUsable: true, piVersion: "0.86.1",
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const policy = writablePolicy();
    const revision = policyRevision(policy);
    const res = await post(base, "/sessions", {
      directory: projects.app, title: "t",
      permission_policy: policy, policy_revision: revision,
      injected: "must-not-forward",
    }, AUTH);
    assert.equal(res.status, 200);
    assert.equal(seen.length, 1);
    assert.deepEqual(seen[0].options, { permission_policy: policy, policy_revision: revision });
    // Legacy create without policy passes empty options (read-only default).
    await post(base, "/sessions", { directory: projects.app, title: "t2" }, AUTH);
    assert.deepEqual(seen[1].options, {});
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("enabled policy over HTTP yields a writable child; disabled stays read-only", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root,
    piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"),
    timeoutMs: 2000,
    spawnFn: bag.spawnFn,
    piUsable: true,
    piVersion: "0.86.1",
  });
  const server = createPiAdapterServer({
    adapter, token: TOKEN, adapterVersion: "0.2.0", instance: "inst-1",
    piUsable: true, piVersion: "0.86.1",
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const policy = writablePolicy();
    const created = await post(base, "/sessions", {
      directory: projects.app, title: "w",
      permission_policy: policy, policy_revision: policyRevision(policy),
    }, AUTH);
    assert.equal(created.status, 200);
    const writableArgs = bag.calls[0].args;
    assert.ok(writableArgs.includes("read,grep,find,ls,edit,write"));
    assert.ok(writableArgs.includes("-e"));
    assert.ok(bag.calls[0].opts.env.WB_PI_POLICY_JSON.includes('"enabled":true'));
    const disabled = safeDefaultPolicy();
    const created2 = await post(base, "/sessions", {
      directory: projects.app, title: "r",
      permission_policy: disabled, policy_revision: policyRevision(disabled),
    }, AUTH);
    assert.equal(created2.status, 200);
    assert.deepEqual(bag.calls[1].args,
      ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
    // Mismatched revision fails closed at the HTTP boundary.
    const bad = await post(base, "/sessions", {
      directory: projects.app, title: "x",
      permission_policy: policy, policy_revision: "f".repeat(64),
    }, AUTH);
    assert.equal(bad.status, 409);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await adapter.shutdown({ graceMs: 0 });
  }
});

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
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "m", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  child.respond({ type: "tool_execution_start", toolCallId: "call-1", toolName: "edit",
    args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  // Old suffixed shape is rejected: cancelled, never pending.
  child.respond({ type: "extension_ui_request", id: "ui-old", method: "select",
    title: "WB_PERMISSION_V1:call-1 edit notes.txt",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Empty id is rejected the same way.
  child.respond({ type: "extension_ui_request", id: "ui-empty", method: "select",
    title: "WB_PERMISSION_V1:",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Foreign (non-marker) UI is ignored without any write.
  const writesBefore = child.requests().length;
  child.respond({ type: "extension_ui_request", id: "ui-foreign", method: "select",
    title: "Pick a model:", options: ["a", "b"] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(child.requests().length, writesBefore);
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  const writes = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.ok(writes.some((r) => r.id === "ui-old" && r.cancelled === true));
  assert.ok(writes.some((r) => r.id === "ui-empty" && r.cancelled === true));
  // Preflight metadata (not title text) still identifies the pending call.
  child.respond({ type: "extension_ui_request", id: "ui-good", method: "select",
    title: "WB_PERMISSION_V1:call-1",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(projects.app, session.id);
  assert.equal(listed.length, 1);
  assert.equal(listed[0].resource, "notes.txt");
  await adapter.shutdown({ graceMs: 0 });
});

// --------------------------------------- 3. missing preflight at approval
test("once/always without the exact preflight cannot send an allow response", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "s", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  const entry = adapter.sessions.get(session.id);
  async function askOnce(callId, uiId) {
    child.respond({ type: "tool_execution_start", toolCallId: callId, toolName: "edit",
      args: { path: "notes.txt" } });
    await new Promise((resolve) => setImmediate(resolve));
    child.respond({ type: "extension_ui_request", id: uiId, method: "select",
      title: `WB_PERMISSION_V1:${callId}`,
      options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
    await new Promise((resolve) => setImmediate(resolve));
    const listed = await adapter.listPermissions(projects.app, session.id);
    assert.equal(listed.length, 1);
    return listed[0];
  }
  const pending = await askOnce("call-1", "ui-1");
  // Evict the preflight (tool end raced, map overflow, restart): once and
  // always must conflict and must NOT write an allow value.
  entry.preflights.delete("call-1");
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, pending.id, "once"), /scope changed/i);
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, pending.id, "always"),
    /not found|scope changed/i);
  const writes = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.ok(writes.every((r) => r.cancelled === true));
  assert.ok(!writes.some((r) => r.value === OPTION_ONCE || r.value === OPTION_ALWAYS));
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  // Duplicate/late request stays rejected.
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, pending.id, "once"), /not found/i);
  // Stale reject only cancels: ok:true, never an approval.
  const pending2 = await askOnce("call-2", "ui-2");
  entry.preflights.delete("call-2");
  const rejected = await adapter.respondPermission(projects.app, session.id, pending2.id, "reject");
  assert.deepEqual(rejected, { ok: true, decision: "reject" });
  const writes2 = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.ok(writes2.some((r) => r.id === "ui-2" && r.cancelled === true));
  assert.ok(!writes2.some((r) => r.value === OPTION_ONCE || r.value === OPTION_ALWAYS));
  await adapter.shutdown({ graceMs: 0 });
});

// ------------------------- stale resolution requires confirmed delivery
test("stale waits keep pending on failed cancel and clear on later reject", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "st", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  const entry = adapter.sessions.get(session.id);
  async function askOnce(callId, uiId) {
    child.respond({ type: "tool_execution_start", toolCallId: callId, toolName: "edit",
      args: { path: "notes.txt" } });
    await new Promise((resolve) => setImmediate(resolve));
    child.respond({ type: "extension_ui_request", id: uiId, method: "select",
      title: `WB_PERMISSION_V1:${callId}`,
      options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
    await new Promise((resolve) => setImmediate(resolve));
    const listed = await adapter.listPermissions(projects.app, session.id);
    assert.equal(listed.length, 1);
    return listed[0];
  }
  const pending = await askOnce("call-1", "ui-1");
  entry.preflights.delete("call-1");
  // Break stdin delivery: stale once must not succeed and keeps pending.
  const goodWrite = child.stdin.write;
  child.stdin.write = (line, cb) => {
    if (typeof cb === "function") cb(new Error("EPIPE"));
    return false;
  };
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, pending.id, "once"),
    /unavailable/i);
  // Stale reject with broken delivery also fails: never ok:true.
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, pending.id, "reject"),
    /unavailable/i);
  assert.equal((await adapter.listPermissions(projects.app, session.id)).length, 1);
  assert.ok(!child.requests().some((r) => r.type === "extension_ui_response"
    && (r.value === OPTION_ONCE || r.value === OPTION_ALWAYS)));
  // Delivery restored: a later reject confirms cancellation and clears it.
  child.stdin.write = goodWrite;
  const rejected = await adapter.respondPermission(projects.app, session.id, pending.id, "reject");
  assert.deepEqual(rejected, { ok: true, decision: "reject" });
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  await adapter.shutdown({ graceMs: 0 });
});

test("malformed trusted UI that cannot be cancelled fail-closes the session", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "fc", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  const entry = adapter.sessions.get(session.id);
  child.respond({ type: "tool_execution_start", toolCallId: "call-1", toolName: "edit",
    args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  // Break stdin delivery, then send a malformed trusted-shaped ask: the
  // owned session must fail closed rather than hang untracked.
  child.stdin.write = (line, cb) => {
    if (typeof cb === "function") cb(new Error("EPIPE"));
    return false;
  };
  child.respond({ type: "extension_ui_request", id: "ui-broken", method: "select",
    title: "WB_PERMISSION_V1:call-1 edit notes.txt",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.rpc.dead, true);
  // No permission is exposed after the fail-closed session death.
  await assert.rejects(
    adapter.listPermissions(projects.app, session.id), /not found/i);
  await adapter.shutdown({ graceMs: 0 });
});

// ------------------------------------------------------- 5. minimal data
test("preflight and public state retain only bounded path identity", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "d", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  const entry = adapter.sessions.get(session.id);
  const secret = "sk-live-secret-" + "y".repeat(20000);
  child.respond({ type: "tool_execution_start", toolCallId: "call-w", toolName: "write",
    args: { path: "notes.txt", content: secret } });
  child.respond({ type: "tool_execution_start", toolCallId: "call-e", toolName: "edit",
    args: { path: "notes.txt", edits: [{ oldText: secret, newText: secret }] } });
  child.respond({ type: "tool_execution_start", toolCallId: "call-g", toolName: "grep",
    args: { pattern: secret, path: "notes.txt", unrelated: { nested: secret } } });
  await new Promise((resolve) => setImmediate(resolve));
  const stored = JSON.stringify([...entry.preflights.entries()]);
  assert.ok(!stored.includes("sk-live-secret"));
  assert.ok(stored.length < 2000);
  assert.deepEqual(entry.preflights.get("call-w").input, { path: "notes.txt" });
  assert.deepEqual(entry.preflights.get("call-e").input, { path: "notes.txt" });
  assert.deepEqual(entry.preflights.get("call-g").input, { path: "notes.txt" });
  child.respond({ type: "extension_ui_request", id: "ui-w", method: "select",
    title: "WB_PERMISSION_V1:call-w",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(projects.app, session.id);
  assert.ok(!JSON.stringify(listed).includes("sk-live-secret"));
  await adapter.shutdown({ graceMs: 0 });
});

// ------------------------------------------------------- 6. confirmed write
test("unconfirmed UI write keeps pending and reports unavailable", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi",
    agentDir: path.join(projects.tmp, "agent-dir"), timeoutMs: 2000,
    spawnFn: bag.spawnFn, piUsable: true, piVersion: "0.86.1",
  });
  const policy = writablePolicy();
  const session = await adapter.createSession(projects.app, "u", {
    permission_policy: policy, policy_revision: policyRevision(policy),
  });
  const child = bag.children[0];
  child.respond({ type: "tool_execution_start", toolCallId: "call-1", toolName: "edit",
    args: { path: "notes.txt" } });
  await new Promise((resolve) => setImmediate(resolve));
  child.respond({ type: "extension_ui_request", id: "ui-1", method: "select",
    title: "WB_PERMISSION_V1:call-1",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(projects.app, session.id);
  assert.equal(listed.length, 1);
  // Break stdin delivery: the callback reports failure.
  child.stdin.write = (line, cb) => {
    if (typeof cb === "function") cb(new Error("EPIPE"));
    return false;
  };
  await assert.rejects(
    adapter.respondPermission(projects.app, session.id, listed[0].id, "once"),
    /unavailable/i);
  // Pending stays present: never removed without confirmed delivery.
  assert.equal((await adapter.listPermissions(projects.app, session.id)).length, 1);
  await adapter.shutdown({ graceMs: 0 });
});
