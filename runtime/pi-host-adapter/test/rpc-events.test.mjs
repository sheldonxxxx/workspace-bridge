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

test("tool start events are normalized to path-only metadata", async () => {
  const bag = createFakeSpawn();
  const seen = [];
  const rpc = makeRpc(bag, { onEvent: (message) => { seen.push(message); } });
  const child = await startRpc(rpc, bag.children);
  const secret = "sk-secret-" + "x".repeat(5000);
  child.respond({ type: "tool_execution_start", toolCallId: "c1", toolName: "write",
    args: { path: "notes.txt", content: secret } });
  child.respond({ type: "tool_execution_start", toolCallId: "c2", toolName: "grep",
    args: { pattern: secret, path: "sub" } });
  child.respond({ type: "tool_execution_start", toolCallId: "c3", toolName: "bash",
    args: { command: "rm -rf /" } });
  child.respond({ type: "tool_execution_end", toolCallId: "c1", toolName: "write",
    result: { content: secret } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(seen.length, 4);
  assert.deepEqual(seen[0],
    { type: "tool_execution_start", toolCallId: "c1", toolName: "write", input: { path: "notes.txt" } });
  assert.deepEqual(seen[1],
    { type: "tool_execution_start", toolCallId: "c2", toolName: "grep", input: { path: "sub" } });
  assert.deepEqual(seen[2],
    { type: "tool_execution_start", toolCallId: "c3", toolName: "bash", input: {} });
  assert.deepEqual(seen[3], { type: "tool_execution_end", toolCallId: "c1" });
  assert.ok(!JSON.stringify(seen).includes("sk-secret"));
  await rpc.close({ graceMs: 0 });
});
