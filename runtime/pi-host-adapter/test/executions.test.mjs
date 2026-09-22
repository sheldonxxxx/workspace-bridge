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
import { OPTION_ALWAYS, OPTION_ONCE, OPTION_REJECT } from "../trusted-permission-extension.mjs";
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
  const firstById = Object.fromEntries(first.updates.map((u) => [u.tool_call_id, u]));
  // Normalized bash start carries the exact shell authority: ask mode
  // persists ask (not malformed deny), read persists allow.
  assert.equal(firstById["rpc-a"].permission_effect, "allow");
  assert.equal(firstById["rpc-b"].permission_effect, "ask");
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

test("shell_mode=allow bash start persists allow via normalized flow", async () => {
  const projects = makeProjects();
  const bag = createFakeSpawn();
  const adapter = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi", agentDir: "/tmp/agent",
    timeoutMs: 1000, spawnFn: bag.spawnFn, maxLineBytes: 1024 * 1024,
  });
  const policy = v3Policy({ shell_mode: "allow", write_tools_enabled: false });
  const options = { permission_policy: policy, policy_revision: policyRevision(policy) };
  const promise = adapter.createSession(projects.app, "t", options);
  const child = bag.children[0];
  respondState(child, child.lastRequest().id, { sessionId: "ses-allow-1" });
  const session = await promise;
  child.respond({ type: "tool_execution_start", toolCallId: "sh-allow", toolName: "bash",
    args: { command: "echo allow-ok", timeout: 10 } });
  await new Promise((resolve) => setImmediate(resolve));
  const entry = adapter.sessions.get(session.id);
  const preflight = entry.preflights.get("sh-allow");
  assert.ok(preflight);
  assert.equal(preflight.effect, "allow");
  // Preflight input is bounded exact authority only.
  assert.deepEqual(Object.keys(preflight.input).sort(), ["command", "timeoutMs"]);
  assert.equal(preflight.input.command, "echo allow-ok");
  assert.equal(preflight.input.timeoutMs, 10000);
  const listed = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 10 });
  const byId = Object.fromEntries(listed.updates.map((u) => [u.tool_call_id, u]));
  assert.equal(byId["sh-allow"].permission_effect, "allow");
  await adapter.shutdown({ graceMs: 0 });
});

test("shell_mode=ask bash start persists ask and survives trusted UI re-evaluation", async () => {
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
  respondState(child, child.lastRequest().id, { sessionId: "ses-ask-1" });
  const session = await promise;
  const entry = adapter.sessions.get(session.id);
  child.respond({ type: "tool_execution_start", toolCallId: "sh-ask", toolName: "bash",
    args: { command: "echo ask-ok", timeout: 20 } });
  await new Promise((resolve) => setImmediate(resolve));
  // Journal persists ask, not malformed deny.
  const started = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 10 });
  const byId = Object.fromEntries(started.updates.map((u) => [u.tool_call_id, u]));
  assert.equal(byId["sh-ask"].permission_effect, "ask");
  const preflight = entry.preflights.get("sh-ask");
  assert.ok(preflight);
  assert.equal(preflight.effect, "ask");
  assert.ok(preflight.commandHash?.match(/^[0-9a-f]{64}$/));
  assert.equal(preflight.timeoutMs, 20000);
  assert.ok(preflight.grantKey.includes(String(preflight.timeoutMs)));
  assert.ok(preflight.alwaysPattern.includes(preflight.commandHash));
  // Trusted UI ask with the exact marker/options re-evaluates the same
  // exact command+timeout and becomes answerable (no fail-closed cancel).
  child.respond({ type: "extension_ui_request", id: "ui-ask-1", method: "select",
    title: "WB_PERMISSION_V1:sh-ask",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  const pending = await adapter.listPermissions(projects.app, session.id);
  assert.equal(pending.length, 1);
  assert.equal(pending[0].tool, "bash");
  assert.equal(pending[0].resource, "echo ask-ok");
  assert.equal(pending[0].command_sha256, preflight.commandHash);
  assert.equal(pending[0].timeout_ms, 20000);
  assert.equal(pending[0].always_pattern, preflight.alwaysPattern);
  const answered = await adapter.respondPermission(projects.app, session.id, pending[0].id, "once");
  assert.deepEqual(answered, { ok: true, decision: "once" });
  const writes = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], { type: "extension_ui_response", id: "ui-ask-1", value: OPTION_ONCE });
  // The exact-command decision is linked to the execution journal.
  const after = await adapter.readExecutions(projects.app, session.id, { after: started.next, limit: 10 });
  const decisions = after.updates.filter((u) => u.tool_call_id === "sh-ask");
  assert.ok(decisions.length >= 1);
  assert.equal(decisions[0].permission_decision, "once");
  assert.equal(decisions[0].permission_effect, "ask");
  await adapter.shutdown({ graceMs: 0 });
});

test("shell always decision stays exact-command scoped and deny/malformed stay closed", async () => {
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
  respondState(child, child.lastRequest().id, { sessionId: "ses-always-1" });
  const session = await promise;
  const entry = adapter.sessions.get(session.id);
  // Malformed bash (no command) fails closed even in ask mode.
  child.respond({ type: "tool_execution_start", toolCallId: "sh-bad", toolName: "bash", args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.preflights.get("sh-bad")?.effect, "deny");
  child.respond({ type: "extension_ui_request", id: "ui-bad", method: "select",
    title: "WB_PERMISSION_V1:sh-bad",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter.listPermissions(projects.app, session.id), []);
  const badWrites = child.requests().filter((r) => r.type === "extension_ui_response" && r.id === "ui-bad");
  assert.equal(badWrites.length, 1);
  assert.deepEqual(badWrites[0], { type: "extension_ui_response", id: "ui-bad", cancelled: true });
  // Valid bash answered always records the exact hash+timeout scope.
  child.respond({ type: "tool_execution_start", toolCallId: "sh-always", toolName: "bash",
    args: { command: "echo always-ok", timeout: 5 } });
  await new Promise((resolve) => setImmediate(resolve));
  child.respond({ type: "extension_ui_request", id: "ui-always", method: "select",
    title: "WB_PERMISSION_V1:sh-always",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  const pending = await adapter.listPermissions(projects.app, session.id);
  assert.equal(pending.length, 1);
  const first = entry.preflights.get("sh-always");
  assert.ok(first.alwaysPattern.includes(first.commandHash));
  assert.ok(first.alwaysPattern.endsWith(`:${first.timeoutMs}`));
  const done = await adapter.respondPermission(projects.app, session.id, pending[0].id, "always");
  assert.deepEqual(done, { ok: true, decision: "always" });
  const journal = await adapter.readExecutions(projects.app, session.id, { after: 0, limit: 50 });
  const rec = journal.updates.find((u) => u.tool_call_id === "sh-always" && u.permission_decision === "always");
  assert.ok(rec);
  assert.equal(rec.permission_effect, "ask");
  await adapter.shutdown({ graceMs: 0 });

  // shell_mode=deny stays denied through the same normalized flow.
  const bag2 = createFakeSpawn();
  const adapter2 = new PiAdapter({
    projectsRoot: projects.root, piBinary: "pi", agentDir: "/tmp/agent",
    timeoutMs: 1000, spawnFn: bag2.spawnFn, maxLineBytes: 1024 * 1024,
  });
  const denyPolicy = v3Policy({ shell_mode: "deny", write_tools_enabled: false });
  const promise2 = adapter2.createSession(projects.app, "t",
    { permission_policy: denyPolicy, policy_revision: policyRevision(denyPolicy) });
  const child2 = bag2.children[0];
  respondState(child2, child2.lastRequest().id, { sessionId: "ses-deny-1" });
  const session2 = await promise2;
  child2.respond({ type: "tool_execution_start", toolCallId: "sh-deny", toolName: "bash",
    args: { command: "echo no", timeout: 5 } });
  await new Promise((resolve) => setImmediate(resolve));
  const denied = await adapter2.readExecutions(projects.app, session2.id, { after: 0, limit: 10 });
  const deniedById = Object.fromEntries(denied.updates.map((u) => [u.tool_call_id, u]));
  assert.equal(deniedById["sh-deny"].permission_effect, "deny");
  child2.respond({ type: "extension_ui_request", id: "ui-deny", method: "select",
    title: "WB_PERMISSION_V1:sh-deny",
    options: [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await adapter2.listPermissions(projects.app, session2.id), []);
  await adapter2.shutdown({ graceMs: 0 });
});
