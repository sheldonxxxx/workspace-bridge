import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter, AdapterError, sanitizeMessages, sanitizeModels, normalizeModelRef } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { createFakeSpawn, respondState } from "./helpers.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-adapter-"));
  const appA = path.join(tmp, "Projects", "app-a");
  const appB = path.join(tmp, "Projects", "app-b");
  fs.mkdirSync(appA, { recursive: true });
  fs.mkdirSync(appB, { recursive: true });
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  return { tmp, root, appA: fs.realpathSync(appA), appB: fs.realpathSync(appB) };
}

function defaultRespond(child, req, sid) {
  switch (req.type) {
    case "get_state":
      respondState(child, req.id, { sessionId: sid });
      break;
    case "prompt":
      child.respond({ id: req.id, type: "response", command: "prompt", success: true });
      break;
    case "get_messages":
      child.respond({ id: req.id, type: "response", command: "get_messages", success: true, data: { messages: [] } });
      break;
    case "get_available_models":
      child.respond({ id: req.id, type: "response", command: "get_available_models", success: true, data: { models: [] } });
      break;
    case "abort":
      child.respond({ id: req.id, type: "response", command: "abort", success: true });
      break;
    case "set_model":
      child.respond({ id: req.id, type: "response", command: "set_model", success: true, data: {} });
      break;
    default:
      break;
  }
}

function autoSpawn({ onRequest } = {}) {
  const bag = createFakeSpawn();
  let n = 0;
  function spawnFn(binary, args, opts) {
    const child = bag.spawnFn(binary, args, opts);
    child.autoExitCode = 0;
    n += 1;
    const sid = `ses-auto-${n}`;
    child.on("stdin", (line) => {
      for (const raw of String(line).split("\n").filter(Boolean)) {
        const req = JSON.parse(raw);
        setImmediate(() => {
          if (onRequest && onRequest(child, req, sid) === true) return;
          defaultRespond(child, req, sid);
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
    timeoutMs: 1000,
    spawnFn: spawnBag.spawnFn,
    piUsable: true,
    piVersion: "0.86.1",
    ...opts,
  });
}

test("createSession binds pi sessionId to canonical cwd", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA, "review");
  assert.match(session.id, /^ses-auto-/);
  assert.equal(session.directory, projects.appA);
  assert.equal(session.title, "review");
  const child = bag.children[0];
  assert.equal(child.opts.cwd, projects.appA);
  assert.equal(child.opts.env.PI_CODING_AGENT_DIR, path.join(projects.tmp, "agent-dir"));
  await adapter.shutdown({ graceMs: 10 });
});

test("createSession fails closed when pi is unusable", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag, { piUsable: false });
  await assert.rejects(adapter.createSession(projects.appA), /unavailable/);
  assert.equal(bag.children.length, 0);
});

test("getSession reports idle/busy from isStreaming with bounded state", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  const idle = await adapter.getSession(projects.appA, session.id);
  assert.equal(idle.status, "idle");
  assert.equal(idle.state.isStreaming, false);
  assert.equal(idle.session.directory, projects.appA);
  assert.equal(await adapter.getSession(projects.appA, "nope"), null);
  await adapter.shutdown({ graceMs: 10 });
});

test("promptAsync returns accepted-only and requires text", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  const result = await adapter.promptAsync(projects.appA, session.id, "hello");
  assert.deepEqual(result, { accepted: true });
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, ""), /text is required/);
  await assert.rejects(adapter.promptAsync(projects.appA, "missing", "hi"), /not found/);
  await adapter.shutdown({ graceMs: 10 });
});

test("promptAsync switches model only on unambiguous mapping", async () => {
  const projects = makeProjects();
  const models = [{ provider: "p", id: "m", name: "M" }];
  const bag = autoSpawn({
    onRequest: (child, req) => {
      if (req.type === "get_available_models") {
        child.respond({ id: req.id, type: "response", command: "get_available_models", success: true, data: { models } });
        return true;
      }
      return false;
    },
  });
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  await adapter.promptAsync(projects.appA, session.id, "hi", "p/m");
  const types = bag.children[0].requests().map((r) => r.type);
  assert.ok(types.includes("set_model"));
  assert.ok(types.includes("prompt"));
  const setReq = bag.children[0].requests().find((r) => r.type === "set_model");
  assert.equal(setReq.provider, "p");
  assert.equal(setReq.modelId, "m");
  await adapter.shutdown({ graceMs: 10 });
});

test("promptAsync rejects unknown and ambiguous models without prompting", async () => {
  const projects = makeProjects();
  const bag = autoSpawn({
    onRequest: (child, req) => {
      if (req.type === "get_available_models") {
        // Duplicate provider/id entries: no unambiguous mapping exists.
        const models = [{ provider: "p", id: "m" }, { provider: "p", id: "m" }];
        child.respond({ id: req.id, type: "response", command: "get_available_models", success: true, data: { models } });
        return true;
      }
      return false;
    },
  });
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, "hi", "p/m"), /Unsupported model/);
  const types = bag.children[0].requests().map((r) => r.type);
  assert.ok(!types.includes("prompt"));
  await assert.rejects(adapter.promptAsync(projects.appA, session.id, "hi", "not-a-model"), /Unsupported model/);
  await adapter.shutdown({ graceMs: 10 });
});

test("messages sanitize thinking, bounds, and roles", async () => {  const raw = [
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
  assert.deepEqual(models, [{ provider: "p", id: "m", name: "M" }]);
  assert.throws(() => sanitizeModels({}), AdapterError);
});

test("normalizeModelRef accepts string and aliased objects", () => {
  assert.deepEqual(normalizeModelRef("p/m"), { provider: "p", id: "m" });
  assert.deepEqual(normalizeModelRef({ providerID: "p", modelID: "m" }), { provider: "p", id: "m" });
  assert.deepEqual(normalizeModelRef({ provider: "p", model: "m" }), { provider: "p", id: "m" });
  assert.equal(normalizeModelRef(null), null);
  assert.throws(() => normalizeModelRef("nope"), /Unsupported/);
});

test("abort uses RPC abort and never kills the child", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  const ok = await adapter.abortSession(projects.appA, session.id);
  assert.equal(ok, true);
  assert.deepEqual(bag.children[0].killedSignals, []);
  assert.ok(bag.children[0].requests().some((r) => r.type === "abort"));
  await adapter.shutdown({ graceMs: 10 });
});

test("child death fails closed without respawn", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  bag.children[0].die(1);
  await assert.rejects(adapter.getSession(projects.appA, session.id), /unavailable/);
  assert.equal(bag.children.length, 1);
  await adapter.shutdown({ graceMs: 10 });
});

test("every operation checks exact cwd binding", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.getSession(projects.appB, session.id), /match/);
  await assert.rejects(adapter.promptAsync(projects.appB, session.id, "hi"), /match/);
  await assert.rejects(adapter.messages(projects.appB, session.id), /match/);
  await assert.rejects(adapter.abortSession(projects.appB, session.id), /match/);
  await assert.rejects(adapter.sessionStatus(projects.appB, session.id), /match/);
  await adapter.shutdown({ graceMs: 10 });
});

test("shutdown gives bounded grace to owned children only", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  await adapter.createSession(projects.appA);
  await adapter.createSession(projects.appB);
  await adapter.shutdown({ graceMs: 50 });
  assert.equal(adapter.sessionCount, 0);
  for (const child of bag.children) {
    assert.ok(child.killedSignals.includes("SIGTERM"));
  }
});

test("listModels reuses only the exact-workspace child", async () => {
  const projects = makeProjects();
  const bag = autoSpawn();
  const adapter = makeAdapter(projects, bag);
  await adapter.createSession(projects.appA);
  const spawned = bag.children.length;
  // Workspace B must not borrow A's child: a short-lived child serves B in
  // B's own cwd and is closed before returning.
  const modelsB = await adapter.listModels(projects.appB);
  assert.deepEqual(modelsB, []);
  assert.ok(!bag.children[0].requests().some((r) => r.type === "get_available_models"));
  assert.equal(bag.children.length, spawned + 1);
  assert.equal(bag.children[spawned].opts.cwd, projects.appB);
  assert.ok(bag.children[spawned].killedSignals.length > 0);
  // The exact-workspace request reuses the live child without spawning.
  await adapter.listModels(projects.appA);
  assert.equal(bag.children.length, spawned + 1);
  assert.ok(bag.children[0].requests().some((r) => r.type === "get_available_models"));
  await adapter.shutdown({ graceMs: 10 });
});

function driftingSpawn() {
  const bag = createFakeSpawn();
  let n = 0;
  const stateCounts = new Map();
  function spawnFn(binary, args, opts) {
    const child = bag.spawnFn(binary, args, opts);
    child.autoExitCode = 0;
    n += 1;
    const sid = `ses-drift-${n}`;
    child.on("stdin", (line) => {
      for (const raw of String(line).split("\n").filter(Boolean)) {
        const req = JSON.parse(raw);
        setImmediate(() => {
          if (req.type === "get_state") {
            const count = (stateCounts.get(child) || 0) + 1;
            stateCounts.set(child, count);
            if (count === 1) respondState(child, req.id, { sessionId: sid });
            else respondState(child, req.id, { sessionId: "ses-intruder" });
            return;
          }
          defaultRespond(child, req, sid);
        });
      }
    });
    return child;
  }
  return { ...bag, spawnFn };
}

test("post-start get_state drift fails closed without rebinding", async () => {
  const projects = makeProjects();
  const bag = driftingSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.getSession(projects.appA, session.id), /unavailable/);
  // No silent rebind: the original id still owns the entry, the intruder id
  // owns nothing, and no replacement child was spawned.
  assert.ok(adapter.sessions.has(session.id));
  assert.ok(!adapter.sessions.has("ses-intruder"));
  assert.equal(bag.children.length, 1);
  assert.equal(adapter.sessions.get(session.id).rpc.dead, true);
  await assert.rejects(adapter.sessionStatus(projects.appA, session.id), /unavailable/);
  await adapter.shutdown({ graceMs: 10 });
});

test("post-start get_state drift fails closed on sessionStatus", async () => {
  const projects = makeProjects();
  const bag = driftingSpawn();
  const adapter = makeAdapter(projects, bag);
  const session = await adapter.createSession(projects.appA);
  await assert.rejects(adapter.sessionStatus(projects.appA, session.id), /unavailable/);
  assert.ok(adapter.sessions.has(session.id));
  assert.ok(!adapter.sessions.has("ses-intruder"));
  assert.equal(bag.children.length, 1);
  await adapter.shutdown({ graceMs: 10 });
});
