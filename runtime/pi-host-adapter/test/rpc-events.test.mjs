// 3B1 RPC framing additions: bounded event callback for the permission
// correlation subset, validated direct extension_ui_response writes that
// never enter the command pending map, and preserved strict behavior.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PiRpcProcess } from "../rpc.mjs";
import { createFakeSpawn, startRpc } from "./helpers.mjs";

function makeRpc(bag, opts = {}) {
  return new PiRpcProcess({
    binary: "pi",
    cwd: "/tmp",
    agentDir: "/tmp/agent",
    spawnFn: bag.spawnFn,
    timeoutMs: 1000,
    ...opts,
  });
}

test("event callback receives only the correlation subset", async () => {
  const bag = createFakeSpawn();
  const seen = [];
  const rpc = makeRpc(bag, { onEvent: (message) => { seen.push(message); } });
  const child = await startRpc(rpc, bag.children);
  child.respond({ type: "tool_execution_start", toolCallId: "c1", toolName: "edit", args: {} });
  child.respond({ type: "extension_ui_request", id: "u1", method: "select", title: "t", options: [] });
  child.respond({ type: "tool_execution_end", toolCallId: "c1", toolName: "edit" });
  child.respond({ type: "agent_settled" });
  child.respond({ type: "ui_prompt_start", kind: "select" });
  child.respond({ type: "message_update", foo: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(seen.map((m) => m.type),
    ["tool_execution_start", "extension_ui_request", "tool_execution_end"]);
  // Listener errors never break framing: a later command still works.
  await rpc.close({ graceMs: 0 });
});

test("throwing event listener never breaks framing", async () => {
  const bag = createFakeSpawn();
  const rpc = makeRpc(bag, { onEvent: () => { throw new Error("listener boom"); } });
  const child = await startRpc(rpc, bag.children);
  child.respond({ type: "tool_execution_start", toolCallId: "c1", toolName: "edit", args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  const pending = rpc.command("get_state");
  child.respond({ id: child.lastRequest().id, type: "response", command: "get_state",
    success: true, data: { sessionId: "s", isStreaming: false } });
  await pending;
  await rpc.close({ graceMs: 0 });
});

test("writeUiResponse validates and never enters the pending map", async () => {
  const bag = createFakeSpawn();
  const rpc = makeRpc(bag);
  const child = await startRpc(rpc, bag.children);
  const pendingBefore = rpc.pending.size;
  assert.equal(await rpc.writeUiResponse({ id: "u1", value: "Allow once" }), true);
  assert.equal(await rpc.writeUiResponse({ id: "u2", cancelled: true }), true);
  assert.equal(await rpc.writeUiResponse({ id: "u3", confirmed: true }), true);
  assert.equal(rpc.pending.size, pendingBefore);
  // Invalid payloads are rejected without writing.
  assert.equal(await rpc.writeUiResponse(null), false);
  assert.equal(await rpc.writeUiResponse({}), false);
  assert.equal(await rpc.writeUiResponse({ id: "", value: "x" }), false);
  assert.equal(await rpc.writeUiResponse({ id: "u", value: "x".repeat(500) }), false);
  assert.equal(await rpc.writeUiResponse({ id: "u" }), false);
  const lines = child.requests().filter((r) => r.type === "extension_ui_response");
  assert.equal(lines.length, 3);
  assert.deepEqual(lines[0], { type: "extension_ui_response", id: "u1", value: "Allow once" });
  assert.deepEqual(lines[1], { type: "extension_ui_response", id: "u2", cancelled: true });
  // A dead session refuses writes.
  rpc.failClosed("test");
  assert.equal(await rpc.writeUiResponse({ id: "u9", value: "x" }), false);
});

test("write failure resolves false so pending is never removed unconfirmed", async () => {
  const bag = createFakeSpawn();
  const rpc = makeRpc(bag);
  await startRpc(rpc, bag.children);
  const child = bag.children[0];
  child.stdin.write = (line, cb) => {
    if (typeof cb === "function") cb(new Error("EPIPE"));
    return false;
  };
  assert.equal(await rpc.writeUiResponse({ id: "u1", value: "Allow once" }), false);
  assert.equal(await rpc.writeUiResponse({ id: "u2", cancelled: true }), false);
});

test("tool start events are normalized to path-only metadata plus separate audit", async () => {
  const bag = createFakeSpawn();
  const seen = [];
  const rpc = makeRpc(bag, { onEvent: (message) => { seen.push(message); } });
  const child = await startRpc(rpc, bag.children);
  const secret = "sk-secret-" + "x".repeat(5000);
  child.respond({ type: "tool_execution_start", toolCallId: "c1", toolName: "write",
    args: { path: "notes.txt", content: secret } });
  child.respond({ type: "tool_execution_start", toolCallId: "c2", toolName: "grep",
    args: { pattern: "benign-search", path: "sub" } });
  child.respond({ type: "tool_execution_start", toolCallId: "c3", toolName: "bash",
    args: { command: "rm -rf /" } });
  child.respond({ type: "tool_execution_end", toolCallId: "c1", toolName: "write",
    result: { content: secret } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(seen.length, 4);
  // Permission metadata stays path-only for files, exact command+timeout
  // for bash (never {}: shell policy needs the exact authority identity).
  assert.deepEqual(seen[0].input, { path: "notes.txt" });
  assert.deepEqual(seen[1].input, { path: "sub" });
  assert.deepEqual(seen[2].input, { command: "rm -rf /", timeoutMs: 30000 });
  // Separate audit path carries bounded evidence, never raw secrets.
  assert.equal(seen[0].auditInput.target, "notes.txt");
  assert.ok(seen[0].auditInput.content_sha256?.match(/^[0-9a-f]{64}$/));
  assert.ok(!JSON.stringify(seen[0].auditInput).includes("sk-secret"));
  assert.ok(!JSON.stringify(seen[1].auditInput).includes("sk-secret"));
  assert.equal(seen[2].auditInput.command, "rm -rf /");
  assert.ok(seen[2].auditInput.command_sha256?.match(/^[0-9a-f]{64}$/));
  assert.ok(!JSON.stringify(seen[3].auditResult).includes("sk-secret"));
  assert.ok(!("fullOutputPath" in (seen[3].auditResult || {})));
  assert.ok(!JSON.stringify(seen).includes("sk-secret"));
  await rpc.close({ graceMs: 0 });
});

test("bash permission input keeps only bounded exact command+timeout", async () => {
  const bag = createFakeSpawn();
  const seen = [];
  const rpc = makeRpc(bag, { onEvent: (message) => { seen.push(message); } });
  const child = await startRpc(rpc, bag.children);
  // Valid bash with seconds timeout plus unrelated raw fields that must
  // never enter permission metadata.
  child.respond({ type: "tool_execution_start", toolCallId: "b1", toolName: "bash",
    args: { command: "echo hi", timeout: 20, env: { SECRET: "x" }, shell: "bash", extra: "drop" } });
  // Alias + ms-timeout canonicalization uses the same single parser.
  child.respond({ type: "tool_execution_start", toolCallId: "b2", toolName: "bash",
    args: { cmd: "echo hi", timeoutMs: 20000 } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(seen.length, 2);
  // Exact authority fields only: no env, no arbitrary args, no audit keys.
  assert.deepEqual(seen[0].input, { command: "echo hi", timeoutMs: 20000 });
  assert.deepEqual(Object.keys(seen[0].input).sort(), ["command", "timeoutMs"]);
  assert.deepEqual(seen[1].input, { command: "echo hi", timeoutMs: 20000 });
  assert.ok(!JSON.stringify(seen.map((m) => m.input)).includes("SECRET"));
  assert.ok(!JSON.stringify(seen.map((m) => m.input)).includes("extra"));
  assert.ok(!JSON.stringify(seen.map((m) => m.input)).includes("command_sha256"));
  // Evaluator computes the same hash/timeout from the normalized form as
  // from the original Pi args.
  const { evaluateToolCall } = await import("../policy.mjs");
  const { extractBashCommand } = await import("../policy.mjs");
  const fromOrig = extractBashCommand({ command: "echo hi", timeout: 20 });
  const fromNorm = extractBashCommand(seen[0].input);
  assert.equal(fromOrig.ok, true);
  assert.equal(fromNorm.ok, true);
  assert.equal(fromNorm.commandHash, fromOrig.commandHash);
  assert.equal(fromNorm.timeoutMs, fromOrig.timeoutMs);
  assert.equal(fromNorm.command, fromOrig.command);
  const allowPolicy = {
    version: 3, write_tools_enabled: false,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [], protected_template_exceptions: [],
    allow_session_always: true, external_access: { default_mode: "deny", roots: [] },
    shell_mode: "allow",
  };
  const vOrig = evaluateToolCall({ cwd: "/tmp", policy: allowPolicy, toolName: "bash",
    input: { command: "echo hi", timeout: 20 } });
  const vNorm = evaluateToolCall({ cwd: "/tmp", policy: allowPolicy, toolName: "bash",
    input: seen[0].input });
  assert.equal(vNorm.effect, "allow");
  assert.equal(vNorm.effect, vOrig.effect);
  assert.equal(vNorm.commandHash, vOrig.commandHash);
  assert.equal(vNorm.timeoutMs, vOrig.timeoutMs);
  await rpc.close({ graceMs: 0 });
});

test("malformed and truncated bash inputs fail closed in permission metadata", async () => {
  const bag = createFakeSpawn();
  const seen = [];
  const rpc = makeRpc(bag, { onEvent: (message) => { seen.push(message); } });
  const child = await startRpc(rpc, bag.children);
  child.respond({ type: "tool_execution_start", toolCallId: "m1", toolName: "bash", args: {} });
  child.respond({ type: "tool_execution_start", toolCallId: "m2", toolName: "bash",
    args: { command: "" } });
  child.respond({ type: "tool_execution_start", toolCallId: "m3", toolName: "bash",
    args: { command: "x".repeat(20000) } });
  child.respond({ type: "tool_execution_start", toolCallId: "m4", toolName: "bash" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(seen.length, 4);
  for (const entry of seen) {
    assert.equal(entry.input, null);
  }
  // Audit still carries bounded evidence (failure marker or truncated
  // preview) and never the raw oversized command verbatim beyond bounds.
  assert.equal(seen[0].auditInput.error, "malformed_input");
  assert.equal(seen[1].auditInput.error, "malformed_input");
  assert.equal(seen[2].auditInput.truncated, true);
  assert.ok(seen[2].auditInput.command.length <= 16384);
  assert.ok(seen[2].auditInput.command_sha256?.match(/^[0-9a-f]{64}$/));
  // None of the truncated/failed permission inputs can evaluate to allow.
  const { evaluateToolCall } = await import("../policy.mjs");
  const allowPolicy = {
    version: 3, write_tools_enabled: false,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [], protected_template_exceptions: [],
    allow_session_always: true, external_access: { default_mode: "deny", roots: [] },
    shell_mode: "allow",
  };
  for (const entry of seen) {
    const verdict = evaluateToolCall({ cwd: "/tmp", policy: allowPolicy, toolName: "bash", input: entry.input });
    assert.equal(verdict.effect, "deny");
    assert.equal(verdict.code, "malformed_input");
  }
  await rpc.close({ graceMs: 0 });
});
