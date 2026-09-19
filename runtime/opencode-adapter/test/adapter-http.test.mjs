import assert from "node:assert/strict";
import { test } from "node:test";

import { createAdapterServer } from "../server.mjs";

const TOKEN = "shared-private-token";

function fakeRuntime() {
  return {
    health: async () => ({ ok: true, version: "1.18.31" }),
    listModels: async () => ([{ provider: "p", model: "m", selector: "p/m", name: "M", default: true, variants: [] }]),
    createSession: async (directory, title) => ({ id: "ses_1", directory, title }),
    getSession: async (directory, id) => ({ id, directory, title: "t" }),
    promptAsync: async () => true,
    messages: async () => [],
    respondPermission: async () => true,
    abortSession: async () => true,
    sessionStatus: async (directory, id) => ({ ses_1: "busy" }[id] || "idle"),
  };
}

const hub = { cursor: 0, poll: async () => ({ events: [], cursor: 0 }) };

async function withServer(token, run) {
  const server = createAdapterServer({
    runtime: fakeRuntime(), hub, token, serverConfigured: true,
    instance: "inst-1", adapterVersion: "0.1.2",
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    return await run(base);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

function post(base, path, body, headers = {}) {
  return fetch(base + path, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body || {}),
  });
}

test("missing WB_RUNTIME_TOKEN locks every operational endpoint", async () => {
  await withServer("", async (base) => {
    const health = await fetch(base + "/health");
    assert.equal(health.status, 200);
    const body = await health.json();
    assert.equal(body.locked, true);
    assert.equal(body.token_configured, false);
    assert.ok(!JSON.stringify(body).includes("undefined"));
    const attempts = [
      fetch(base + "/models"),
      post(base, "/sessions", { directory: "/tmp" }),
      post(base, "/sessions/ses_1/prompt-async", { directory: "/tmp", text: "hi" }),
      post(base, "/sessions/ses_1/permissions/per_1", { directory: "/tmp", response: "always" }),
      post(base, "/sessions/ses_1/abort", { directory: "/tmp" }),
      fetch(base + "/events?cursor=0&timeout=1"),
    ];
    for (const response of await Promise.all(attempts)) {
      assert.equal(response.status, 401);
      const payload = await response.json();
      assert.equal(payload.code, "locked");
    }
  });
});

test("a configured token is required and grants only the narrow protocol", async () => {
  await withServer(TOKEN, async (base) => {
    assert.equal((await fetch(base + "/models")).status, 401);
    assert.equal((await post(base, "/sessions", { directory: "/tmp" }, { "X-Runtime-Token": "wrong" })).status, 401);
    const auth = { "X-Runtime-Token": TOKEN };
    const models = await (await fetch(base + "/models", { headers: auth })).json();
    assert.equal(models.scope, "global");
    assert.deepEqual(models.models.map((m) => m.selector), ["p/m"]);
    assert.equal((await post(base, "/sessions", { directory: "/tmp" }, auth)).status, 200);
    assert.equal((await post(base, "/sessions/ses_1/prompt-async", { directory: "/tmp", text: "hi" }, auth)).status, 200);
    assert.equal((await post(base, "/sessions/ses_1/permissions/per_1", { directory: "/tmp", response: "always" }, auth)).status, 200);
    assert.equal((await post(base, "/sessions/ses_1/abort", { directory: "/tmp" }, auth)).status, 200);
    const health = await (await fetch(base + "/health", { headers: auth })).json();
    assert.equal(health.locked, false);
    assert.equal(health.token_configured, true);
  });
});

test("session status is exposed narrowly per session", async () => {
  await withServer(TOKEN, async (base) => {
    const auth = { "X-Runtime-Token": TOKEN };
    const busy = await (await fetch(base + "/sessions/ses_1/status?directory=/tmp", { headers: auth })).json();
    assert.equal(busy.status, "busy");
    const idle = await (await fetch(base + "/sessions/ses_9/status?directory=/tmp", { headers: auth })).json();
    assert.equal(idle.status, "idle");
    assert.equal((await fetch(base + "/sessions/ses_1/status")).status, 401);
  });
});

test("no arbitrary command route and no token leak", async () => {
  await withServer(TOKEN, async (base) => {
    const auth = { "X-Runtime-Token": TOKEN };
    assert.equal((await post(base, "/commands", { command: "id" }, auth)).status, 404);
    assert.equal((await post(base, "/sessions/ses_1/shell", { command: "id" }, auth)).status, 404);
    const text = JSON.stringify(await (await fetch(base + "/health", { headers: auth })).json());
    assert.ok(!text.includes(TOKEN));
  });
});
