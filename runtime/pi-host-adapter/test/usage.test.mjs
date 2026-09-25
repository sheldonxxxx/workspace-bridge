import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { aggregatePiUsage, isCompletedModelCall, isUsageFinalModelCall, normalizePiUsage, sanitizeMessage } from "../adapter.mjs";
import { PiRuntimeProtocol, terminalFailureError } from "../wbrp.mjs";
import { redactExtensionText } from "../executions.mjs";

test("normalizePiUsage maps native fields and rejects malformed counters", () => {
  assert.deepEqual(normalizePiUsage({
    input: 100, cacheRead: 10, cacheWrite: 5, output: 20, reasoning: 4, totalTokens: 120,
  }), {
    inputTokens: 100, cachedInputTokens: 10, cacheWriteInputTokens: 5,
    outputTokens: 20, reasoningOutputTokens: 4, totalTokens: 120,
  });
  assert.deepEqual(normalizePiUsage({ input: 0 }), { inputTokens: 0 });
  assert.equal(normalizePiUsage({}), null);
  assert.equal(normalizePiUsage(null), null);
  assert.equal(normalizePiUsage({ input: -1 }), null);
  assert.equal(normalizePiUsage({ input: 1.5 }), null);
  assert.equal(normalizePiUsage({ input: true }), null);
  assert.equal(normalizePiUsage({ input: 9007199254740992 }), null);
  // A present-but-malformed recognized counter invalidates the whole
  // snapshot instead of keeping a misleading partial.
  assert.equal(normalizePiUsage({ input: 10, output: -1 }), null);
  assert.equal(normalizePiUsage({ input: 10, output: 1.5 }), null);
  assert.equal(normalizePiUsage({ input: 10, output: true }), null);
  assert.equal(normalizePiUsage({ input: 10, output: 9007199254740992 }), null);
  assert.equal(normalizePiUsage({ input: 10, cacheRead: -2 }), null);
  // Unknown provider fields are ignored for forward compatibility.
  assert.deepEqual(normalizePiUsage({ input: 10, cost: 5, prompt: "x" }), {
    inputTokens: 10,
  });
});

test("usage-final model calls include failed turns, streaming snapshots are not summed", () => {
  const streaming = { role: "assistant", stopReason: "pending",
    usage: { input: 999, output: 999, totalTokens: 1998 } };
  assert.equal(isCompletedModelCall(streaming), false);
  assert.equal(isUsageFinalModelCall(streaming), false);
  for (const reason of ["pending", "deferred", "streaming", "unknown", undefined, null]) {
    assert.equal(isUsageFinalModelCall({ role: "assistant", stopReason: reason }), false);
  }
  // Usage-finality is broader than successful completion: error/aborted
  // with reported usage still count, and a native error never suppresses
  // accounting. Successful completion stays stop/length only.
  assert.equal(isUsageFinalModelCall({ role: "assistant", stopReason: "error" }), true);
  assert.equal(isUsageFinalModelCall({ role: "assistant", stopReason: "aborted" }), true);
  assert.equal(isUsageFinalModelCall({
    role: "assistant", stopReason: "error", errorMessage: "boom",
  }), true);
  assert.equal(isUsageFinalModelCall({ role: "assistant", stopReason: "toolUse" }), true);
  assert.equal(isUsageFinalModelCall({ role: "user", stopReason: "stop" }), false);
  const toolLoop = [
    { role: "assistant", stopReason: "toolUse",
      usage: { input: 100, output: 10, totalTokens: 110 } },
    { role: "assistant", stopReason: "toolUse",
      usage: { input: 120, output: 15, totalTokens: 135 } },
    { role: "assistant", stopReason: "stop",
      usage: { input: 150, output: 20, totalTokens: 170 } },
    streaming,
    { role: "assistant", stopReason: "deferred",
      usage: { input: 999, output: 1, totalTokens: 1000 } },
    { role: "assistant", stopReason: "error",
      usage: { input: 50, output: 5, totalTokens: 55 } },
    { role: "assistant", stopReason: "aborted",
      usage: { input: 7, output: 1, totalTokens: 8 } },
    // Native error text never suppresses already-consumed usage.
    { role: "assistant", stopReason: "error", errorMessage: "boom",
      usage: { input: 3, output: 1, totalTokens: 4 } },
  ];
  assert.deepEqual(aggregatePiUsage(toolLoop), {
    inputTokens: 430, outputTokens: 52, totalTokens: 482,
  });
  assert.equal(aggregatePiUsage([streaming]), null);
  assert.equal(aggregatePiUsage([]), null);
  // A malformed recognized counter invalidates that message only; other
  // valid messages still aggregate.
  assert.deepEqual(aggregatePiUsage([
    { role: "assistant", stopReason: "stop", usage: { input: 10, output: -1 } },
    { role: "assistant", stopReason: "stop", usage: { input: 5, output: 2, totalTokens: 7 } },
  ]), { inputTokens: 5, outputTokens: 2, totalTokens: 7 });
});

function fakeAdapter(root, messagesBySession = new Map()) {
  const agentDir = path.join(root, "agent");
  const projectsRoot = path.join(root, "projects");
  const directory = path.join(projectsRoot, "work");
  fs.mkdirSync(agentDir, { recursive: true });
  fs.mkdirSync(directory, { recursive: true });
  const sessionFile = path.join(agentDir, "native-session.jsonl");
  fs.writeFileSync(sessionFile, "session\n");
  const rawMessages = new Map(messagesBySession);
  const adapter = {
    agentDir, projectsRoot, piVersion: "test", sessions: new Map(),
    listModelsFn: async () => [{ provider: "p", id: "m", name: "M" }],
    async createSession(cwd, title, options) {
      this.sessions.set("native-session", { sessionFile });
      if (!rawMessages.has("native-session")) rawMessages.set("native-session", []);
      return { id: "native-session", directory };
    },
    async sessionStatus() { return this.busy ? "busy" : "idle"; },
    async promptAsync() { this.busy = true; return { accepted: true }; },
    async listPermissions() { return []; },
    executionHead() { return 0; },
    async readExecutions() { return { updates: [], next: 0, head: 0 }; },
    async messages() {
      const raw = rawMessages.get("native-session") || [];
      return raw.map((entry, index) => {
        // Mirror the sanitized production shape (bounded redacted error +
        // allowlisted failureKind) so terminal-failure tests exercise the
        // same observability contract without raw stopReason leakage.
        const completed = ["stop", "length"].includes(entry.stopReason)
          && !entry.errorMessage ? 1 : null;
        const out = {
          role: entry.role, text: entry.text || "",
          completed,
        };
        if (!completed && entry.role === "assistant") {
          const rawError = typeof entry.errorMessage === "string" && entry.errorMessage
            ? entry.errorMessage.slice(0, 300) : null;
          const error = rawError ? redactExtensionText(rawError).slice(0, 300) : null;
          if (error) out.error = error;
          if (error) out.failureKind = "error";
          else if (entry.stopReason === "aborted") out.failureKind = "aborted";
          else out.failureKind = "incomplete";
        }
        if (typeof entry.errorMessage === "string" && entry.errorMessage && !out.error) {
          out.error = redactExtensionText(entry.errorMessage.slice(0, 300)).slice(0, 300);
        }
        return out;
      });
    },
    sessionMessageCount(cwd, sessionId) {
      return (rawMessages.get(sessionId) || []).length;
    },
    assistantUsageSince(cwd, sessionId, afterIndex) {
      const raw = (rawMessages.get(sessionId) || []).slice(afterIndex);
      return aggregatePiUsage(raw);
    },
    __raw: rawMessages,
  };
  return { adapter, directory, rawMessages };
}

test("Pi run aggregates every model call for that run only", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-usage-"));
  try {
    const { adapter, directory, rawMessages } = fakeAdapter(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({
      workspaceId: "ws", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do work" }],
    });
    assert.equal(run.usage, undefined);
    rawMessages.set("native-session", [
      { role: "user", stopReason: "stop" },
      { role: "assistant", stopReason: "toolUse",
        usage: { input: 100, output: 10, totalTokens: 110 } },
      { role: "assistant", stopReason: "stop",
        usage: { input: 150, output: 20, totalTokens: 170 } },
    ]);
    adapter.busy = false;
    const finished = await protocol.run(run.id);
    assert.deepEqual(finished.usage, {
      inputTokens: 250, outputTokens: 30, totalTokens: 280,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi continuation starts a fresh accounting boundary", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-continuation-"));
  try {
    const { adapter, directory, rawMessages } = fakeAdapter(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({
      workspaceId: "ws", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const first = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "First" }],
      clientRunId: "first",
    });
    rawMessages.set("native-session", [
      { role: "assistant", stopReason: "stop",
        usage: { input: 100, output: 10, totalTokens: 110 } },
    ]);
    adapter.busy = false;
    assert.deepEqual((await protocol.run(first.id)).usage, {
      inputTokens: 100, outputTokens: 10, totalTokens: 110,
    });
    const second = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Second" }],
      clientRunId: "second",
    });
    // Messages from the first run must not leak into the continuation.
    const before = await protocol.run(second.id).catch(() => null);
    assert.ok(!before || before.usage === undefined);
    rawMessages.set("native-session", [
      { role: "assistant", stopReason: "stop",
        usage: { input: 100, output: 10, totalTokens: 110 } },
      { role: "assistant", stopReason: "stop",
        usage: { input: 50, output: 5, totalTokens: 55 } },
    ]);
    adapter.busy = false;
    assert.deepEqual((await protocol.run(second.id)).usage, {
      inputTokens: 50, outputTokens: 5, totalTokens: 55,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi absent provider usage stays absent and survives restart", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-absent-"));
  try {
    const { adapter, directory, rawMessages } = fakeAdapter(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({
      workspaceId: "ws", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "No usage" }],
    });
    rawMessages.set("native-session", [
      { role: "assistant", stopReason: "stop" },
    ]);
    adapter.busy = false;
    const finished = await protocol.run(run.id);
    assert.equal(finished.usage, undefined);
    const reopened = new PiRuntimeProtocol(adapter);
    assert.equal((await reopened.run(run.id)).usage, undefined);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("sanitizeMessage keeps success narrow while classifying failure safely", () => {
  const ok = sanitizeMessage({ id: "a", role: "assistant",
    content: [{ type: "text", text: "done" }], timestamp: 7, stopReason: "stop" }, 0);
  assert.equal(ok.completed, 7);
  assert.equal(ok.failureKind, undefined);
  const toolUse = sanitizeMessage({ id: "b", role: "assistant",
    content: [{ type: "text", text: "calling" }], timestamp: 8, stopReason: "toolUse" }, 1);
  assert.equal(toolUse.completed, null);
  assert.equal(toolUse.failureKind, "incomplete");
  const aborted = sanitizeMessage({ id: "c", role: "assistant",
    content: [], timestamp: 9, stopReason: "aborted" }, 2);
  assert.equal(aborted.completed, null);
  assert.equal(aborted.failureKind, "aborted");
  const failed = sanitizeMessage({ id: "d", role: "assistant",
    content: [], timestamp: 10, stopReason: "error", errorMessage: "boom" }, 3);
  assert.equal(failed.completed, null);
  assert.equal(failed.failureKind, "error");
  assert.equal(failed.error, "boom");
  assert.ok(!("stopReason" in failed));
  // Secret-bearing native errors are redacted and bounded.
  const secret = sanitizeMessage({ id: "e", role: "assistant",
    content: [], timestamp: 11, stopReason: "error",
    errorMessage: "provider failed with api_key=SECRET123 and more" }, 4);
  assert.ok(!secret.error.includes("SECRET123"));
  assert.ok(secret.error.includes("[redacted]"));
  assert.ok(secret.error.length <= 300);
});

test("terminalFailureError is bounded, classified, and redacted", () => {
  assert.equal(terminalFailureError(null),
    "Pi run ended without a completed assistant message");
  assert.equal(terminalFailureError({}),
    "Pi run ended without a completed assistant message");
  const classified = terminalFailureError({ failureKind: "aborted", error: null });
  assert.ok(classified.includes("(aborted)"));
  assert.ok(classified.length <= 300);
  const withDetail = terminalFailureError({ failureKind: "error", error: "boom" });
  assert.ok(withDetail.includes("(error)"));
  assert.ok(withDetail.includes("boom"));
  assert.ok(withDetail.length <= 300);
  // Unknown kinds fall back to a safe allowlisted label, never raw text.
  const unknown = terminalFailureError({ failureKind: "raw-payload", error: "x" });
  assert.ok(unknown.includes("(incomplete)"));
  assert.ok(!unknown.includes("raw-payload"));
});

test("Pi failed run retains usage with a useful sanitized error", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-failed-"));
  try {
    const { adapter, directory, rawMessages } = fakeAdapter(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({
      workspaceId: "ws", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do work" }],
    });
    rawMessages.set("native-session", [
      { role: "assistant", stopReason: "toolUse",
        usage: { input: 100, output: 10, totalTokens: 110 } },
      { role: "assistant", stopReason: "error", errorMessage: "provider boom",
        text: "partial", usage: { input: 50, output: 5, totalTokens: 55 } },
    ]);
    adapter.busy = false;
    const finished = await protocol.run(run.id);
    assert.equal(finished.phase, "terminal");
    assert.equal(finished.outcome, "failed");
    assert.deepEqual(finished.usage, {
      inputTokens: 150, outputTokens: 15, totalTokens: 165,
    });
    assert.ok(finished.error.includes("(error)"));
    assert.ok(finished.error.includes("provider boom"));
    assert.ok(finished.error.length <= 300);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Pi aborted run with secret-bearing error redacts and bounds", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-aborted-"));
  try {
    const { adapter, directory, rawMessages } = fakeAdapter(root);
    const protocol = new PiRuntimeProtocol(adapter);
    const profile = protocol.profileList().profiles[0];
    const conversation = await protocol.createConversation({
      workspaceId: "ws", directory,
      securityProfile: { id: profile.id, revision: profile.revision },
    });
    const run = await protocol.startRun(conversation.id, {
      input: [{ type: "text", text: "Do work" }],
    });
    const secretError = `aborted after api_key=SECRET1234567890 ${"x".repeat(500)}`;
    rawMessages.set("native-session", [
      { role: "assistant", stopReason: "aborted", errorMessage: secretError,
        usage: { input: 20, output: 2, totalTokens: 22 } },
    ]);
    adapter.busy = false;
    const finished = await protocol.run(run.id);
    assert.equal(finished.outcome, "failed");
    assert.deepEqual(finished.usage, {
      inputTokens: 20, outputTokens: 2, totalTokens: 22,
    });
    assert.ok(!finished.error.includes("SECRET1234567890"));
    assert.ok(finished.error.includes("[redacted]"));
    assert.ok(finished.error.length <= 300);
    assert.ok(finished.error.includes("(error)") || finished.error.includes("(aborted)"));
    assert.ok(!JSON.stringify(finished).includes("SECRET1234567890"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
