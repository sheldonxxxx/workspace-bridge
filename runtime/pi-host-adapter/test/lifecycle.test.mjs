// Fake-child integration lifecycle: create -> get_state -> prompt accepted
// -> messages -> abort, against the real spawn path (node + fake-pi.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";

const FAKE_PI = fileURLToPath(new URL("./fake-pi.mjs", import.meta.url));

test("fake-child lifecycle over real stdio", async () => {
  try {
    fs.chmodSync(FAKE_PI, 0o755);
  } catch { /* best effort; shebang already present */ }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-lifecycle-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  const agentDir = path.join(tmp, "agent-dir");
  const recordPath = path.join(tmp, "spawn-record.json");
  const adapter = new PiAdapter({
    projectsRoot: root,
    piBinary: FAKE_PI,
    agentDir,
    timeoutMs: 10000,
    piUsable: true,
    piVersion: "fake",
  });
  // Direct the fake's record output at our file.
  const previous = process.env.FAKE_PI_RECORD;
  process.env.FAKE_PI_RECORD = recordPath;
  try {
    const session = await adapter.createSession(fs.realpathSync(app), "lifecycle");
    assert.equal(session.id, "fake-session-1");
    assert.equal(session.directory, fs.realpathSync(app));

    const got = await adapter.getSession(fs.realpathSync(app), session.id);
    assert.equal(got.status, "idle");

    const accepted = await adapter.promptAsync(fs.realpathSync(app), session.id, "read package.json");
    assert.deepEqual(accepted, { accepted: true });

    const messages = await adapter.messages(fs.realpathSync(app), session.id, 40);
    assert.ok(messages.length >= 2);
    assert.ok(!JSON.stringify(messages).includes("hidden reasoning"));
    const assistant = messages.find((m) => m.role === "assistant");
    assert.ok(assistant.text.includes("ack:"));
    assert.deepEqual(assistant.tools, ["read"]);
    // Real completion evidence: the terminal assistant turn carries a
    // non-null completed timestamp derived from stopReason:"stop".
    assert.equal(typeof assistant.created, "number");
    assert.equal(assistant.completed, 1758398400001);

    const models = await adapter.listModels(fs.realpathSync(app));
    assert.deepEqual(models, [{ provider: "fake-provider", id: "fake-model", name: "Fake Model" }]);

    assert.equal(await adapter.abortSession(fs.realpathSync(app), session.id), true);
    await adapter.shutdown({ graceMs: 2000 });

    // Spawn contract: direct argv with read-only tools, validated cwd, and
    // the resolved absolute agent dir in the child environment.
    const record = JSON.parse(fs.readFileSync(recordPath, "utf8"));
    assert.deepEqual(record.argv, ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
    assert.equal(record.cwd, fs.realpathSync(app));
    assert.equal(record.PI_CODING_AGENT_DIR, agentDir);
    assert.equal(record.pathPresent, true);
  } finally {
    if (previous === undefined) delete process.env.FAKE_PI_RECORD;
    else process.env.FAKE_PI_RECORD = previous;
    await adapter.shutdown({ graceMs: 1000 }).catch(() => {});
  }
});
