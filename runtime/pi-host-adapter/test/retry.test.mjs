// Native retry lifecycle and managed backoff policy (Pi 0.87.0).
//
// - Logical busy is isStreaming || isRetrying; isStreaming=false +
//   isRetrying=true must read busy, never idle.
// - Runtime Protocol polling must not terminal-fail a run while native Pi
//   is in retry backoff, even when the last assistant message is a
//   transient 429 error. Only after retrying=false and truly idle may the
//   existing terminal classification inspect the final assistant message.
// - Bridge-managed AgentSessions get a deterministic non-persistent
//   SettingsManager override (5 retries, 2s base, 30s agent cap; provider
//   maxRetries=0, provider maxRetryDelayMs=60000) with unrelated settings
//   preserved and no settings.json write.
// - auto_retry_start/end emit only sanitized lifecycle metadata via a
//   dedicated provider_retry event; raw error text is never logged.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { PiRuntimeProtocol } from "../wbrp.mjs";
import {
  applyManagedRetryPolicy,
  createSdkSession,
  managedRetryOverride,
} from "../sdk-transport.mjs";
import { buildRecord } from "../logging.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-retry-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  const root = canonicalizeProjectsDir(path.join(tmp, "Projects"));
  return { tmp, root, app: fs.realpathSync(app) };
}

function makeAdapter(projects, transport, opts = {}) {
  const agentDir = path.join(projects.tmp, "agent-dir");
  try { fs.mkdirSync(agentDir, { recursive: true }); } catch { /* best effort */ }
  return new PiAdapter({
    projectsRoot: projects.root,
    agentDir,
    createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn,
    piUsable: true,
    piVersion: "0.87.0",
    ...opts,
  });
}

// ------------------------------------------------- authoritative busy state
test("all four isStreaming/isRetrying combinations project correctly", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.app);
  const sdk = transport.lastSession();
  const cases = [
    { streaming: false, retrying: false, status: "idle" },
    { streaming: true, retrying: false, status: "busy" },
    { streaming: false, retrying: true, status: "busy" },
    { streaming: true, retrying: true, status: "busy" },
  ];
  for (const row of cases) {
    sdk.setStreaming(row.streaming);
    sdk.setRetrying(row.retrying);
    assert.equal(
      await adapter.sessionStatus(projects.app, session.id),
      row.status,
      `streaming=${row.streaming} retrying=${row.retrying}`,
    );
    const got = await adapter.getSession(projects.app, session.id);
    assert.equal(got.status, row.status);
    assert.equal(got.state.isStreaming, row.streaming);
    assert.equal(got.state.isRetrying, row.retrying);
  }
  await adapter.shutdown();
});

test("isStreaming=false isRetrying=true is busy and never idle", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.app);
  const sdk = transport.lastSession();
  sdk.setStreaming(false);
  sdk.setRetrying(true);
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "busy");
  assert.equal((await adapter.getSession(projects.app, session.id)).status, "busy");
  assert.equal((await adapter.getSession(projects.app, session.id)).state.isRetrying, true);
  await adapter.shutdown();
});

test("sdk state read fails closed when access throws", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.app);
  const entry = adapter.sessions.get(session.id);
  Object.defineProperty(entry.session, "isStreaming", {
    get() { throw new Error("sdk state boom"); },
    configurable: true,
  });
  await assert.rejects(adapter.sessionStatus(projects.app, session.id), /boom|unavailable/);
  assert.equal(await adapter.getSession(projects.app, session.id).catch(() => null), null);
  await adapter.shutdown();
});

test("abort still aborts the session while retrying without disposing", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const session = await adapter.createSession(projects.app);
  const sdk = transport.lastSession();
  sdk.setStreaming(false);
  sdk.setRetrying(true);
  assert.equal(await adapter.abortSession(projects.app, session.id), true);
  assert.equal(sdk.abortCalls, 1);
  assert.equal(sdk.disposeCalls, 0);
  await adapter.shutdown();
});

// --------------------------------------- Runtime Protocol retry regression
function transient429Assistant() {
  return {
    id: "asst-429",
    role: "assistant",
    content: [{ type: "text", text: "" }],
    timestamp: 1758398400001,
    stopReason: "error",
    errorMessage: "opencode-go API error (429) rate_limit_exceeded",
  };
}

function completedAssistant() {
  return {
    id: "asst-ok",
    role: "assistant",
    content: [{ type: "text", text: "done after retry" }],
    timestamp: 1758398400002,
    stopReason: "stop",
  };
}

test("WBRP run stays active while native Pi retries a transient 429", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const protocol = new PiRuntimeProtocol(adapter);
  // PiRuntimeProtocol persists under the adapter agentDir; point it at an
  // isolated temp dir for this test.
  const profile = protocol.profileList().profiles[0];
  const conversation = await protocol.createConversation({
    workspaceId: "ws-retry",
    directory: projects.app,
    securityProfile: { id: profile.id, revision: profile.revision },
  });
  const run = await protocol.startRun(conversation.id, {
    input: [{ type: "text", text: "Do work" }],
  });
  assert.equal(run.phase, "active");
  const sdk = transport.lastSession();
  // Real failure mode: accepted run, last assistant message is a
  // transient 429 error, streaming has stopped but native retry backoff
  // is active.
  sdk.setMessages([
    { role: "user", content: "Do work", timestamp: 1758398400000 },
    transient429Assistant(),
  ]);
  sdk.setStreaming(false);
  sdk.setRetrying(true);
  const polled = await protocol.run(run.id);
  assert.equal(polled.phase, "active", "retry backoff must not terminalize");
  assert.equal(polled.activeState, "running");
  assert.notEqual(polled.outcome, "failed");
  assert.ok(!("error" in polled) || !polled.error);
  // Only after retrying=false and truly idle may terminal classification
  // inspect the final assistant message: still failing while the error
  // stands.
  sdk.setRetrying(false);
  const failed = await protocol.run(run.id);
  assert.equal(failed.phase, "terminal");
  assert.equal(failed.outcome, "failed");
  assert.ok(failed.error.includes("(error)"));
  await adapter.shutdown();
});

test("WBRP run succeeds after retry backoff resolves to completion", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport);
  const protocol = new PiRuntimeProtocol(adapter);
  const profile = protocol.profileList().profiles[0];
  const conversation = await protocol.createConversation({
    workspaceId: "ws-retry-ok",
    directory: projects.app,
    securityProfile: { id: profile.id, revision: profile.revision },
  });
  const run = await protocol.startRun(conversation.id, {
    input: [{ type: "text", text: "Do work" }],
  });
  const sdk = transport.lastSession();
  sdk.setMessages([
    { role: "user", content: "Do work", timestamp: 1 },
    transient429Assistant(),
  ]);
  sdk.setStreaming(false);
  sdk.setRetrying(true);
  assert.equal((await protocol.run(run.id)).phase, "active");
  sdk.setMessages([
    { role: "user", content: "Do work", timestamp: 1 },
    completedAssistant(),
  ]);
  sdk.setRetrying(false);
  const finished = await protocol.run(run.id);
  assert.equal(finished.phase, "terminal");
  assert.equal(finished.outcome, "succeeded");
  assert.equal(finished.result, "done after retry");
  await adapter.shutdown();
});

// ------------------------------------------------ managed retry policy
test("managed override is the deterministic 5x2s/30s policy", () => {
  assert.deepEqual(managedRetryOverride(), {
    retry: {
      enabled: true,
      maxRetries: 5,
      baseDelayMs: 2000,
      maxAgentDelayMs: 30000,
      provider: { maxRetries: 0, maxRetryDelayMs: 60000 },
    },
  });
});

test("managed sessions get effective retry settings with provider retries zero", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-managed-retry-"));
  const cwd = path.join(tmp, "ws");
  const agentDir = path.join(tmp, "agent");
  fs.mkdirSync(cwd, { recursive: true });
  fs.mkdirSync(agentDir, { recursive: true });
  const { SettingsManager } = await import("@earendil-works/pi-coding-agent");
  const manager = SettingsManager.create(fs.realpathSync(cwd), fs.realpathSync(agentDir), {
    projectTrusted: false,
  });
  applyManagedRetryPolicy(manager);
  assert.deepEqual(manager.getRetrySettings(), {
    enabled: true,
    maxRetries: 5,
    baseDelayMs: 2000,
    maxAgentDelayMs: 30000,
  });
  const provider = manager.getProviderRetrySettings();
  assert.equal(provider.maxRetries, 0);
  assert.equal(provider.maxRetryDelayMs, 60000);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("managed override preserves unrelated settings and never persists", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-retry-preserve-"));
  const cwd = path.join(tmp, "ws");
  const agentDir = path.join(tmp, "agent");
  fs.mkdirSync(cwd, { recursive: true });
  fs.mkdirSync(agentDir, { recursive: true });
  const { SettingsManager } = await import("@earendil-works/pi-coding-agent");
  const manager = SettingsManager.create(fs.realpathSync(cwd), fs.realpathSync(agentDir), {
    projectTrusted: false,
  });
  // Unrelated provider timeout preserved through the deep merge.
  manager.applyOverrides({ retry: { provider: { timeoutMs: 12345 } } });
  let saveCalls = 0;
  const originalSave = manager.save.bind(manager);
  manager.save = (...args) => {
    saveCalls += 1;
    return originalSave(...args);
  };
  applyManagedRetryPolicy(manager);
  assert.equal(saveCalls, 0, "applyOverrides must not call a persistence path");
  assert.equal(manager.getProviderRetrySettings().timeoutMs, 12345);
  assert.equal(manager.getProviderRetrySettings().maxRetries, 0);
  assert.equal(manager.getProviderRetrySettings().maxRetryDelayMs, 60000);
  assert.deepEqual(manager.getRetrySettings(), {
    enabled: true,
    maxRetries: 5,
    baseDelayMs: 2000,
    maxAgentDelayMs: 30000,
  });
  // Flush any queued writes, then confirm no settings file was written by
  // the override.
  await manager.flush().catch(() => {});
  assert.equal(fs.existsSync(path.join(fs.realpathSync(agentDir), "settings.json")), false);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("real createSdkSession applies the managed policy without a settings write", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-sdk-managed-"));
  const cwd = path.join(tmp, "ws");
  const agentDir = path.join(tmp, "agent");
  fs.mkdirSync(cwd, { recursive: true });
  fs.mkdirSync(agentDir, { recursive: true });
  const noopUi = {
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
  const { safeDefaultPolicy } = await import("../policy.mjs");
  const created = await createSdkSession({
    cwd: fs.realpathSync(cwd),
    agentDir: fs.realpathSync(agentDir),
    tools: ["read", "grep", "find", "ls"],
    excludeTools: null,
    extensionPaths: [],
    policy: safeDefaultPolicy(),
    uiContext: noopUi,
  });
  try {
    assert.deepEqual(created.settingsManager.getRetrySettings(), {
      enabled: true,
      maxRetries: 5,
      baseDelayMs: 2000,
      maxAgentDelayMs: 30000,
    });
    assert.equal(created.settingsManager.getProviderRetrySettings().maxRetries, 0);
    assert.equal(created.settingsManager.getProviderRetrySettings().maxRetryDelayMs, 60000);
    await created.settingsManager.flush().catch(() => {});
    assert.equal(
      fs.existsSync(path.join(fs.realpathSync(agentDir), "settings.json")),
      false,
      "managed override must not persist settings.json",
    );
  } finally {
    try { created.session.dispose(); } catch { /* best effort */ }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

// ------------------------------------------------ sanitized retry logging
test("auto_retry events emit sanitized provider_retry without raw error text", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const seen = [];
  const adapter = makeAdapter(projects, transport, {
    onLog: (level, component, event, fields) => seen.push({ level, component, event, fields }),
  });
  const session = await adapter.createSession(projects.app);
  const entry = adapter.sessions.get(session.id);
  const secret = `tok_secret_XYZ /tmp/secret-scratch model-x prompt-text ${"E".repeat(5000)}`;
  entry.session.emit({
    type: "auto_retry_start",
    attempt: 2,
    maxAttempts: 5,
    delayMs: 4000,
    errorMessage: `opencode-go API error (429) ${secret} {"body":"payload"}`,
  });
  entry.session.emit({
    type: "auto_retry_end",
    success: false,
    attempt: 5,
    finalError: `exhausted ${secret}`,
  });
  entry.session.emit({ type: "auto_retry_end", success: true, attempt: 3 });
  assert.equal(seen.length, 3);
  for (const row of seen) {
    assert.equal(row.level, "INFO");
    assert.equal(row.component, "pi-adapter");
    assert.equal(row.event, "provider_retry");
  }
  const [start, failed, succeeded] = seen.map((r) => r.fields);
  assert.equal(start.session_id, session.id);
  assert.equal(start.attempt, 2);
  assert.equal(start.max_attempts, 5);
  assert.equal(start.delay_ms, 4000);
  assert.equal(start.state, "retrying");
  assert.equal(failed.success, false);
  assert.equal(failed.state, "failed");
  assert.equal(failed.attempt, 5);
  assert.equal(succeeded.success, true);
  assert.equal(succeeded.state, "succeeded");
  const dumped = JSON.stringify(seen);
  assert.ok(!dumped.includes("tok_secret_XYZ"));
  assert.ok(!dumped.includes("/tmp/secret-scratch"));
  assert.ok(!dumped.includes("model-x"));
  assert.ok(!dumped.includes("prompt-text"));
  assert.ok(!dumped.includes("opencode-go API error"));
  assert.ok(!dumped.includes("payload"));
  assert.ok(!dumped.includes("errorMessage"));
  assert.ok(!dumped.includes("finalError"));
  await adapter.shutdown();
});

test("retry logging bounds scalars and never breaks the session when logging throws", async () => {
  const projects = makeProjects();
  const transport = createFakeTransport();
  const adapter = makeAdapter(projects, transport, {
    onLog: () => { throw new Error("log sink boom"); },
  });
  const session = await adapter.createSession(projects.app);
  const entry = adapter.sessions.get(session.id);
  // Must not throw even when the sink throws.
  entry.session.emit({ type: "auto_retry_start", attempt: 1, maxAttempts: 5, delayMs: 2000,
    errorMessage: "transient" });
  entry.session.emit({ type: "auto_retry_end", success: true, attempt: 1 });
  assert.equal(await adapter.sessionStatus(projects.app, session.id), "idle");
  await adapter.shutdown();

  // Bounded through the shared logging contract.
  const record = buildRecord("pi-adapter", "provider_retry", "INFO", {
    session_id: "x".repeat(5000),
    attempt: 2,
    max_attempts: 5,
    delay_ms: 4000,
    success: true,
    state: "succeeded",
    errorMessage: "raw must drop",
    finalError: "raw must drop",
    model: "provider/model must drop",
    prompt: "prompt must drop",
  });
  assert.ok(record);
  assert.ok(record.session_id.length <= 200);
  assert.equal(record.attempt, 2);
  assert.equal(record.max_attempts, 5);
  assert.equal(record.delay_ms, 4000);
  assert.equal(record.success, true);
  assert.equal(record.state, "succeeded");
  assert.ok(!("errorMessage" in record));
  assert.ok(!("finalError" in record));
  assert.ok(!("model" in record));
  assert.ok(!("prompt" in record));
  assert.ok(!JSON.stringify(record).includes("raw must drop"));
  assert.equal(buildRecord("pi-adapter", "provider_retry", "INFO", {
    session_id: "s", attempt: 9007199254740993,
  }).attempt, 1000000000000);
});
