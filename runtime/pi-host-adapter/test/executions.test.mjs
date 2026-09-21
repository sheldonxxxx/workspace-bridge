// 3C1 corrective: update-cursor journal + verified Pi 0.86.1 shapes.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { policyRevision } from "../policy.mjs";
import {
  ExecutionJournal, summarizeInput, summarizeResult, summaryRecord,
} from "../executions.mjs";
import { enforcementFingerprint } from "../fingerprint.mjs";
import { createFakeSpawn, respondState } from "./helpers.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

function makeProjects() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-exec-"));
  const app = path.join(tmp, "Projects", "app");
  fs.mkdirSync(app, { recursive: true });
  fs.writeFileSync(path.join(app, "notes.txt"), "hello\n");
  return { tmp, root: canonicalizeProjectsDir(path.join(tmp, "Projects")), app: fs.realpathSync(app) };
}

function v3Policy(overrides = {}) {
  return {
    version: 3,
    write_tools_enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**"],
    protected_template_exceptions: [],
    allow_session_always: true,
    external_access: { default_mode: "deny", roots: [] },
    shell_mode: "ask",
    ...overrides,
  };
}

// Verified Pi 0.86.1 bash ToolResult fixtures: success carries
// content[] + optional details{truncation, fullOutputPath}; failures
// surface as isError content text; fullOutputPath is never retained.
function bashSuccessResult(text) {
  return { content: [{ type: "text", text }], details: undefined };
}

function bashTruncatedResult(text) {
  return {
    content: [{ type: "text", text }],
    details: { truncation: { truncated: true, totalLines: 5000, outputLines: 2000 }, fullOutputPath: "/tmp/pi-bash-xyz" },
  };
}

test("fingerprints are deterministic, cover execution code, and path-free", () => {
  const a = enforcementFingerprint();
  const b = enforcementFingerprint();
  assert.equal(a.fingerprint, b.fingerprint);
  assert.match(a.fingerprint, /^[0-9a-f]{64}$/);
  assert.ok(Array.isArray(a.modules) && a.modules.includes("policy.mjs"));
  assert.ok(a.modules.includes("executions.mjs"));
  assert.ok(!JSON.stringify(a).includes("/tmp"));
  assert.ok(!JSON.stringify(a).includes("Volumes"));
});

test("evidence bounds on verified shapes: no fullOutputPath, no read contents", () => {
  const writeIn = summarizeInput("write", { path: "a.txt", content: "SECRET-" + "x".repeat(5000) });
  assert.equal(writeIn.ok, true);
  assert.ok(!JSON.stringify(writeIn.summary).includes("SECRET"));
  assert.ok(writeIn.summary.content_sha256?.match(/^[0-9a-f]{64}$/));
  const readOut = summarizeResult("read", { content: [{ type: "text", text: "SECRET-FILE-CONTENTS" }], count: 3 }, false);
  assert.ok(!JSON.stringify(readOut).includes("SECRET"));
  assert.equal(readOut.count, 3);
  // Bash timeout is SECONDS in real Pi args (verified bashSchema).
  const bashSecs = summarizeInput("bash", { command: "sleep 1", timeout: 45 });
  assert.equal(bashSecs.ok, true);
  assert.equal(bashSecs.summary.timeout_ms, 45000);
  const bashIn = summarizeInput("bash", { command: "x".repeat(20000), timeoutMs: 9999999 });
  assert.equal(bashIn.ok, true);
  assert.equal(bashIn.summary.command.length, 16384);
  assert.equal(bashIn.summary.timeout_ms, 300000);
  const bashOut = summarizeResult("bash", bashTruncatedResult("y".repeat(40000)), false);
  assert.equal(bashOut.output_preview.length, 32768);
  assert.equal(bashOut.truncated, true);
  assert.ok(!("fullOutputPath" in bashOut));
  const bashErr = summarizeResult("bash",
    { content: [{ type: "text", text: "out\nCommand exited with code 1" }] }, true);
  assert.equal(bashErr.is_error, true);
  assert.ok(bashErr.output_preview.includes("Command exited with code 1"));
  assert.ok(!("exit_code" in bashErr));
  const unknown = summarizeInput("powershell", { command: "ls" });
  assert.equal(unknown.ok, false);
  const unknownOut = summarizeResult("powershell", { anything: "x".repeat(90000) }, false);
  assert.ok(JSON.stringify(unknownOut).length < 2000);
  // List surface never includes output bodies.
  const rec = {
    seq: 1, update_seq: 2, tool_call_id: "c1", tool: "bash", state: "completed",
    started_at: "", ended_at: "", duration_ms: 1, is_error: false,
    permission_effect: "allow", permission_decision: "",
    truncated: false, input_summary: { command: "ls" },
  };
  const summary = summaryRecord(rec);
  assert.equal(summary.target_preview, "ls");
  assert.ok(!("output_preview" in summary));
  assert.ok(!("result_summary" in summary));
});

test("a/b/c: start advances cursor, end advances head, old cursor retrieves completion", () => {
  const journal = new ExecutionJournal({ maxRecords: 50 });
  const started = journal.start({
    toolCallId: "call-1", tool: "bash",
    inputSummary: { command: "ls -la", command_sha256: "a".repeat(64), timeout_ms: 30000 },
    permissionEffect: "ask",
  });
  assert.equal(started.state, "started");
  const headAfterStart = journal.head;
  assert.ok(headAfterStart >= 1);
  // (a) start -> poll -> cursor advances.
  const first = journal.read({ after: 0, limit: 10 });
  assert.equal(first.updates.length, 1);
  assert.equal(first.updates[0].tool_call_id, "call-1");
  assert.equal(first.updates[0].state, "started");
  const cursor = first.next;
  assert.ok(cursor >= headAfterStart);
  assert.equal(journal.read({ after: cursor, limit: 10 }).updates.length, 0);
  // (b) end of the same tool advances the head.
  journal.end({
    toolCallId: "call-1",
    resultSummary: summarizeResult("bash", bashSuccessResult("file-a\nfile-b\n"), false),
    isError: false,
  });
  assert.ok(journal.head > headAfterStart);
  // (c) poll after the OLD cursor returns the same toolCallId completed.
  const second = journal.read({ after: cursor, limit: 10 });
  assert.equal(second.updates.length, 1);
  assert.equal(second.updates[0].tool_call_id, "call-1");
  assert.equal(second.updates[0].state, "completed");
  assert.equal(second.updates[0].is_error, false);
  assert.ok(second.updates[0].result_summary.output_preview.includes("file-a"));
  assert.ok(second.next > cursor);
});

test("d: permission decision after a consumed start emits an update", () => {
  const journal = new ExecutionJournal({ maxRecords: 50 });
  journal.start({
    toolCallId: "call-p", tool: "edit",
    inputSummary: { target: "notes.txt" }, permissionEffect: "ask",
  });
  const consumed = journal.read({ after: 0, limit: 10 }).next;
  journal.setPermissionDecision("call-p", "once");
  assert.ok(journal.head > consumed);
  const later = journal.read({ after: consumed, limit: 10 });
  assert.equal(later.updates.length, 1);
  assert.equal(later.updates[0].tool_call_id, "call-p");
  assert.equal(later.updates[0].permission_decision, "once");
});

test("e: interrupted updates emit and calculate terminal timing", () => {
  const journal = new ExecutionJournal({ maxRecords: 50 });
  journal.start({
    toolCallId: "call-i", tool: "read",
    inputSummary: { target: "notes.txt" }, permissionEffect: "allow",
  });
  const cursor = journal.read({ after: 0, limit: 10 }).next;
  journal.markInterrupted();
  assert.ok(journal.head > cursor);
  const later = journal.read({ after: cursor, limit: 10 });
  assert.equal(later.updates.length, 1);
  assert.equal(later.updates[0].state, "interrupted");
  assert.ok(typeof later.updates[0].ended_at === "string" && later.updates[0].ended_at);
  assert.ok(later.updates[0].duration_ms === null || later.updates[0].duration_ms >= 0);
});

test("f: parallel calls ending out of order preserve each call and update order", () => {
  const journal = new ExecutionJournal({ maxRecords: 50 });
  journal.start({ toolCallId: "call-a", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  journal.start({ toolCallId: "call-b", tool: "bash", inputSummary: { command: "sleep 2" }, permissionEffect: "ask" });
  journal.start({ toolCallId: "call-c", tool: "grep", inputSummary: { target: ".", query: "x" }, permissionEffect: "allow" });
  // End out of order: c (error), then a, then b.
  journal.end({ toolCallId: "call-c", resultSummary: summarizeResult("grep", { content: [{ type: "text", text: "hit" }] }, true), isError: true });
  journal.end({ toolCallId: "call-a", resultSummary: summarizeResult("read", { count: 1 }, false), isError: false });
  journal.end({ toolCallId: "call-b", resultSummary: summarizeResult("bash", bashSuccessResult("done"), false), isError: false });
  const all = journal.read({ after: 0, limit: 10 });
  assert.equal(all.updates.length, 3);
  const byId = Object.fromEntries(all.updates.map((u) => [u.tool_call_id, u]));
  assert.equal(byId["call-a"].state, "completed");
  assert.equal(byId["call-b"].state, "completed");
  assert.equal(byId["call-c"].state, "completed");
  assert.equal(byId["call-c"].is_error, true);
  // Update order follows completion order: c, a, b.
  assert.deepEqual(all.updates.map((u) => u.tool_call_id), ["call-c", "call-a", "call-b"]);
});

test("adapter correlation: parallel starts/ends via RPC update the journal observably", async () => {
  const projects = makeProjects();
  const bag = createFakeSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi", agentDir: "/tmp/agent",
    timeoutMs: 1000, spawnFn: bag.spawnFn, maxLineBytes: 1024 * 1024,
  });
  const policy = v3Policy({ shell_mode: "ask", write_tools_enabled: false });
  const options = { permission_policy: policy, policy_revision: policyRevision(policy) };
  const promise = adapter.createSession(projects.app, "t", options);
  const child = bag.children[0];
  respondState(child, child.lastRequest().id, { sessionId: "ses-parallel-1" });
  const session = await promise;
  const emit = (message) => child.respond(message);
  const directory = projects.app;
  // Two parallel tool starts (read allow, bash ask with real-seconds timeout).
  emit({ type: "tool_execution_start", toolCallId: "rpc-a", toolName: "read", args: { path: "notes.txt" } });
  emit({ type: "tool_execution_start", toolCallId: "rpc-b", toolName: "bash", args: { command: "echo hi", timeout: 20 } });
  await new Promise((resolve) => setImmediate(resolve));
  const first = await adapter.readExecutions(directory, session.id, { after: 0, limit: 10 });
  assert.equal(first.updates.length, 2);
  const cursor = first.next;
  // End out of order with verified content/details shapes; bash end omits timeout units.
  emit({
    type: "tool_execution_end", toolCallId: "rpc-b", toolName: "bash",
    result: bashSuccessResult("hi\n"), isError: false,
  });
  emit({
    type: "tool_execution_end", toolCallId: "rpc-a", toolName: "read",
    result: { count: 12 }, isError: false,
  });
  await new Promise((resolve) => setImmediate(resolve));
  const second = await adapter.readExecutions(directory, session.id, { after: cursor, limit: 10 });
  assert.equal(second.updates.length, 2);
  const byId = Object.fromEntries(second.updates.map((u) => [u.tool_call_id, u]));
  assert.equal(byId["rpc-a"].state, "completed");
  assert.equal(byId["rpc-b"].state, "completed");
  assert.ok(byId["rpc-b"].result_summary.output_preview.includes("hi"));
  assert.ok(!("fullOutputPath" in byId["rpc-b"].result_summary));
  // Read tool result carries no contents.
  assert.ok(!JSON.stringify(byId["rpc-a"].result_summary).includes("hello"));
  await adapter.shutdown({ graceMs: 0 });
});

test("continuation floor in update space never drops new tools after many mutations", () => {
  // Divergence regression: two tools plus ends/decision drive the update
  // head far beyond the start ordinal count. A continuation capturing
  // floor=head must still attribute the next tool, whose stable start
  // cursor exceeds the floor even though its start ordinal does not.
  const journal = new ExecutionJournal({ maxRecords: 50 });
  journal.start({ toolCallId: "a1", tool: "read", inputSummary: { target: "a.txt" }, permissionEffect: "allow" });
  journal.start({ toolCallId: "a2", tool: "bash", inputSummary: { command: "make test" }, permissionEffect: "ask" });
  journal.end({ toolCallId: "a1", resultSummary: summarizeResult("read", { count: 4 }, false), isError: false });
  journal.end({
    toolCallId: "a2",
    resultSummary: summarizeResult("bash", bashSuccessResult("ok\n"), false),
    isError: false,
  });
  journal.setPermissionDecision("a2", "once");
  const floor = journal.head;
  assert.ok(floor > 2);
  const next = journal.start({ toolCallId: "b1", tool: "edit", inputSummary: { target: "b.txt" }, permissionEffect: "ask" });
  // Stable start cursor is in the SAME global update space as the floor.
  assert.ok(next.start_seq > floor);
  // The display ordinal has diverged below the floor and must never be
  // compared to it.
  assert.ok(next.start_order <= floor);
  const fresh = journal.read({ after: floor, limit: 10 });
  assert.equal(fresh.updates.length, 1);
  assert.equal(fresh.updates[0].tool_call_id, "b1");
  assert.equal(fresh.updates[0].state, "started");
  journal.end({ toolCallId: "b1", resultSummary: summarizeResult("edit", { content: [{ type: "text", text: "edited" }] }, false), isError: false });
  const done = journal.read({ after: fresh.next, limit: 10 });
  assert.equal(done.updates.length, 1);
  assert.equal(done.updates[0].tool_call_id, "b1");
  assert.equal(done.updates[0].state, "completed");
});

test("eviction reports explicit gap, never silent completeness", () => {
  const journal = new ExecutionJournal({ maxRecords: 2 });
  journal.start({ toolCallId: "c1", tool: "read", inputSummary: { target: "a" }, permissionEffect: "allow" });
  journal.start({ toolCallId: "c2", tool: "read", inputSummary: { target: "b" }, permissionEffect: "allow" });
  journal.start({ toolCallId: "c3", tool: "read", inputSummary: { target: "c" }, permissionEffect: "allow" });
  const gap = journal.read({ after: 0, limit: 10 });
  assert.equal(gap.audit_gap, true);
  assert.equal(gap.cursor_too_old, true);
  assert.equal(gap.updates.length, 0);
});

test("session creation returns fingerprint without paths", async () => {
  const projects = makeProjects();
  const bag = createFakeSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi", agentDir: "/tmp/agent",
    timeoutMs: 1000, spawnFn: bag.spawnFn, maxLineBytes: 1024 * 1024,
  });
  const policy = v3Policy();
  const options = { permission_policy: policy, policy_revision: policyRevision(policy) };
  const promise = adapter.createSession(projects.app, "t", options);
  const child = bag.children[0];
  respondState(child, child.lastRequest().id, { sessionId: "ses-fp-1" });
  const session = await promise;
  assert.match(session.enforcement_fingerprint, /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(session.fingerprint_modules || []).includes("/"));
  assert.ok(!String(session.enforcement_fingerprint).includes("/"));
  await adapter.shutdown({ graceMs: 0 });
});
