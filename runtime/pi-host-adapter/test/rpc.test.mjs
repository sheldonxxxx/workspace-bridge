import assert from "node:assert/strict";
import { test } from "node:test";

import { PiRpcProcess, RpcError } from "../rpc.mjs";
import { createFakeSpawn, respondState } from "./helpers.mjs";

const AGENT_DIR = "/tmp/fake-agent-dir";

function newRpc(spawnBag, opts = {}) {
  return new PiRpcProcess({
    binary: "pi", cwd: "/tmp/ws", agentDir: AGENT_DIR, spawnFn: spawnBag.spawnFn,
    timeoutMs: 500, ...opts,
  });
}

test("spawn uses direct argv, validated cwd, and absolute agent-dir env", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  assert.equal(child.binary, "pi");
  assert.deepEqual(child.args, ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
  assert.equal(child.opts.cwd, "/tmp/ws");
  assert.equal(child.opts.stdio.join(","), "pipe,pipe,pipe");
  assert.equal(child.opts.env.PI_CODING_AGENT_DIR, AGENT_DIR);
  assert.ok(child.opts.env.PATH !== undefined);
  const req = child.lastRequest();
  assert.equal(req.type, "get_state");
  respondState(child, req.id, { sessionId: "ses-spawn-1" });
  await started;
  assert.equal(rpc.sessionId, "ses-spawn-1");
  assert.equal(rpc.spawnArgs[0], "pi");
});

test("get_state binding fails closed without sessionId", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  const req = child.lastRequest();
  child.respond({ id: req.id, type: "response", command: "get_state", success: true, data: {} });
  await assert.rejects(started, RpcError);
  assert.equal(rpc.dead, true);
});

test("split and coalesced JSONL frames correlate correctly", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const first = rpc.command("abort");
  const second = rpc.command("get_messages");
  const idA = child.requests().at(-2).id;
  const idB = child.requests().at(-1).id;
  // Coalesced: two responses in one chunk; split: response across chunks.
  const lineA = JSON.stringify({ id: idA, type: "response", command: "abort", success: true });
  const lineB = JSON.stringify({ id: idB, type: "response", command: "get_messages", success: true, data: { messages: [] } });
  child.respondRaw(Buffer.from(`${lineA}\n${lineB.slice(0, 20)}`));
  child.respondRaw(Buffer.from(`${lineB.slice(20)}\n`));
  await first;
  const data = await second;
  assert.deepEqual(data, { messages: [] });
});

test("split multi-byte UTF-8 sequence decodes correctly", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const pending = rpc.command("get_messages");
  const req = child.lastRequest();
  const payload = JSON.stringify({
    id: req.id, type: "response", command: "get_messages", success: true,
    data: { messages: [{ role: "assistant", content: "日本語テスト🎨絵文字" }] },
  }) + "\n";
  const raw = Buffer.from(payload, "utf8");
  // Split inside the 4-byte emoji sequence at the raw byte boundary.
  const emojiIndex = raw.indexOf(Buffer.from("🎨", "utf8"));
  assert.ok(emojiIndex > 0);
  const split = emojiIndex + 2;
  child.respondRaw(raw.subarray(0, split));
  child.respondRaw(raw.subarray(split));
  const data = await pending;
  assert.equal(data.messages[0].content, "日本語テスト🎨絵文字");
  assert.equal(rpc.dead, false);
});

test("one chunk with multiple sub-limit frames succeeds despite aggregate size", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag, { maxLineBytes: 220 });
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const first = rpc.command("abort");
  const second = rpc.command("get_messages");
  const idA = child.requests().at(-2).id;
  const idB = child.requests().at(-1).id;
  const pad = "y".repeat(60);
  const lineA = JSON.stringify({ id: idA, type: "response", command: "abort", success: true, pad });
  const lineB = JSON.stringify({ id: idB, type: "response", command: "get_messages", success: true, data: { messages: [] }, pad });
  assert.ok(Buffer.byteLength(lineA) < 220 && Buffer.byteLength(lineB) < 220);
  const combined = Buffer.from(`${lineA}\n${lineB}\n`);
  assert.ok(combined.length > 220);
  // A single stdout chunk carrying both frames must not trip the per-frame limit.
  child.respondRaw(combined);
  await first;
  assert.deepEqual(await second, { messages: [] });
  assert.equal(rpc.dead, false);
});

test("command mismatch with correct id is a protocol failure", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const pending = rpc.command("abort");
  const req = child.lastRequest();
  child.respond({ id: req.id, type: "response", command: "get_state", success: true, data: {} });
  await assert.rejects(pending, /unavailable/);
  assert.equal(rpc.dead, true);
  assert.equal(rpc.exitInfo.reason, "protocol");
});

test("incomplete trailing frame on EOF fails closed", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  child.respondRaw(Buffer.from('{"id":"x","type":"response"'));
  child.eof();
  assert.equal(rpc.dead, true);
  assert.equal(rpc.exitInfo.reason, "protocol");
});

test("malformed line marks session dead and rejects pending", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const pending = rpc.command("abort");
  child.respondRaw(Buffer.from("this is not json\n"));
  await assert.rejects(pending, /unavailable/);
  assert.equal(rpc.dead, true);
  await assert.rejects(rpc.command("abort"), /unavailable/);
});

test("oversized line marks session dead", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag, { maxLineBytes: 256 });
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const pending = rpc.command("abort");
  child.respondRaw(Buffer.from("x".repeat(512) + "\n"));
  await assert.rejects(pending, /unavailable/);
  assert.equal(rpc.dead, true);
});

test("uncorrelated response is a protocol failure", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  child.respond({ id: "no-such-id", type: "response", command: "abort", success: true });
  assert.equal(rpc.dead, true);
});

test("command timeout rejects without killing the session child", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag, { timeoutMs: 50 });
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  await assert.rejects(rpc.command("abort"), /timed out/);
  assert.deepEqual(child.killedSignals, []);
  assert.equal(rpc.dead, false);
});

test("command failure surfaces bounded pi error", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  const pending = rpc.command("prompt", { message: "hi" });
  const req = child.lastRequest();
  child.respond({ id: req.id, type: "response", command: "prompt", success: false, error: "No API key found for the selected model." });
  await assert.rejects(pending, /No API key/);
});

test("child exit marks session dead and never respawns", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  let exited = null;
  rpc.onExit = (info) => { exited = info; };
  child.die(1);
  assert.equal(rpc.dead, true);
  assert.equal(exited.reason, "exit");
  assert.equal(bag.children.length, 1);
  await assert.rejects(rpc.command("get_state"), /unavailable/);
  assert.equal(bag.children.length, 1);
});

test("stdout EOF marks session dead", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  respondState(child, child.lastRequest().id);
  await started;
  child.eof();
  assert.equal(rpc.dead, true);
});

test("stderr is bounded and never leaks into errors", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  child.stderr.emit("data", Buffer.from("secret-token-abc ".repeat(2000)));
  respondState(child, child.lastRequest().id);
  await started;
  assert.ok(rpc.stderrBytes <= 8192);
  const pending = rpc.command("prompt", { message: "hi" });
  const req = child.lastRequest();
  child.respond({ id: req.id, type: "response", command: "prompt", success: false, error: "boom" });
  try {
    await pending;
    assert.fail("should reject");
  } catch (error) {
    assert.ok(!String(error.message).includes("secret-token-abc"));
  }
});

test("close gives bounded grace then kills only the owned child", async () => {
  const bag = createFakeSpawn();
  const rpc = newRpc(bag);
  const started = rpc.start();
  const child = bag.children[0];
  child.autoExitCode = 0;
  respondState(child, child.lastRequest().id);
  await started;
  await rpc.close({ graceMs: 1000 });
  assert.deepEqual(child.killedSignals, ["SIGTERM"]);
});
