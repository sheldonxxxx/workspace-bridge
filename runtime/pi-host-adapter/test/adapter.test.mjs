import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter, AdapterError, sanitizeMessages, sanitizeModels, normalizeModelRef } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-adapter-"));
  const appA = path.join(tmp, "Projects", "app-a");
  const appB = path.join(tmp, "Projects", "app-b");
  fs.mkdirSync(appA, { recursive: true });
  fs.mkdirSync(appB, { recursive: true });
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  return { tmp, root, appA: fs.realpathSync(appA), appB: fs.realpathSync(appB) };
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

test("createSession binds the authoritative SDK session id to canonical cwd", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA, "review");
  assert.match(session.id, /^ses-fake-/);
  assert.equal(session.directory, projects.appA);
  assert.equal(session.title, "review");
  const created = transport.lastCreated();
  assert.equal(created.cwd, projects.appA);
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls"]);
  assert.equal(created.excludeTools, null);
  assert.deepEqual(created.extensionPaths, []);
  const sdkSession = transport.lastSession();
  assert.equal(sdkSession.bindMode, "rpc");
  assert.ok(sdkSession.boundUiContext);
  await adapter.shutdown();
});

test("duplicate SDK session ids fail closed without rebinding", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  transport.fixedIds.push("ses-clash", "ses-clash");
  const adapter = makeAdapter(projects, transport);
  await adapter.createSession(projects.appA, "one");
  await assert.rejects(adapter.createSession(projects.appA, "two"), /binding failed/);
  assert.equal(adapter.sessionCount, 1);
  // The clashing replacement was disposed, never adopted.
  assert.equal(transport.sessions[1].disposeCalls, 1);
  await adapter.shutdown();
});

test("createSession fails closed when pi is unusable", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport, { piUsable: false });
  await assert.rejects(adapter.createSession(projects.appA), /unavailable/);
  assert.equal(transport.created.length, 0);
});

test("createSession surfaces transport failures as adapter errors", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({
    projectsRoot: projects.root,
    agentDir: path.join(projects.tmp, "agent-dir"),
    createSessionFn: async () => { throw new Error("sdk boom"); },
    piUsable: true,
    piVersion: "0.87.0",
  });
  await assert.rejects(adapter.createSession(projects.appA), /sdk boom/);
  assert.equal(adapter.sessionCount, 0);
});

test("getSession reports idle/busy from isStreaming with bounded state", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  const sdkSession = transport.lastSession();
  sdkSession.setMessages([{ role: "user", content: "hi", timestamp: 1 }]);
  const idle = await adapter.getSession(projects.appA, session.id);
  assert.equal(idle.status, "idle");
  assert.equal(idle.state.isStreaming, false);
  assert.equal(idle.state.messageCount, 1);
  assert.equal(idle.state.pendingMessageCount, 0);
  assert.equal(idle.session.directory, projects.appA);
  sdkSession.setStreaming(true);
  assert.equal((await adapter.getSession(projects.appA, session.id)).status, "busy");
  assert.equal(await adapter.getSession(projects.appA, "nope"), null);
  await adapter.shutdown();
});

test("promptAsync returns accepted-only and requires text", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  const result = await adapter.promptAsync(projects.appA, session.id, "hello");
  assert.deepEqual(result, { accepted: true });
  const sdkSession = transport.lastSession();
  assert.equal(sdkSession.promptCalls.length, 1);
  assert.equal(sdkSession.promptCalls[0].text, "hello");
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, ""), /text is required/);
  await assert.rejects(adapter.promptAsync(projects.appA, "missing", "hi"), /not found/);
  await adapter.shutdown();
});

test("promptAsync resolves on acceptance without waiting for the run", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  const sdkSession = transport.lastSession();
  let releaseRun = null;
  const runGate = new Promise((resolve) => { releaseRun = resolve; });
  sdkSession.promptImpl = (text, opts) => {
    opts.preflightResult(true);
    return runGate;
  };
  const accepted = await adapter.promptAsync(projects.appA, session.id, "long task");
  assert.deepEqual(accepted, { accepted: true });
  // The run is still in flight: acceptance did not wait for completion.
  assert.equal(sdkSession.promptCalls.length, 1);
  releaseRun();
  await adapter.shutdown();
});

test("promptAsync rejects when preflight refuses before acceptance", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  const sdkSession = transport.lastSession();
  sdkSession.promptImpl = (text, opts) => {
    opts.preflightResult(false);
    return Promise.reject(new Error("no model"));
  };
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, "hi"), /rejected|no model/);
  await adapter.shutdown();
});

test("promptAsync switches model only on unambiguous mapping", async () => {
  const projects = makeProjects();
  const models = [{ provider: "p", id: "m", name: "M" }];
  const transport = createFakeTransport({ models });
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  await adapter.promptAsync(projects.appA, session.id, "hi", "p/m");
  const sdkSession = transport.lastSession();
  assert.deepEqual(sdkSession.setModelCalls, [models[0]]);
  assert.equal(sdkSession.promptCalls.length, 1);
  await adapter.shutdown();
});

test("promptAsync rejects unknown and ambiguous models without prompting", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport({
    models: [{ provider: "p", id: "m" }, { provider: "p", id: "m" }],
  });
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, "hi", "p/m"), /Unsupported model/);
  assert.equal(transport.lastSession().promptCalls.length, 0);
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, "hi", "not-a-model"), /Unsupported model/);
  await adapter.shutdown();
});

test("messages sanitize thinking, bounds, and roles", async () => {
  const raw = [
    { id: "1", role: "user", content: "hello", timestamp: 1 },
    { role: "assistant",
      content: [
        { type: "thinking", thinking: "secret chain of thought" },
        { type: "text", text: "visible" },
        { type: "toolCall", id: "c1", name: "read", arguments: {} },
      ],
      timestamp: 2 },
    { role: "assistant", content: [{ type: "text", text: "x".repeat(9000) }] },
    null,
  ];
  const messages = sanitizeMessages(raw, 10);
  assert.equal(messages.length, 3);
  assert.equal(messages[0].text, "hello");
  assert.equal(messages[1].text, "visible");
  assert.ok(!JSON.stringify(messages).includes("secret chain of thought"));
  assert.deepEqual(messages[1].tools, ["read"]);
  assert.equal(messages[2].text.length, 8000);
  assert.equal(sanitizeMessages(raw, 1).length, 1);
  assert.throws(() => sanitizeMessages("nope"), AdapterError);
});

test("assistant completion derives from terminal stopReason only", () => {
  const assistant = (stopReason, extra = {}) => sanitizeMessages(
    [{ id: "a", role: "assistant", content: [{ type: "text", text: "done" }],
       timestamp: 1758398400001, stopReason, ...extra }], 10)[0];
  // Successful terminal turns carry completion evidence.
  assert.equal(assistant("stop").completed, 1758398400001);
  assert.equal(assistant("length").completed, 1758398400001);
  // Intermediate, pending, failed, aborted, unknown, or missing reasons do not.
  for (const reason of ["toolUse", "pending", "deferred", "error", "aborted", "streaming", undefined, null, 42]) {
    const message = assistant(reason);
    assert.equal(message.completed, null, `stopReason=${String(reason)}`);
    assert.equal(message.created, 1758398400001);
  }
  // Raw stopReason is never exposed; reasoning and tool args stay hidden.
  assert.ok(!("stopReason" in assistant("stop")));
  // A message with error is never final, even with a terminal reason.
  const failed = assistant("stop", { errorMessage: "boom" });
  assert.equal(failed.completed, null);
  assert.equal(failed.error, "boom");
  // Long error text stays bounded.
  assert.equal(assistant("stop", { errorMessage: "x".repeat(900) }).error.length, 300);
  // Non-numeric timestamps never complete, even when terminal.
  assert.equal(assistant("stop", { timestamp: "yesterday" }).completed, null);
  // Non-assistant roles never complete, even with a terminal reason.
  const user = sanitizeMessages(
    [{ id: "u", role: "user", content: "hi", timestamp: 7, stopReason: "stop" }], 10)[0];
  assert.equal(user.completed, null);
  assert.equal(user.created, 7);
});

test("models discovery returns bounded provider/id/name fields", () => {
  const models = sanitizeModels([
    { provider: "p", id: "m", name: "M", api: "x", cost: { input: 1 }, baseUrl: "https://x" },
    { provider: "", id: "m" },
    null,
  ]);
  assert.deepEqual(models, [{ provider: "p", id: "m", name: "M", reasoningOptions: [] }]);
  assert.throws(() => sanitizeModels({}), AdapterError);
});

test("normalizeModelRef accepts string and aliased objects", () => {
  assert.deepEqual(normalizeModelRef("p/m"), { provider: "p", id: "m" });
  assert.deepEqual(normalizeModelRef({ providerID: "p", modelID: "m" }), { provider: "p", id: "m" });
  assert.deepEqual(normalizeModelRef({ provider: "p", model: "m" }), { provider: "p", id: "m" });
  assert.equal(normalizeModelRef(null), null);
  assert.throws(() => normalizeModelRef("nope"), /Unsupported/);
});

test("abort uses session abort without disposing it", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  const sdkSession = transport.lastSession();
  const ok = await adapter.abortSession(projects.appA, session.id);
  assert.equal(ok, true);
  assert.equal(sdkSession.abortCalls, 1);
  assert.equal(sdkSession.disposeCalls, 0);
  // The session stays usable after abort.
  assert.equal((await adapter.getSession(projects.appA, session.id)).status, "idle");
  await adapter.shutdown();
});

test("every operation checks exact cwd binding", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.getSession(projects.appB, session.id), /match/);
  await assert.rejects(adapter.promptAsync(projects.appB, session.id, "hi"), /match/);
  await assert.rejects(adapter.messages(projects.appB, session.id), /match/);
  await assert.rejects(adapter.abortSession(projects.appB, session.id), /match/);
  await assert.rejects(adapter.sessionStatus(projects.appB, session.id), /match/);
  await adapter.shutdown();
});

test("shutdown disposes owned sessions and clears correlation state", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  await adapter.createSession(projects.appA);
  await adapter.createSession(projects.appB);
  await adapter.shutdown();
  assert.equal(adapter.sessionCount, 0);
  for (const sdkSession of transport.sessions) {
    assert.equal(sdkSession.disposeCalls, 1);
  }
  // Late operations fail closed as not_found after shutdown.
  assert.equal(await adapter.getSession(projects.appA, transport.sessions[0].sessionId), null);
});

test("listModels validates the directory and returns the profile inventory", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport({
    models: [{ provider: "p", id: "m", name: "M" }],
  });
  const adapter = makeAdapter(projects, transport);
  await adapter.createSession(projects.appA);
  assert.deepEqual(await adapter.listModels(projects.appA), [{ provider: "p", id: "m", name: "M", reasoningOptions: [] }]);
  assert.deepEqual(await adapter.listModels(projects.appB), [{ provider: "p", id: "m", name: "M", reasoningOptions: [] }]);
  await assert.rejects(adapter.listModels("/no-such-workspace-xyz"), /does not exist|beneath|workspace/i);
  await adapter.shutdown();
});

test("messages read the authoritative in-process transcript", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.appA);
  transport.lastSession().setMessages([
    { role: "user", content: "hi", timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "hello" }],
      timestamp: 2, stopReason: "stop" },
  ]);
  const messages = await adapter.messages(projects.appA, session.id, 40);
  assert.equal(messages.length, 2);
  assert.equal(messages[1].text, "hello");
  assert.equal(messages[1].completed, 2);
  await adapter.shutdown();
});
