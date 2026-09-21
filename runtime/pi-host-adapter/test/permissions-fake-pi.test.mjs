// End-to-end permission correlation over real stdio: the fake Pi child
// simulates start -> UI ask -> response -> end with no provider. The
// adapter correlates the preflight, exposes the pending record, answers it,
// and the fake observes the exact extension_ui_response.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision } from "../policy.mjs";

const FAKE_PI = fileURLToPath(new URL("./fake-pi.mjs", import.meta.url));

function enabledPolicy() {
  return {
    version: 1,
    enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
  };
}

test("fake Pi start -> UI ask -> response -> end without a provider", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-perm-fake-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  fs.writeFileSync(path.join(app, "notes.txt"), "hello\n");
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  const cwd = fs.realpathSync(app);
  const uiRecord = path.join(tmp, "ui-response.json");
  const adapter = new PiAdapter({
    projectsRoot: root,
    piBinary: FAKE_PI,
    agentDir: path.join(tmp, "agent-dir"),
    timeoutMs: 10000,
    piUsable: true,
    piVersion: "fake",
  });
  const previous = { ...process.env };
  process.env.FAKE_PI_UI_ASK = "1";
  process.env.FAKE_PI_UI_RECORD = uiRecord;
  process.env.FAKE_PI_SESSION_ID = "fake-perm-session";
  try {
    const policy = enabledPolicy();
    const session = await adapter.createSession(cwd, "ui-ask", {
      permission_policy: policy,
      policy_revision: policyRevision(policy),
    });
    assert.equal(session.id, "fake-perm-session");
    const entry = adapter.sessions.get(session.id);
    assert.equal(entry.writable, true);

    // The prompt suspends on the UI ask: start it, poll for the pending
    // record, then answer once. The fake only finishes the prompt after
    // our response resumes the exact suspended call.
    const promptPromise = adapter.promptAsync(cwd, session.id, "please edit notes");
    let pending = null;
    for (let i = 0; i < 200 && !pending; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      const listed = await adapter.listPermissions(cwd, session.id);
      if (listed.length) pending = listed[0];
    }
    assert.ok(pending, "expected a pending permission from the fake UI ask");
    assert.equal(pending.tool, "edit");
    assert.equal(pending.resource, "notes.txt");
    assert.equal(pending.tool_call_id, "call-ui-1");

    const answered = await adapter.respondPermission(cwd, session.id, pending.id, "once");
    assert.deepEqual(answered, { ok: true, decision: "once" });
    assert.deepEqual(await promptPromise, { accepted: true });

    // The fake child observed the exact UI response value.
    let observed = null;
    for (let i = 0; i < 100 && !observed; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      try {
        observed = JSON.parse(fs.readFileSync(uiRecord, "utf8"));
      } catch {
        observed = null;
      }
    }
    assert.deepEqual(observed,
      { type: "extension_ui_response", id: "ui-1", value: "Allow once" });
    assert.deepEqual(await adapter.listPermissions(cwd, session.id), []);
    await adapter.shutdown({ graceMs: 2000 });
  } finally {
    process.env.FAKE_PI_UI_ASK = previous.FAKE_PI_UI_ASK;
    if (previous.FAKE_PI_UI_ASK === undefined) delete process.env.FAKE_PI_UI_ASK;
    process.env.FAKE_PI_UI_RECORD = previous.FAKE_PI_UI_RECORD;
    if (previous.FAKE_PI_UI_RECORD === undefined) delete process.env.FAKE_PI_UI_RECORD;
    process.env.FAKE_PI_SESSION_ID = previous.FAKE_PI_SESSION_ID;
    if (previous.FAKE_PI_SESSION_ID === undefined) delete process.env.FAKE_PI_SESSION_ID;
    await adapter.shutdown({ graceMs: 1000 }).catch(() => {});
  }
});
