import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  buildClient,
  SdkRuntime,
  EventHub,
  SdkError,
  sanitizeMetadata,
} from "../sdk-runtime.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function ok(data) {
  return { data, error: undefined, response: { status: 200 } };
}

function failure(status, message, name) {
  return { data: undefined, error: { name: name || "Error", message }, response: { status } };
}

function fakeClient(overrides = {}) {
  const calls = [];
  const base = {
    provider: {
      list: async (...args) => {
        calls.push(["provider.list", args]);
        return ok({ all: [{ id: "anthropic" }, { id: "openai" }] });
      },
    },
    config: {
      providers: async (options) => {
        calls.push(["config.providers", options]);
        return ok({
          providers: [
            {
              id: "anthropic",
              name: "Anthropic",
              key: "SUPER_SECRET_KEY",
              models: {
                "claude-sonnet": { id: "claude-sonnet", name: "Claude Sonnet", options: {} },
                "claude-haiku": { id: "claude-haiku", name: "Claude Haiku", options: {} },
              },
            },
          ],
          default: { anthropic: "claude-sonnet" },
        });
      },
    },
    session: {
      status: async (options) => {
        calls.push(["session.status", options]);
        return ok({ ses_abc: { type: "busy" }, ses_idle: { type: "idle" }, ses_retry: { type: "retry", attempt: 1, message: "m", next: 2 } });
      },
      create: async (options) => {
        calls.push(["session.create", options]);
        return ok({ id: "ses_abc", directory: options.query.directory, title: options.body.title, version: "1.18.31" });
      },
      get: async (options) => {
        calls.push(["session.get", options]);
        if (options.path.id === "missing") return failure(404, "not found", "NotFoundError");
        return ok({ id: options.path.id, directory: options.query.directory, title: "t", version: "1.18.31" });
      },
      promptAsync: async (options) => {
        calls.push(["session.promptAsync", options]);
        return ok({});
      },
      messages: async (options) => {
        calls.push(["session.messages", options]);
        return ok([
          { info: { id: "m1", role: "assistant", sessionID: "ses_abc", time: { created: 10, completed: 20 } },
            parts: [{ type: "text", text: "implemented and tests pass" }, { type: "tool", tool: "bash" },
                    { type: "reasoning", text: "hidden" }] },
        ]);
      },
      abort: async (options) => {
        calls.push(["session.abort", options]);
        return ok(true);
      },
    },
    postSessionIdPermissionsPermissionId: async (options) => {
      calls.push(["permission.reply", options]);
      return ok(true);
    },
    event: {
      subscribe: async () => ({ stream: (async function* () {})() }),
    },
  };
  return { client: { ...base, ...overrides }, calls };
}

test("buildClient wires baseUrl and Basic Auth without starting a server", () => {
  let received = null;
  const client = buildClient({
    baseUrl: "http://host.docker.internal:4096",
    username: "opencode",
    password: "secret",
    createClient: (config) => {
      received = config;
      return {};
    },
  });
  assert.deepEqual(client, {});
  assert.equal(received.baseUrl, "http://host.docker.internal:4096");
  const expected = "Basic " + Buffer.from("opencode:secret").toString("base64");
  assert.equal(received.headers.Authorization, expected);
});

test("adapter sources never call createOpencode/createOpencodeServer", () => {
  for (const file of ["adapter.mjs", "sdk-runtime.mjs", "server.mjs"]) {
    const source = readFileSync(path.join(HERE, "..", file), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/[^\n]*/g, "");
    assert.ok(!/createOpencode\s*\(/.test(source), `${file} must not call createOpencode()`);
    assert.ok(!/createOpencodeServer/.test(source), `${file} must not call createOpencodeServer`);
  }
});

test("listModels is global: exact selectors without a workspace directory", async () => {
  const { client, calls } = fakeClient();
  const runtime = new SdkRuntime({ client });
  const models = await runtime.listModels();
  assert.deepEqual(models, [
    { provider: "anthropic", model: "claude-sonnet", selector: "anthropic/claude-sonnet", name: "Claude Sonnet", default: true, variants: [] },
    { provider: "anthropic", model: "claude-haiku", selector: "anthropic/claude-haiku", name: "Claude Haiku", default: false, variants: [] },
  ]);
  assert.ok(!JSON.stringify(models).includes("SUPER_SECRET_KEY"));
  const [name, options] = calls.find(([n]) => n === "config.providers");
  assert.equal(name, "config.providers");
  assert.ok(!options?.query?.directory, "Model discovery must not pass a workspace directory");
});

test("session lifecycle passes the mapped directory and exact model through", async () => {
  const { client, calls } = fakeClient();
  const runtime = new SdkRuntime({ client });
  const session = await runtime.createSession("/projects/alpha", "Handoff title");
  assert.equal(session.id, "ses_abc");
  assert.equal(session.directory, "/projects/alpha");
  await runtime.promptAsync("/projects/alpha", "ses_abc", "do the work",
    { providerID: "anthropic", modelID: "claude-sonnet" });
  const prompt = calls.find(([name]) => name === "session.promptAsync")[1];
  assert.equal(prompt.query.directory, "/projects/alpha");
  assert.deepEqual(prompt.body.model, { providerID: "anthropic", modelID: "claude-sonnet" });
  assert.deepEqual(prompt.body.parts, [{ type: "text", text: "do the work" }]);
});

test("sessionStatus normalizes idle/busy/retry and treats a missing entry as idle", async () => {
  const { client, calls } = fakeClient();
  const runtime = new SdkRuntime({ client });
  assert.equal(await runtime.sessionStatus("/d", "ses_abc"), "busy");
  assert.equal(await runtime.sessionStatus("/d", "ses_idle"), "idle");
  assert.equal(await runtime.sessionStatus("/d", "ses_retry"), "retry");
  assert.equal(await runtime.sessionStatus("/d", "ses_unknown"), "idle");
  const [name, options] = calls.find(([n]) => n === "session.status");
  assert.equal(name, "session.status");
  assert.equal(options.query.directory, "/d");
});

test("sessionStatus rejects malformed or unknown entries", async () => {
  const { client } = fakeClient();
  client.session.status = async () => ok({ ses_abc: { type: "weird" } });
  const runtime = new SdkRuntime({ client });
  await assert.rejects(() => runtime.sessionStatus("/d", "ses_abc"), SdkError);
  client.session.status = async () => ok({ ses_abc: null });
  await assert.rejects(() => runtime.sessionStatus("/d", "ses_abc"), SdkError);
  client.session.status = async () => ok([]);
  await assert.rejects(() => runtime.sessionStatus("/d", "ses_abc"), SdkError);
});

test("respondPermission forwards once/always/reject unchanged", async () => {
  const { client, calls } = fakeClient();
  const runtime = new SdkRuntime({ client });
  for (const decision of ["once", "always", "reject"]) {
    assert.equal(await runtime.respondPermission("/d", "ses_abc", "per_1", decision), true);
  }
  const replies = calls.filter(([name]) => name === "permission.reply").map(([, options]) => options);
  assert.deepEqual(replies.map((r) => r.body.response), ["once", "always", "reject"]);
  assert.deepEqual(replies[1].path, { id: "ses_abc", permissionID: "per_1" });
  await assert.rejects(() => runtime.respondPermission("/d", "ses_abc", "per_1", "maybe"), SdkError);
});

test("getSession maps not-found to null and other errors to SdkError", async () => {
  const { client } = fakeClient({
    session: {
      ...fakeClient().client.session,
      get: async (options) => options.path.id === "boom" ? failure(500, "server down") : failure(404, "no", "NotFoundError"),
    },
  });
  const runtime = new SdkRuntime({ client });
  assert.equal(await runtime.getSession("/d", "missing"), null);
  await assert.rejects(() => runtime.getSession("/d", "boom"), SdkError);
});

test("messages normalize roles, text, tools and errors", async () => {
  const { client } = fakeClient();
  const runtime = new SdkRuntime({ client });
  const messages = await runtime.messages("/d", "ses_abc", 10);
  assert.deepEqual(messages, [{
    id: "m1", role: "assistant", created: 10, completed: 20,
    text: "implemented and tests pass", tools: ["bash"], error: null,
  }]);
});

test("permission.asked maps real V1 fields and keeps requested vs always distinct", () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({
    type: "permission.asked",
    properties: {
      id: "per_1", sessionID: "ses_abc", permission: "edit",
      patterns: ["/data/requested/**"], always: ["/data/always/**"],
      tool: { name: "edit" },
      time: { created: 1700000000000 },
      metadata: { note: "api_key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", path: "/tmp" },
    },
  });
  assert.equal(hub.cursor, 1);
  const [event] = hub.events;
  assert.equal(event.type, "permission.asked");
  assert.equal(event.session_id, "ses_abc");
  assert.equal(event.data.id, "per_1");
  assert.equal(event.data.action, "edit");
  assert.deepEqual(event.data.requested_patterns, ["/data/requested/**"]);
  assert.deepEqual(event.data.pattern, ["/data/always/**"]);
  assert.deepEqual(event.data.tool, { name: "edit" });
  assert.equal(event.data.call_id, null);
  assert.equal(event.data.redacted, true);
  assert.ok(!JSON.stringify(event.data.metadata).includes("sk-ABCDEF"));
});

test("permission.replied maps real V1 requestID and reply", () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({
    type: "permission.replied",
    properties: { sessionID: "ses_abc", requestID: "per_1", reply: "always" },
  });
  assert.equal(hub.cursor, 1);
  const [event] = hub.events;
  assert.equal(event.type, "permission.replied");
  assert.equal(event.session_id, "ses_abc");
  assert.deepEqual(event.data, { permission_id: "per_1", response: "always" });
});

test("permission.replied keeps permissionID/response as compatibility fallback", () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({
    type: "permission.replied",
    properties: { sessionID: "ses_abc", permissionID: "per_old", response: "once" },
  });
  const [event] = hub.events;
  assert.deepEqual(event.data, { permission_id: "per_old", response: "once" });
});

test("permission.updated is a compatibility alias for permission.asked", () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({
    type: "permission.updated",
    properties: {
      id: "per_9", type: "edit", title: "t",
      pattern: ["/legacy/**"], sessionID: "ses_abc",
      callID: "call_9", time: { created: 1700000000000 },
      metadata: {},
    },
  });
  assert.equal(hub.cursor, 1);
  const [event] = hub.events;
  assert.equal(event.type, "permission.asked");
  assert.equal(event.data.id, "per_9");
  assert.equal(event.session_id, "ses_abc");
  assert.equal(event.data.action, "edit");
  assert.deepEqual(event.data.pattern, ["/legacy/**"]);
  assert.deepEqual(event.data.requested_patterns, ["/legacy/**"]);
  assert.equal(event.data.call_id, "call_9");
});

test("poll returns only events after the given cursor", async () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({ type: "session.idle", properties: { sessionID: "ses_abc" } });
  hub.push({ type: "session.error", properties: { sessionID: "ses_abc", error: { name: "ApiError", message: "boom" } } });
  assert.equal(hub.cursor, 2);
  const first = await hub.poll(0, 0);
  assert.equal(first.events.length, 2);
  const second = await hub.poll(1, 0);
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].type, "session.error");
  assert.deepEqual(second.events[0].data, { name: "ApiError", message: "boom" });
  const none = await hub.poll(2, 0);
  assert.equal(none.events.length, 0);
});

test("poll waits and wakes on a new event", async () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  const pending = hub.poll(0, 1000);
  hub.push({ type: "session.idle", properties: { sessionID: "ses_abc" } });
  const result = await pending;
  assert.equal(result.events.length, 1);
});

test("irrelevant events are not forwarded", () => {
  const { client } = fakeClient();
  const hub = new EventHub({ client });
  hub.push({ type: "message.updated", properties: { info: { id: "m" } } });
  assert.equal(hub.events.length, 0);
  assert.equal(hub.cursor, 0);
});

test("sanitizeMetadata bounds depth and flags redaction", () => {
  const { value, redacted } = sanitizeMetadata({ a: { b: { c: { d: "too deep" } } }, token: "ghp_" + "A".repeat(30) });
  assert.equal(redacted, true);
  assert.ok(!JSON.stringify(value).includes("ghp_"));
});

test("createSession preserves a missing upstream directory instead of the query directory", async () => {
  const { client } = fakeClient();
  client.session.create = async () => ok({ id: "ses_nodir", title: "t" });
  const runtime = new SdkRuntime({ client });
  const session = await runtime.createSession("/projects/alpha", "Handoff");
  assert.equal(session.id, "ses_nodir");
  assert.equal(session.directory, "");
  assert.notEqual(session.directory, "/projects/alpha");
  assert.equal(JSON.parse(JSON.stringify(session)).directory, "");
});

test("getSession preserves a missing upstream directory instead of the query directory", async () => {
  const { client } = fakeClient();
  client.session.get = async () => ok({ id: "ses_nodir", title: "t" });
  const runtime = new SdkRuntime({ client });
  const session = await runtime.getSession("/projects/alpha", "ses_nodir");
  assert.equal(session.id, "ses_nodir");
  assert.equal(session.directory, "");
  assert.notEqual(session.directory, "/projects/alpha");
  assert.equal(JSON.parse(JSON.stringify(session)).directory, "");
});

test("an observed non-empty directory is still preserved verbatim", async () => {
  const { client } = fakeClient();
  client.session.create = async () => ok({ id: "ses_ok", directory: "/observed/root", title: "t" });
  const runtime = new SdkRuntime({ client });
  const session = await runtime.createSession("/projects/alpha", "Handoff");
  assert.equal(session.directory, "/observed/root");
});
