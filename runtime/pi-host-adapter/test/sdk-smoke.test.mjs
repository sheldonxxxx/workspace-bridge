// Provider-free real AgentSession smoke against the installed Pi SDK.
//
// Runs in the normal suite (no gate, no LLM prompt, no quota, no
// network): creates and disposes a real AgentSession in an isolated
// temporary profile, verifies model discovery, session persistence under
// the isolated agentDir, the SDK version pin, and the suppressed
// auto-discovery boundary. Never sends a prompt; consumes nothing.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { SDK_VERSION, createSdkSession, listSdkModels, sessionDirFor } from "../sdk-transport.mjs";
import { safeDefaultPolicy } from "../policy.mjs";

function makeProfile() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-sdk-smoke-"));
  const cwd = path.join(tmp, "ws");
  fs.mkdirSync(cwd, { recursive: true });
  const agentDir = path.join(tmp, "agent");
  return { tmp, cwd: fs.realpathSync(cwd), agentDir };
}

function noopUi() {
  return {
    select: async () => undefined,
    confirm: async () => false,
    input: async () => undefined,
    notify: () => {},
    onTerminalInput: () => () => {},
    setStatus: () => {},
    setWorkingMessage: () => {},
    setWorkingVisible: () => {},
    setWorkingIndicator: () => {},
    setHiddenThinkingLabel: () => {},
    setWidget: () => {},
    setFooter: () => {},
  };
}

test("SDK dependency pin matches the installed package", async () => {
  const pkg = JSON.parse(fs.readFileSync(
    new URL("../node_modules/@earendil-works/pi-coding-agent/package.json", import.meta.url), "utf8"));
  assert.equal(pkg.version, "0.87.0");
  assert.equal(SDK_VERSION, pkg.version);
  const adapterPkg = JSON.parse(fs.readFileSync(
    new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(adapterPkg.dependencies["@earendil-works/pi-coding-agent"], "0.87.0");
});

test("real AgentSession creates, persists, and disposes in an isolated profile", async () => {
  const { cwd, agentDir } = makeProfile();
  const seen = [];
  const { session, resourceLoader } = await createSdkSession({
    cwd,
    agentDir,
    tools: ["read", "grep", "find", "ls"],
    excludeTools: null,
    extensionPaths: [],
    policy: safeDefaultPolicy(),
    uiContext: noopUi(),
    onEvent: (event) => { seen.push(event && event.type); },
  });
  try {
    // Authoritative persisted identity under the isolated profile.
    assert.ok(session && typeof session.sessionId === "string" && session.sessionId);
    assert.ok(typeof session.sessionFile === "string" && session.sessionFile);
    const realSessionDir = fs.realpathSync(path.dirname(session.sessionFile));
    assert.ok(realSessionDir.startsWith(fs.realpathSync(agentDir) + path.sep),
      `session file escapes the isolated profile: ${session.sessionFile}`);
    // Session files persist lazily on first entry; the assigned path is
    // the durable identity future restart recovery will open.
    assert.equal(session.isStreaming, false);
    assert.deepEqual(session.messages, []);
    // Suppressed auto-discovery: nothing from the project or profile.
    assert.deepEqual(resourceLoader.getSkills().skills, []);
    assert.deepEqual(resourceLoader.getAgentsFiles().agentsFiles, []);
    // Model discovery works provider-free against the isolated profile.
    const models = await listSdkModels({ agentDir });
    assert.ok(Array.isArray(models));
    // Session stays usable: state reads work after creation.
    assert.equal(session.pendingMessageCount, 0);
  } finally {
    session.dispose();
  }
});

test("session directory stays inside the isolated profile", () => {
  const dir = sessionDirFor("/tmp/iso-agent", "/tmp/Projects/app");
  assert.ok(dir.startsWith(`/tmp/iso-agent${path.sep}`));
  assert.ok(!dir.includes(".."));
});
