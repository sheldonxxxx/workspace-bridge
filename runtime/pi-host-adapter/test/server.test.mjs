import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { createPiAdapterServer } from "../server.mjs";

const TOKEN = "shared-private-token";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-server-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  return { tmp, app: fs.realpathSync(app) };
}

function fakeAdapter(projects) {
  return {
    projectsRoot: projects.tmp,
    sessionCount: 0,
    health: null,
    listModels: async () => [{ provider: "p", model: "m", name: "M" }],
    createSession: async (directory, title) => ({ id: "ses_1", directory, title }),
    getSession: async (directory, id) => ({ session: { id, directory, title: "t" }, status: "idle", state: { isStreaming: false } }),
    sessionStatus: async () => "idle",
    promptAsync: async () => ({ accepted: true }),
    messages: async () => [],
    abortSession: async () => true,
    listPermissions: async () => [],
    respondPermission: async () => ({ ok: true, decision: "once" }),
    listExtensions: async () => ({ packages: [] }),
  };
}

async function withServer({ token = TOKEN, adapter = null, piUsable = true, piVersion = "" } = {}, run) {
  const server = createPiAdapterServer({
    adapter, token, adapterVersion: "0.1.0", instance: "inst-1", piUsable, piVersion,
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    return await run(base);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

function post(base, urlPath, body, headers = {}) {
  return fetch(base + urlPath, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body || {}),
  });
}

const AUTH = { "X-Runtime-Token": TOKEN };

test("health is readable and exposes booleans/version/status only", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects), piVersion: "0.86.1" }, async (base) => {
    const res = await fetch(`${base}/health`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ok, true);
    assert.equal(body.status, "ok");
    assert.equal(body.locked, false);
    assert.equal(body.token_configured, true);
    assert.equal(body.pi_usable, true);
    assert.equal(body.pi_version, "0.86.1");
    assert.equal(body.adapter_version, "0.1.0");
    const serialized = JSON.stringify(body);
    assert.ok(!serialized.includes(projects.tmp));
    assert.ok(!serialized.includes(TOKEN));
  });
});

test("missing token locks every operational endpoint but keeps health", async () => {
  const projects = makeProjects();
  await withServer({ token: "", adapter: fakeAdapter(projects) }, async (base) => {
    const health = await fetch(`${base}/health`);
    assert.equal(health.status, 200);
    const body = await health.json();
    assert.equal(body.locked, true);
    assert.equal(body.status, "locked");
    assert.equal(body.token_configured, false);
    const attempts = [
      fetch(`${base}/models?directory=${encodeURIComponent(projects.app)}`),
      post(base, `/sessions`, { directory: projects.app }),
      fetch(`${base}/sessions/ses_1?directory=${encodeURIComponent(projects.app)}`),
      fetch(`${base}/sessions/ses_1/status?directory=${encodeURIComponent(projects.app)}`),
      post(base, `/sessions/ses_1/prompt-async`, { directory: projects.app, text: "hi" }),
      fetch(`${base}/sessions/ses_1/messages?directory=${encodeURIComponent(projects.app)}`),
      post(base, `/sessions/ses_1/abort`, { directory: projects.app }),
    ];
    for (const response of await Promise.all(attempts)) {
      assert.equal(response.status, 401);
      assert.equal((await response.json()).code, "locked");
    }
  });
});

test("invalid token is rejected as unauthorized", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects) }, async (base) => {
    const res = await post(base, `/sessions`, { directory: projects.app }, { "X-Runtime-Token": "wrong" });
    assert.equal(res.status, 401);
    assert.equal((await res.json()).code, "unauthorized");
  });
});

test("degraded pi surfaces in health without locking", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects), piUsable: false }, async (base) => {
    const body = await (await fetch(`${base}/health`)).json();
    assert.equal(body.ok, false);
    assert.equal(body.status, "degraded");
    assert.equal(body.pi_usable, false);
    const res = await fetch(`${base}/models?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH });
    assert.equal(res.status, 200);
  });
});

test("narrow session routes work end to end", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects) }, async (base) => {
    const created = await post(base, `/sessions`, { directory: projects.app, title: "t" }, AUTH);
    assert.equal(created.status, 200);
    assert.equal((await created.json()).session.id, "ses_1");
    const got = await fetch(`${base}/sessions/ses_1?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH });
    assert.equal(got.status, 200);
    assert.equal((await got.json()).status, "idle");
    const status = await fetch(`${base}/sessions/ses_1/status?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH });
    assert.deepEqual(await status.json(), { status: "idle" });
    const prompted = await post(base, `/sessions/ses_1/prompt-async`, { directory: projects.app, text: "hi" }, AUTH);
    assert.deepEqual(await prompted.json(), { accepted: true });
    const messages = await fetch(`${base}/sessions/ses_1/messages?directory=${encodeURIComponent(projects.app)}&limit=10`, { headers: AUTH });
    assert.deepEqual(await messages.json(), { messages: [] });
    const aborted = await post(base, `/sessions/ses_1/abort`, { directory: projects.app }, AUTH);
    assert.deepEqual(await aborted.json(), { ok: true });
    const models = await fetch(`${base}/models?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH });
    assert.deepEqual(await models.json(), { models: [{ provider: "p", model: "m", name: "M" }], scope: "global" });
  });
});

test("no questions/events or shell endpoints exist", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects) }, async (base) => {
    const unknown = [
      fetch(`${base}/sessions/ses_1/questions?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH }),
      fetch(`${base}/events?cursor=0`, { headers: AUTH }),
      post(base, `/exec`, { command: "ls" }, AUTH),
      post(base, `/spawn`, { binary: "pi" }, AUTH),
      fetch(`${base}/nope`, { headers: AUTH }),
    ];
    for (const response of await Promise.all(unknown)) {
      assert.equal(response.status, 404);
    }
  });
});

test("permission list/respond routes are authenticated and exact-session bound", async () => {
  const projects = makeProjects();
  const seen = [];
  const adapter = fakeAdapter(projects);
  adapter.listPermissions = async (directory, sessionId) => {
    seen.push(["list", directory, sessionId]);
    return [{ session: sessionId, id: "perm_1", tool: "edit", action: "edit",
      resource: "notes.txt", requested: ["notes.txt"], always_pattern: "edit:notes.txt",
      tool_call_id: "call-1", created: "2026-09-21T00:00:00.000Z", metadata: { code: "tool_ask" } }];
  };
  adapter.respondPermission = async (directory, sessionId, permissionId, response) => {
    seen.push(["respond", directory, sessionId, permissionId, response]);
    return { ok: true, decision: response };
  };
  await withServer({ adapter }, async (base) => {
    // Unauthenticated permission access fails closed.
    const anon = await fetch(`${base}/sessions/ses_1/permissions?directory=${encodeURIComponent(projects.app)}`);
    assert.equal(anon.status, 401);
    const listed = await fetch(
      `${base}/sessions/ses_1/permissions?directory=${encodeURIComponent(projects.app)}`, { headers: AUTH });
    assert.equal(listed.status, 200);
    const body = await listed.json();
    assert.equal(body.permissions.length, 1);
    assert.equal(body.permissions[0].resource, "notes.txt");
    const serialized = JSON.stringify(body);
    assert.ok(!serialized.includes(projects.tmp));
    const answered = await post(base, `/sessions/ses_1/permissions/perm_1/respond`,
      { directory: projects.app, response: "once" }, AUTH);
    assert.equal(answered.status, 200);
    assert.deepEqual(await answered.json(), { ok: true, decision: "once" });
    assert.deepEqual(seen, [
      ["list", projects.app, "ses_1"],
      ["respond", projects.app, "ses_1", "perm_1", "once"],
    ]);
    // Locked adapter keeps permission routes locked too.
  });
  await withServer({ token: "", adapter: fakeAdapter(projects) }, async (base) => {
    const res = await fetch(
      `${base}/sessions/ses_1/permissions?directory=${encodeURIComponent(projects.app)}`);
    assert.equal(res.status, 401);
    const denied = await post(base, `/sessions/ses_1/permissions/perm_1/respond`,
      { directory: projects.app, response: "once" });
    assert.equal(denied.status, 401);
  });
});

test("health advertises the permission capability without raw policy", async () => {
  const projects = makeProjects();
  await withServer({ adapter: fakeAdapter(projects), piVersion: "0.86.1" }, async (base) => {
    const body = await (await fetch(`${base}/health`)).json();
    assert.deepEqual(body.capabilities,
      { pending_snapshot: true, permission_response: true, execution_history: true,
        extension_inventory: true });
    assert.ok(!JSON.stringify(body).includes("permission_policy"));
    assert.ok(!JSON.stringify(body).includes("/tmp"));
  });
});
