// In-process session lifecycle: create -> status -> prompt accepted ->
// messages -> abort -> dispose, against the fake SDK transport (no real
// spawn, no provider, no model calls).
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";

test("in-process lifecycle over the fake SDK transport", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-lifecycle-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  const cwd = fs.realpathSync(app);
  const agentDir = path.join(tmp, "agent-dir");
  const transport = createFakeTransport({
    models: [{ provider: "fake-provider", id: "fake-model", name: "Fake Model" }],
  });
  const adapter = new PiAdapter({
    projectsRoot: root,
    agentDir,
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true,
    piVersion: "fake",
  });
  const session = await adapter.createSession(cwd, "lifecycle");
  assert.match(session.id, /^ses-fake-/);
  assert.equal(session.directory, cwd);

  const sdkSession = transport.lastSession();
  sdkSession.setMessages([
    { role: "user", content: "read package.json", timestamp: 1758398400000 },
    {
      role: "assistant",
      content: [
        { type: "thinking", thinking: "hidden reasoning must not surface" },
        { type: "text", text: "ack: read package.json" },
        { type: "toolCall", id: "call-1", name: "read", arguments: {} },
      ],
      timestamp: 1758398400001,
      stopReason: "stop",
    },
  ]);

  const got = await adapter.getSession(cwd, session.id);
  assert.equal(got.status, "idle");

  const accepted = await adapter.promptAsync(cwd, session.id, "read package.json");
  assert.deepEqual(accepted, { accepted: true });

  const messages = await adapter.messages(cwd, session.id, 40);
  assert.ok(messages.length >= 2);
  assert.ok(!JSON.stringify(messages).includes("hidden reasoning"));
  const assistant = messages.find((m) => m.role === "assistant");
  assert.ok(assistant.text.includes("ack:"));
  assert.deepEqual(assistant.tools, ["read"]);
  // Real completion evidence: the terminal assistant turn carries a
  // non-null completed timestamp derived from stopReason:"stop".
  assert.equal(typeof assistant.created, "number");
  assert.equal(assistant.completed, 1758398400001);

  const models = await adapter.listModels(cwd);
  assert.deepEqual(models, [{ provider: "fake-provider", id: "fake-model", name: "Fake Model", reasoningOptions: [] }]);

  assert.equal(await adapter.abortSession(cwd, session.id), true);
  assert.equal(sdkSession.abortCalls, 1);

  // Session creation contract: canonical cwd, read-only default loadout,
  // and the isolated agent dir recorded for the transport.
  const created = transport.lastCreated();
  assert.equal(created.cwd, cwd);
  assert.deepEqual(created.tools, ["read", "grep", "find", "ls"]);
  assert.deepEqual(created.extensionPaths, []);

  await adapter.shutdown();
  assert.equal(sdkSession.disposeCalls, 1);
  assert.equal(adapter.sessionCount, 0);
});
