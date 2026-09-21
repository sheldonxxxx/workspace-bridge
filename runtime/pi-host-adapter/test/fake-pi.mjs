#!/usr/bin/env node
// Fake `pi --mode rpc` child for integration tests. Speaks the verified
// protocol shape: commands arrive as `{type, id, ...}` JSON lines; responses
// correlate by id as `{type:"response", id, command, success, data|error}`.
//
// Records its argv/cwd/selected env to $FAKE_PI_RECORD for spawn assertions.
// Session id is fixed via $FAKE_PI_SESSION_ID (default "fake-session-1").
import fs from "node:fs";
import readline from "node:readline";

const recordPath = process.env.FAKE_PI_RECORD || "";
try {
  if (recordPath) {
    fs.writeFileSync(recordPath, JSON.stringify({
      argv: process.argv.slice(2),
      cwd: process.cwd(),
      PI_CODING_AGENT_DIR: process.env.PI_CODING_AGENT_DIR || "",
      pathPresent: Boolean(process.env.PATH),
    }));
  }
} catch {
  // Recording must never break the fake protocol.
}

const SESSION_ID = process.env.FAKE_PI_SESSION_ID || "fake-session-1";
const MODELS = [
  { provider: "fake-provider", id: "fake-model", name: "Fake Model", api: "fake" },
];
const storedMessages = [];
// 3B1 UI-ask simulation (no provider): when FAKE_PI_UI_ASK=1, one prompt
// emits tool_execution_start -> extension_ui_request(select with the exact
// trusted marker/options) and waits for the matching extension_ui_response
// on stdin before emitting tool_execution_end and the prompt response.
// The received UI response is recorded to $FAKE_PI_UI_RECORD for assertions.
const UI_ASK = process.env.FAKE_PI_UI_ASK === "1";
const UI_RECORD = process.env.FAKE_PI_UI_RECORD || "";
let uiPending = null;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { type, id } = msg;
  if (type === "extension_ui_response" && uiPending && msg.id === uiPending.uiId) {
    try {
      if (UI_RECORD) {
        fs.writeFileSync(UI_RECORD, JSON.stringify(msg));
      }
    } catch {
      // Recording must never break the fake protocol.
    }
    const done = uiPending;
    uiPending = null;
    send({ type: "tool_execution_end", toolCallId: done.toolCallId, toolName: done.toolName,
      result: { ok: true }, isError: false });
    send({ id: done.promptId, type: "response", command: "prompt", success: true });
    return;
  }
  switch (type) {
    case "get_state":
      send({ id, type: "response", command: "get_state", success: true, data: {
        sessionId: SESSION_ID,
        sessionFile: "/tmp/fake-session.jsonl",
        isStreaming: false,
        messageCount: storedMessages.length,
        pendingMessageCount: 0,
      } });
      break;
    case "prompt":
      if (typeof msg.message === "string" && msg.message) {
        storedMessages.push({ role: "user", content: msg.message, timestamp: 1758398400000 });
        storedMessages.push({
          role: "assistant",
          content: [
            { type: "thinking", thinking: "hidden reasoning must not surface" },
            { type: "text", text: `ack: ${msg.message.slice(0, 50)}` },
            { type: "toolCall", id: "call-1", name: "read", arguments: {} },
          ],
          timestamp: 1758398400001,
          stopReason: "stop",
        });
      }
      if (UI_ASK && !uiPending) {
        uiPending = { uiId: "ui-1", toolCallId: "call-ui-1", toolName: "edit", promptId: id };
        send({ type: "tool_execution_start", toolCallId: "call-ui-1", toolName: "edit",
          args: { path: "notes.txt" } });
        send({ type: "extension_ui_request", id: "ui-1", method: "select",
          title: "WB_PERMISSION_V1:call-ui-1",
          options: ["Allow once", "Always allow exact target this session", "Reject"] });
        break;
      }
      send({ id, type: "response", command: "prompt", success: true });
      break;
    case "get_messages":
      send({ id, type: "response", command: "get_messages", success: true, data: { messages: storedMessages } });
      break;
    case "get_available_models":
      send({ id, type: "response", command: "get_available_models", success: true, data: { models: MODELS } });
      break;
    case "set_model": {
      const ok = MODELS.some((m) => m.provider === msg.provider && m.id === msg.modelId);
      if (ok) send({ id, type: "response", command: "set_model", success: true, data: MODELS[0] });
      else send({ id, type: "response", command: "set_model", success: false, error: `Model not found: ${msg.provider}/${msg.modelId}` });
      break;
    }
    case "abort":
      send({ id, type: "response", command: "abort", success: true });
      break;
    default:
      send({ id, type: "response", command: String(type), success: false, error: `Unknown command: ${String(type)}` });
      break;
  }
});
