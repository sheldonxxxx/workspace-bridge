import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { PiRuntimeProtocol } from "../wbrp.mjs";
import { safeDefaultPolicy } from "../policy.mjs";
import { durableFixture, boundToolHook, flush } from "./durable-fixture.mjs";
import { securityBlock } from "./security-block.mjs";

const [root, boundary] = process.argv.slice(2);
const { adapter, transport, directory, logs } = durableFixture(root);
const protocol = new PiRuntimeProtocol(adapter);
const ask = ["waiting_approval", "after_approval", "failed_approval"].includes(boundary);
const security = securityBlock({ ...safeDefaultPolicy(), write_inside: ask ? "ask" : "allow" },
  "durable-test");
const conversation = await protocol.createConversation({ workspaceId: "workspace-test", directory,
  security });
const input = [{ type: "text", text: "Run one test write" }];
const clientRunId = "one-test-run";
const run = await protocol.startRun(conversation.id, { input, clientRunId, security });
const session = transport.lastSession();
const entry = adapter.sessions.get(session.sessionId);
const hook = boundToolHook(transport);
const call = { toolCallId: "write-once", toolName: "write",
  input: { path: "notes.txt", content: "PRIVATE-WRITE-CONTENT" } };
const metadata = { conversationId: conversation.id, runId: run.id,
  sessionId: session.sessionId, input, clientRunId, boundary };
const failCommit = () => { throw new Error("storage password=NEVER-EXPOSED at /private/raw-secret-path"); };
if (boundary === "failed_intent") entry.journal.onCommit = failCommit;
session.emit({ type: "tool_execution_start", toolCallId: call.toolCallId,
  toolName: call.toolName, args: call.input });
const pendingCall = hook(call);
if (ask) {
  await flush();
  const pending = await protocol.interactions(run.id);
  assert.equal(pending.interactions.length, 1);
  metadata.interactionId = pending.interactions[0].id;
  if (boundary !== "waiting_approval") {
    if (boundary === "failed_approval") entry.journal.onCommit = failCommit;
    if (boundary === "failed_approval") {
      await assert.rejects(() => protocol.resolve(metadata.interactionId, { choiceId: "always" }),
        { code: "execution_audit_unavailable" });
      assert.equal((await pendingCall).block, true);
    } else {
      await protocol.resolve(metadata.interactionId, { choiceId: "always" });
      assert.equal(await pendingCall, undefined);
    }
  }
} else {
  const outcome = await pendingCall;
  if (boundary === "failed_intent") {
    assert.equal(outcome.block, true);
    metadata.blocked = true;
    await assert.rejects(() => adapter.promptAsync(directory, session.sessionId, "must not run"),
      { code: "execution_audit_unavailable" });
    assert.equal(session.promptCalls.length, 1);
  } else {
    assert.equal(outcome, undefined);
  }
}
if (["after_effect", "after_completion", "failed_completion"].includes(boundary)) {
  fs.appendFileSync(path.join(root, "effects.log"), "one-effect\n");
  if (boundary !== "after_effect") {
    if (boundary === "failed_completion") entry.journal.onCommit = failCommit;
    session.emit({ type: "tool_execution_end", toolCallId: call.toolCallId,
      toolName: call.toolName, result: { status: "written", bytes: 21 }, isError: false });
    if (boundary === "failed_completion") {
      await assert.rejects(() => adapter.readExecutions(directory, session.sessionId),
        { code: "execution_audit_unavailable" });
    } else {
      assert.equal(entry.journal.records.get(call.toolCallId).state, "completed");
    }
  }
}
// No run refresh has copied these tool records into WBRP snapshots.
assert.equal(JSON.parse(fs.readFileSync(protocol.stateFile, "utf8")).activities.length, 0);
assert.ok(!JSON.stringify(logs).includes("NEVER-EXPOSED"));
metadata.logs = logs;
fs.writeFileSync(path.join(root, "crash-metadata.json"), JSON.stringify(metadata));
process.kill(process.pid, "SIGKILL");
