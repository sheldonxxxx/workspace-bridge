import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { ExecutionStore } from "../execution-store.mjs";
import { ExecutionJournal, summarizeInput, summarizeResult } from "../executions.mjs";

const binding = (overrides = {}) => ({
  sessionId: "session-a",
  directory: "/work/project",
  policyRevision: "policy-a",
  extensionRevision: "extensions-a",
  ...overrides,
});

function withAgentDir(callback) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "execution-store-test-"));
  const agentDir = path.join(tmp, "agent");
  fs.mkdirSync(agentDir, { mode: 0o700 });
  try {
    return callback({ tmp, agentDir });
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function journalFor(store, options = {}) {
  return new ExecutionJournal({ ...options, onCommit: (snapshot) => store.save(snapshot) });
}

test("restores completed, started, and permission audit state with monotonic cursors", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  assert.equal(store.load(), null);
  const journal = journalFor(store, { maxRecords: 10 });

  journal.start({ toolCallId: "completed-call", tool: "bash",
    inputSummary: summarizeInput("bash", { command: "printf ok" }).summary, permissionEffect: "allow" });
  journal.end({ toolCallId: "completed-call",
    resultSummary: summarizeResult("bash", { content: [{ type: "text", text: "ok" }] }, false), isError: false });
  journal.start({ toolCallId: "pending-call", tool: "edit",
    inputSummary: summarizeInput("edit", { path: "notes.txt", oldText: "a", newText: "b" }).summary,
    permissionEffect: "ask" });
  journal.setPermissionDecision("pending-call", "once");
  const beforeRestart = journal.head;

  const restoredSnapshot = new ExecutionStore({ agentDir, binding: binding() }).load();
  const restoredStore = new ExecutionStore({ agentDir, binding: binding() });
  const restored = journalFor(restoredStore, { snapshot: restoredSnapshot });
  assert.equal(restored.head, beforeRestart);
  assert.equal(restored.snapshot().updateSeq, beforeRestart);
  const byId = new Map(restored.snapshot().records.map((record) => [record.tool_call_id, record]));
  assert.equal(byId.get("completed-call").state, "completed");
  assert.ok(byId.get("completed-call").start_seq < byId.get("completed-call").update_seq);
  assert.equal(byId.get("pending-call").state, "started");
  assert.equal(byId.get("pending-call").permission_effect, "ask");
  assert.equal(byId.get("pending-call").permission_decision, "once");

  restored.end({ toolCallId: "pending-call", resultSummary: {}, isError: false });
  assert.ok(restored.head > beforeRestart);
  assert.equal(new ExecutionStore({ agentDir, binding: binding() }).load().updateSeq, restored.head);
}));

test("interruption is idempotent and a late end cannot make an interrupted call completed", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "call", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  journal.markInterrupted();
  const interrupted = journal.snapshot().records[0];
  const cursor = journal.head;

  journal.markInterrupted();
  journal.end({ toolCallId: "call", resultSummary: { count: 1 }, isError: false });
  assert.equal(journal.head, cursor);
  assert.equal(journal.snapshot().records[0].state, "interrupted");
  assert.equal(journal.snapshot().records[0].update_seq, interrupted.update_seq);
  assert.equal(new ExecutionStore({ agentDir, binding: binding() }).load().records[0].state, "interrupted");
}));

test("a persistence failure latches the journal and never exposes volatile success", () => {
  const journal = new ExecutionJournal({ onCommit: () => { throw new Error("disk failure"); } });
  assert.throws(() => journal.start({ toolCallId: "volatile", tool: "read",
    inputSummary: { target: "a.txt" }, permissionEffect: "allow" }), /execution journal unavailable/);
  for (const action of [() => journal.assertHealthy(), () => journal.snapshot(), () => journal.read({ after: 0 })]) {
    assert.throws(action, /execution journal unavailable/);
  }
});

test("complete corrupt frames fail closed; a partial tail is discarded before durable append", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "first", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  const file = store.file;

  fs.appendFileSync(file, "{\"version\":");
  const recoveredStore = new ExecutionStore({ agentDir, binding: binding() });
  const recoveredSnapshot = recoveredStore.load();
  assert.equal(recoveredSnapshot.records[0].tool_call_id, "first");
  const recovered = journalFor(recoveredStore, { snapshot: recoveredSnapshot });
  recovered.end({ toolCallId: "first", resultSummary: { count: 2 }, isError: false });
  assert.equal(new ExecutionStore({ agentDir, binding: binding() }).load().records[0].state, "completed");

  fs.appendFileSync(file, "{\"not\":\"valid commit\"}\n");
  const corruptStore = new ExecutionStore({ agentDir, binding: binding() });
  assert.throws(() => corruptStore.load(), /execution journal unavailable/);
  assert.throws(() => corruptStore.load(), /execution journal unavailable/);
}));

test("session, directory, and security revisions isolate stores; symlink roots cannot escape", () => withAgentDir(({ tmp, agentDir }) => {
  const baseStore = new ExecutionStore({ agentDir, binding: binding({ sessionId: "../../escape" }) });
  const journal = journalFor(baseStore);
  journal.start({ toolCallId: "bound", tool: "read", inputSummary: { target: "x" }, permissionEffect: "allow" });
  assert.ok(baseStore.file.startsWith(`${baseStore.root}${path.sep}`));

  const variants = [
    binding({ sessionId: "another-session" }),
    binding({ directory: "/work/other" }),
    binding({ policyRevision: "policy-b" }),
    binding({ extensionRevision: "extensions-b" }),
  ];
  for (const variant of variants) {
    assert.equal(new ExecutionStore({ agentDir, binding: variant }).load(), null);
  }

  const outside = path.join(tmp, "outside");
  fs.mkdirSync(outside, { mode: 0o700 });
  const escapedAgent = path.join(tmp, "symlink-agent");
  fs.mkdirSync(escapedAgent, { mode: 0o700 });
  fs.symlinkSync(outside, path.join(escapedAgent, "execution-journals"), "dir");
  const escaped = new ExecutionStore({ agentDir: escapedAgent, binding: binding() });
  assert.throws(() => escaped.save(journal.snapshot()), /execution journal unavailable/);
  assert.deepEqual(fs.readdirSync(outside), []);
}));

test("an appended older checkpoint cannot silently reset the audit cursor", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "call", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  const checkpoint = fs.readFileSync(store.file, "utf8");
  journal.end({ toolCallId: "call", resultSummary: { count: 1 }, isError: false });
  fs.appendFileSync(store.file, checkpoint);
  assert.throws(() => new ExecutionStore({ agentDir, binding: binding() }).load(),
    /execution journal unavailable/);
}));

test("stored frames redact normalized sensitive strings and retain no raw file content", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "bash", tool: "bash",
    inputSummary: summarizeInput("bash", { command: "curl https://user:pass@example.test/?token=TOPSECRET" }).summary,
    permissionEffect: "allow" });
  journal.end({ toolCallId: "bash", resultSummary: summarizeResult("bash", {
    content: [{ type: "text", text: "Bearer RESULTSECRET" }],
  }, false), isError: false });
  journal.start({ toolCallId: "write", tool: "write",
    inputSummary: summarizeInput("write", { path: "notes.txt", content: "RAW_WRITE_CONTENT" }).summary,
    permissionEffect: "ask" });
  journal.end({ toolCallId: "write", resultSummary: summarizeResult("write", {
    content: [{ type: "text", text: "RAW_READ_CONTENT" }],
  }, false), isError: false });
  journal.start({ toolCallId: "read", tool: "read",
    inputSummary: summarizeInput("read", { path: "secret.txt" }).summary,
    permissionEffect: "allow" });
  journal.end({ toolCallId: "read", resultSummary: summarizeResult("read", {
    content: [{ type: "text", text: "RAW_READ_CONTENT" }],
  }, false), isError: false });

  const bytes = fs.readFileSync(store.file, "utf8");
  assert.ok(!bytes.includes("TOPSECRET"));
  assert.ok(!bytes.includes("RESULTSECRET"));
  assert.ok(!bytes.includes("RAW_WRITE_CONTENT"));
  assert.ok(!bytes.includes("RAW_READ_CONTENT"));
  assert.ok(bytes.includes("[redacted]"));
  assert.ok(bytes.includes("content_sha256"));
}));

test("bounded journal compacts and restores retained records and eviction coverage", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store, { maxRecords: 2 });
  for (let i = 0; i < 90; i += 1) {
    journal.start({ toolCallId: `call-${i}`, tool: "read", inputSummary: { target: `file-${i}` }, permissionEffect: "allow" });
  }
  const file = fs.readFileSync(store.file);
  assert.ok(file.length < 64 * 1024);
  assert.ok(file.filter((byte) => byte === 0x0a).length <= 64, "compaction should keep the log below its frame threshold");

  const snapshot = new ExecutionStore({ agentDir, binding: binding() }).load();
  assert.equal(snapshot.records.length, 2);
  assert.ok(snapshot.evictedThrough > 0);
  assert.equal(snapshot.updateSeq, journal.head);
  assert.deepEqual(snapshot.records.map((record) => record.tool_call_id), ["call-88", "call-89"]);
}));

test("orphaned compaction temp after SIGKILL leaves the previous checkpoint restorable", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "before-crash", tool: "read", inputSummary: { target: "safe.txt" }, permissionEffect: "allow" });
  const storePath = path.resolve(new URL("../execution-store.mjs", import.meta.url).pathname);
  const source = `
    import fs from "node:fs";
    import { ExecutionStore } from ${JSON.stringify(`file://${storePath}`)};
    const store = new ExecutionStore({ agentDir: ${JSON.stringify(agentDir)}, binding: ${JSON.stringify(binding())} });
    const snapshot = store.load();
    fs.renameSync = () => process.kill(process.pid, "SIGKILL");
    store._compact(snapshot);
  `;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.equal(child.signal, "SIGKILL", `child should die before checkpoint rename; stderr: ${child.stderr}`);
  assert.ok(fs.existsSync(store.file), "the pre-crash journal should remain in place");
  assert.ok(fs.readdirSync(store.root).some((name) => name.startsWith(`.${store.bindingHash}.`) && name.endsWith(".tmp")));
  const restored = new ExecutionStore({ agentDir, binding: binding() }).load();
  assert.equal(restored.records[0].tool_call_id, "before-crash");
}));

test("short writes are retried to completion and zero-progress writes latch failure", () => withAgentDir(({ agentDir }) => {
  const shortWriteOriginal = fs.writeSync;
  try {
    fs.writeSync = (fd, buffer, offset = 0, length = buffer.length - offset, position = null) =>
      shortWriteOriginal(fd, buffer, offset, Math.min(length, 7), position);
    const shortWriteStore = new ExecutionStore({ agentDir, binding: binding() });
    const shortWriteJournal = journalFor(shortWriteStore);
    shortWriteJournal.start({ toolCallId: "short-write", tool: "read", inputSummary: { target: "ok.txt" }, permissionEffect: "allow" });
    assert.equal(new ExecutionStore({ agentDir, binding: binding() }).load().records[0].tool_call_id, "short-write");
  } finally {
    fs.writeSync = shortWriteOriginal;
  }

  const zeroAgentDir = path.join(agentDir, "zero-progress");
  fs.mkdirSync(zeroAgentDir, { mode: 0o700 });
  const originalWriteSync = fs.writeSync;
  try {
    fs.writeSync = () => 0;
    const zeroStore = new ExecutionStore({ agentDir: zeroAgentDir, binding: binding() });
    const zeroJournal = journalFor(zeroStore);
    const validSnapshot = new ExecutionJournal().snapshot();
    assert.throws(() => zeroJournal.start({ toolCallId: "zero-write", tool: "read",
      inputSummary: { target: "never.txt" }, permissionEffect: "allow" }), /execution journal unavailable/);
    assert.throws(() => zeroStore.save(validSnapshot), /execution journal unavailable/);
  } finally {
    fs.writeSync = originalWriteSync;
  }
}));

test("missing journal after an initial commit fails instead of creating a delta-only file", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "committed", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  fs.unlinkSync(store.file);
  assert.throws(() => journal.end({ toolCallId: "committed", resultSummary: { count: 1 }, isError: false }),
    /execution journal unavailable/);
  assert.throws(() => journal.assertHealthy(), /execution journal unavailable/);
  assert.equal(fs.existsSync(store.file), false);
}));

test("saving an identical snapshot is idempotent and remains restorable", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store);
  journal.start({ toolCallId: "same", tool: "read", inputSummary: { target: "same.txt" }, permissionEffect: "allow" });
  const snapshot = journal.snapshot();
  const initialSize = fs.statSync(store.file).size;
  store.save(snapshot);
  assert.equal(fs.statSync(store.file).size, initialSize);
  const restored = new ExecutionStore({ agentDir, binding: binding() }).load();
  assert.deepEqual(restored, snapshot);
}));

test("maximum multibyte command and output summaries persist beyond a 64 KiB record cap", () => withAgentDir(({ agentDir }) => {
  const store = new ExecutionStore({ agentDir, binding: binding() });
  const journal = journalFor(store, { maxRecords: 3 });
  const command = "界".repeat(16384);
  journal.start({ toolCallId: "large-command", tool: "bash",
    inputSummary: summarizeInput("bash", { command }).summary, permissionEffect: "allow" });
  const outputPreview = "界".repeat(32768);
  journal.start({ toolCallId: "large-output", tool: "bash", inputSummary: { command: "true" }, permissionEffect: "allow" });
  journal.end({ toolCallId: "large-output", resultSummary: { output_preview: outputPreview }, isError: false });

  const restored = new ExecutionStore({ agentDir, binding: binding() }).load();
  assert.equal(restored.records.find((record) => record.tool_call_id === "large-command").input_summary.command.length, 16384);
  assert.equal(restored.records.find((record) => record.tool_call_id === "large-output").result_summary.output_preview.length, 32768);
}));

test("successful synchronous save survives child process SIGKILL and restart", () => withAgentDir(({ agentDir }) => {
  const storePath = path.resolve(new URL("../execution-store.mjs", import.meta.url).pathname);
  const executionsPath = path.resolve(new URL("../executions.mjs", import.meta.url).pathname);
  const source = `
    import { ExecutionStore } from ${JSON.stringify(`file://${storePath}`)};
    import { ExecutionJournal } from ${JSON.stringify(`file://${executionsPath}`)};
    const store = new ExecutionStore({ agentDir: ${JSON.stringify(agentDir)}, binding: ${JSON.stringify(binding())} });
    const journal = new ExecutionJournal({ onCommit: (snapshot) => store.save(snapshot) });
    journal.start({ toolCallId: "killed-after-save", tool: "read", inputSummary: { target: "durable.txt" }, permissionEffect: "allow" });
    process.kill(process.pid, "SIGKILL");
  `;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.equal(child.signal, "SIGKILL", `child should be killed after save; stderr: ${child.stderr}`);
  const snapshot = new ExecutionStore({ agentDir, binding: binding() }).load();
  assert.equal(snapshot.records.length, 1);
  assert.equal(snapshot.records[0].tool_call_id, "killed-after-save");
  assert.equal(snapshot.records[0].state, "started");
}));
