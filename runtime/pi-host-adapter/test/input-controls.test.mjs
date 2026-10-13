import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { PiAdapter } from "../adapter.mjs";
import { PiRuntimeProtocol } from "../wbrp.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-input-"));
  const agentDir = path.join(root, "agent");
  const projectsRoot = path.join(root, "projects");
  const directory = path.join(projectsRoot, "work");
  fs.mkdirSync(agentDir);
  fs.mkdirSync(directory, { recursive: true });
  const sessionFile = path.join(agentDir, "session.jsonl");
  fs.writeFileSync(sessionFile, "session\n");
  const adapter = {
    agentDir, projectsRoot, piVersion: "test", sessions: new Map(), busy: false,
    queued: [], received: [], submissions: [], aborts: 0,
    listModelsFn: async () => [],
    async createSession() {
      this.sessions.set("native", { sessionFile, disposed: false });
      return { id: "native", directory };
    },
    async sessionStatus() { return this.busy ? "busy" : "idle"; },
    async promptAsync(_dir, _id, text) { this.busy = true; this.received.push(text); return { accepted: true }; },
    async submitInput(_dir, _id, text, mode, _isCurrent, onStatus) {
      this.submissions.push({ text, mode });
      this.queued.push({ text, onStatus });
      return { accepted: true };
    },
    pendingInputCount() { return this.queued.length; },
    finish() {
      while (this.queued.length) {
        const item = this.queued.shift();
        this.received.push(item.text);
        item.onStatus("delivered");
      }
      this.busy = false;
    },
    async listPermissions() { return []; },
    executionHead() { return 0; },
    async readExecutions() { return { updates: [], next: 0, head: 0 }; },
    async messages() { return [{ role: "assistant", text: "done", completed: 1 }]; },
    async abortSession() {
      this.aborts++;
      for (const item of this.queued) item.onStatus("discarded");
      this.queued = [];
      this.busy = false;
    },
  };
  const protocol = new PiRuntimeProtocol(adapter);
  return { root, directory, adapter, protocol };
}

async function runIn({ protocol, directory }) {
  const profile = protocol.profileList().profiles[0];
  const conversation = await protocol.createConversation({ workspaceId: "ws", directory,
    securityProfile: { id: profile.id, revision: profile.revision } });
  return protocol.startRun(conversation.id, { input: [{ type: "text", text: "original" }] });
}

const input = (runId, requestId, text) => ({ expectedRunId: runId, requestId,
  input: [{ type: "text", text }] });

test("Pi run steering and queued follow-ups retain one run and FIFO without a read", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    assert.equal(ctx.protocol.descriptor().features.steering, 1);
    assert.equal(ctx.protocol.descriptor().features.queueing, 1);
    const steer = await ctx.protocol.submitInput(run.id, "steer", input(run.id, "s1", "change course"));
    assert.deepEqual(steer.inputMessages, [{ requestId: "s1", mode: "steer", status: "queued" }]);
    await ctx.protocol.submitInput(run.id, "queue", input(run.id, "q1", "first"));
    await ctx.protocol.submitInput(run.id, "queue", input(run.id, "q2", "second"));
    ctx.adapter.busy = false;
    assert.equal((await ctx.protocol.run(run.id)).phase, "active");
    ctx.adapter.busy = true;
    assert.deepEqual(ctx.adapter.submissions.map((item) => item.mode), ["steer", "queue", "queue"]);
    ctx.adapter.finish();
    assert.deepEqual(ctx.adapter.received, ["original", "change course", "first", "second"]);
    const completed = await ctx.protocol.run(run.id);
    assert.equal(completed.outcome, "succeeded");
    assert.equal(completed.inputMessages.length, 3);
    assert.ok(completed.inputMessages.every((item) => item.status === "delivered"));
    assert.ok(!JSON.stringify(completed).includes("change course"));
    assert.ok(!JSON.stringify(completed).includes("inputHash"));
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi run input retries, conflicts, limits and terminal retries are bounded", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    const first = input(run.id, "same", "hello");
    await ctx.protocol.submitInput(run.id, "queue", first);
    await ctx.protocol.submitInput(run.id, "queue", first);
    assert.equal(ctx.adapter.submissions.length, 1);
    await assert.rejects(ctx.protocol.submitInput(run.id, "steer", first),
      (error) => error.code === "idempotency_conflict");
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "same", "other")),
      (error) => error.code === "idempotency_conflict");
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input("wrong", "new", "x")),
      (error) => error.code === "invalid_arguments");
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "bad id", "x")),
      (error) => error.code === "invalid_arguments");
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "long", "x".repeat(8001))),
      (error) => error.code === "invalid_arguments");
    for (let n = 1; n < 20; n++) {
      await ctx.protocol.submitInput(run.id, "queue", input(run.id, `q${n}`, `text${n}`));
    }
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "overflow", "x")),
      (error) => error.code === "queue_full");
    ctx.adapter.finish();
    for (let n = 20; n < 50; n++) {
      await ctx.protocol.submitInput(run.id, "queue", input(run.id, `q${n}`, `text${n}`));
      ctx.adapter.finish();
      ctx.adapter.busy = true;
    }
    assert.equal((await ctx.protocol.run(run.id)).inputMessages.length, 50);
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "full", "x")),
      (error) => error.code === "input_limit");
    ctx.adapter.finish();
    assert.equal((await ctx.protocol.run(run.id)).phase, "terminal");
    await ctx.protocol.submitInput(run.id, "queue", first);
    assert.equal(ctx.adapter.submissions.length, 50);
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue", input(run.id, "later", "x")),
      (error) => error.code === "run_not_active");
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi cancellation and restart discard pending text without replay", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    await ctx.protocol.submitInput(run.id, "queue", input(run.id, "q1", "private text"));
    const persisted = fs.readFileSync(path.join(ctx.adapter.agentDir, "wbrp-state.json"), "utf8");
    assert.ok(!persisted.includes("private text"));
    const cancelled = await ctx.protocol.cancel(run.id);
    assert.equal(cancelled.outcome, "cancelled");
    assert.equal(cancelled.inputMessages[0].status, "discarded");
    assert.equal(ctx.adapter.queued.length, 0);
    assert.equal(ctx.adapter.aborts, 1);
    const reopened = new PiRuntimeProtocol(ctx.adapter);
    assert.equal((await reopened.run(run.id)).outcome, "cancelled");
    await reopened.submitInput(run.id, "queue", input(run.id, "q1", "private text"));
    assert.equal(ctx.adapter.submissions.length, 1);
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi adapter submits native steering and follow-up at the SDK boundary", async () => {
  const ctx = fixture();
  const transport = createFakeTransport();
  const adapter = new PiAdapter({ projectsRoot: fs.realpathSync(ctx.adapter.projectsRoot),
    agentDir: ctx.adapter.agentDir, createSessionFn: transport.createSdkSession,
    listModelsFn: transport.listModelsFn });
  try {
    const session = await adapter.createSession(ctx.directory);
    const native = transport.lastSession();
    const statuses = [];
    await adapter.submitInput(ctx.directory, session.id, "steer text", "steer",
      () => true, (status) => statuses.push(["steer", status]));
    await adapter.submitInput(ctx.directory, session.id, "queued text", "queue",
      () => true, (status) => statuses.push(["queue", status]));
    assert.deepEqual(native.promptCalls.map(({ opts }) => opts.streamingBehavior),
      ["steer", "followUp"]);
    assert.deepEqual(statuses, []);
    native.emit({ type: "message_start", message: { role: "user",
      content: [{ type: "text", text: "steer text" }] } });
    assert.deepEqual(statuses, [["steer", "delivered"]]);
    native.queuedFollowUp = ["queued text"];
    await adapter.abortSession(ctx.directory, session.id);
    assert.equal(native.clearQueueCalls, 1);
    assert.deepEqual(statuses, [["steer", "delivered"], ["queue", "discarded"]]);
    const failed = [];
    await adapter.submitInput(ctx.directory, session.id, "failed pending", "queue",
      () => true, (status) => failed.push(status));
    native.queuedFollowUp = ["failed pending"];
    native.emit({ type: "agent_end", willRetry: false,
      messages: [{ role: "assistant", stopReason: "error" }] });
    assert.equal(native.clearQueueCalls, 2);
    assert.deepEqual(failed, ["discarded"]);
    const ambiguous = [];
    await adapter.submitInput(ctx.directory, session.id, "cancelled in preflight", "queue",
      () => false, (status) => ambiguous.push(status));
    assert.equal(native.clearQueueCalls, 3);
    assert.equal(native.abortCalls, 2);
    assert.deepEqual(ambiguous, ["unknown"]);
    await adapter.promptAsync(ctx.directory, session.id, "same text");
    const identical = [];
    await adapter.submitInput(ctx.directory, session.id, "same text", "queue",
      () => true, (status) => identical.push(status));
    native.emit({ type: "message_start", message: { role: "user",
      content: [{ type: "text", text: "same text" }] } });
    assert.deepEqual(identical, []);
    native.emit({ type: "message_start", message: { role: "user",
      content: [{ type: "text", text: "same text" }] } });
    assert.deepEqual(identical, ["delivered"]);
  } finally {
    await adapter.shutdown();
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi restart interrupts an active run and never resubmits its queued input", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    let accept;
    ctx.adapter.submitInput = async () => new Promise((resolve) => { accept = resolve; });
    const pending = ctx.protocol.submitInput(run.id, "queue",
      input(run.id, "unsettled", "do not replay"));
    assert.equal(ctx.protocol.runs.get(run.id).inputMessages[0].status, "queued");
    const restarted = new PiRuntimeProtocol(ctx.adapter);
    const snapshot = await restarted.run(run.id);
    assert.equal(snapshot.outcome, "interrupted");
    assert.equal(snapshot.inputMessages[0].status, "unknown");
    assert.ok(!fs.readFileSync(path.join(ctx.adapter.agentDir, "wbrp-state.json"), "utf8")
      .includes("do not replay"));
    accept({ accepted: true });
    await pending;
    assert.deepEqual(ctx.adapter.received, ["original"]);
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi cancellation races an unacknowledged submission without a later prompt", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    let accept;
    ctx.adapter.submitInput = async (_dir, _id, _text, _mode, isCurrent) =>
      new Promise((resolve) => { accept = () => resolve({ accepted: isCurrent() }); });
    const pending = ctx.protocol.submitInput(run.id, "queue", input(run.id, "late", "late text"));
    const cancelled = await ctx.protocol.cancel(run.id);
    assert.equal(cancelled.outcome, "cancelled");
    accept();
    const after = await pending;
    assert.equal(after.inputMessages[0].status, "unknown");
    assert.deepEqual(ctx.adapter.received, ["original"]);
    assert.equal((await ctx.protocol.run(run.id)).outcome, "cancelled");
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});

test("Pi admission save failure rolls back receipt and submitting gate", async () => {
  const ctx = fixture();
  try {
    const run = await runIn(ctx);
    const save = ctx.protocol._save.bind(ctx.protocol);
    ctx.protocol._save = () => { throw new Error("storage unavailable"); };
    await assert.rejects(ctx.protocol.submitInput(run.id, "queue",
      input(run.id, "retry", "later")), /storage unavailable/);
    assert.deepEqual(ctx.protocol.runs.get(run.id).inputMessages, []);
    assert.equal(ctx.protocol.inputSubmitting.has(run.id), false);
    assert.equal(ctx.adapter.submissions.length, 0);
    ctx.protocol._save = save;
    const accepted = await ctx.protocol.submitInput(run.id, "queue",
      input(run.id, "retry", "later"));
    assert.equal(accepted.inputMessages[0].status, "queued");
    assert.equal(ctx.adapter.submissions.length, 1);
  } finally {
    fs.rmSync(ctx.root, { recursive: true, force: true });
  }
});
