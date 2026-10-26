import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";

import { AdapterError } from "../adapter.mjs";
import { HANDLERS, serveStdio } from "../stdio.mjs";

function fakeAdapter(root) {
  const agentDir = path.join(root, "agent");
  const projectsRoot = path.join(root, "projects");
  fs.mkdirSync(agentDir, { recursive: true });
  fs.mkdirSync(projectsRoot, { recursive: true });
  return {
    agentDir, projectsRoot, piVersion: "test", sessions: new Map(),
    listModelsFn: async () => [{ provider: "provider", id: "model", name: "Model" }],
    shutdownCalls: 0,
    async shutdown() { this.shutdownCalls += 1; },
  };
}

function harness(buildAdapter) {
  const input = new PassThrough();
  const lines = [];
  const waiters = [];
  let exited = null;
  serveStdio({
    input,
    write: (line) => {
      lines.push(JSON.parse(line));
      for (const waiter of waiters.splice(0)) waiter();
    },
    buildAdapter,
    exit: (code) => { exited = code; },
  });
  const request = async (id, method, params = {}) => {
    input.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    for (;;) {
      const found = lines.find((line) => line.id === id);
      if (found) return found;
      await new Promise((resolve) => waiters.push(resolve));
    }
  };
  return { input, request, exited: () => exited };
}

test("stdio serves the Pi protocol after initialize and keeps v1 error meaning", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-stdio-"));
  try {
    const adapter = fakeAdapter(root);
    const { input, request, exited } = harness(() => adapter);
    const early = await request(1, "descriptor");
    assert.equal(early.error.data.code, "invalid_arguments");
    const wrong = await request(2, "initialize", { protocol: 1 });
    assert.equal(wrong.error.data.code, "protocol_mismatch");
    const ready = (await request(3, "initialize", { protocol: 2, projectsRoot: adapter.projectsRoot })).result;
    assert.equal(ready.protocol, 2);
    assert.match(ready.instanceId, /^pi_instance_/);
    const descriptor = (await request(4, "descriptor")).result;
    assert.equal(descriptor.runtime.id, "pi");
    assert.equal((await request(5, "models")).result.models[0].selector, "provider/model");
    const missing = await request(6, "runs.get", { runId: "nope" });
    assert.equal(missing.error.data.status, 404);
    const unknown = await request(7, "shell.exec", {});
    assert.equal(unknown.error.data.code, "not_found");
    input.end();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(adapter.shutdownCalls, 1);
    assert.equal(exited(), 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a failed initialize reports the adapter error without starting", async () => {
  const { request } = harness(() => { throw new AdapterError("Pi is missing", 503, "pi_unavailable"); });
  const response = await request(1, "initialize", { protocol: 2 });
  assert.equal(response.error.data.code, "pi_unavailable");
  assert.equal(response.error.message, "Pi is missing");
});

test("stdio exposes exactly the v2 resource methods", () => {
  assert.deepEqual(Object.keys(HANDLERS).sort(), [
    "activities.get", "conversations.create", "conversations.get", "conversations.rebind",
    "descriptor", "interactions.resolve", "models", "profiles.delete", "profiles.list",
    "profiles.save", "runs.activities", "runs.cancel", "runs.find", "runs.get",
    "runs.interactions", "runs.queue", "runs.start", "runs.steer",
  ]);
});
