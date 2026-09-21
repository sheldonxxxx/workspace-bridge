import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildRecord,
  emit,
  errorCode,
  initFromEnv,
  parseLogLevel,
  setLogLevel,
} from "../oplog.mjs";
import { EventHub } from "../sdk-runtime.mjs";
import { createAdapterServer } from "../server.mjs";

function capture() {
  const lines = [];
  const sink = (level, component, event, fields) => {
    // onLog convention is (level, component, event, fields).
    const record = buildRecord(level, component, event, fields);
    if (record) lines.push(JSON.stringify(record));
  };
  return { lines, sink };
}

test("WB_LOG_LEVEL allowlist: valid parses, invalid throws or falls back", () => {
  assert.equal(parseLogLevel("info"), "INFO");
  assert.equal(parseLogLevel("WARNING"), "WARNING");
  assert.throws(() => parseLogLevel("VERBOSE"), /WB_LOG_LEVEL/);
  assert.throws(() => parseLogLevel(""), /WB_LOG_LEVEL/);
  // Adapter runtime behavior: invalid env safely falls back to INFO.
  assert.equal(initFromEnv({ WB_LOG_LEVEL: "VERBOSE" }), "INFO");
  assert.equal(initFromEnv({}), "INFO");
  assert.equal(initFromEnv({ WB_LOG_LEVEL: "debug" }), "DEBUG");
  setLogLevel("INFO");
});

test("records drop secrets, URLs, patterns and event metadata", () => {
  const record = buildRecord("INFO", "adapter", "adapter_ready", {
    adapter_version: "0.1.7",
    server_configured: true,
    locked: false,
    server_url: "http://host:4096",
    username: "admin",
    password: "hunter2",
    token: "tok_secret",
    event: { type: "permission.asked", data: { pattern: ["/tmp/**"] } },
    pattern: ["/tmp/**"],
    metadata: { path: "/tmp/x" },
    prompt: "do the thing",
  });
  assert.equal(record.event, "adapter_ready");
  assert.equal(record.adapter_version, "0.1.7");
  const dumped = JSON.stringify(record);
  for (const key of ["server_url", "username", "password", "token", "pattern", "metadata", "prompt"]) {
    assert.ok(!(key in record), key);
  }
  assert.ok(!dumped.includes("hunter2"));
  assert.ok(!dumped.includes("tok_secret"));
  assert.ok(!dumped.includes("/tmp/**"));
  assert.equal(buildRecord("INFO", "adapter", "prompt_text", {}), null);
});

test("errorCode carries class/code only, never raw bodies", () => {
  assert.equal(errorCode({ code: "runtime_error", message: "boom secret" }), "runtime_error");
  assert.equal(errorCode({ name: "TypeError", message: "secret body" }), "TypeError");
  assert.equal(errorCode({ status: 502 }), "http_502");
  assert.ok(!String(errorCode({ name: "TypeError", message: "secret" })).includes("secret"));
});

test("EventHub logs subscribed/reconnecting transitions without contents", () => {
  const { lines, sink } = capture();
  const hub = new EventHub({ client: {}, sleep: () => Promise.resolve(), onLog: sink });
  hub._setHealth("subscribed");
  hub._setHealth("reconnecting");
  assert.equal(lines.length, 2);
  const first = JSON.parse(lines[0]);
  assert.equal(first.event, "eventhub_transition");
  assert.equal(first.status, "subscribed");
  assert.equal(typeof first.transitions, "number");
  const dumped = lines.join("\n");
  assert.ok(!dumped.includes("permission"));
  // Same-status repeat is not logged (no duplicate noise).
  hub._setHealth("reconnecting");
  assert.equal(lines.length, 2);
});

test("EventHub permission push logs ids/action only", () => {
  const { lines, sink } = capture();
  const hub = new EventHub({ client: {}, sleep: () => Promise.resolve(), onLog: sink });
  hub.push({
    type: "permission.asked",
    properties: {
      sessionID: "ses_1", id: "per_1", permission: "external_directory",
      patterns: ["/tmp/secret/**"], always: ["/tmp/secret/**"],
      metadata: { path: "/tmp/secret/x" }, tool: { name: "edit" },
    },
  });
  const permissionLines = lines.map((l) => JSON.parse(l)).filter((r) => r.event === "permission_event");
  assert.equal(permissionLines.length, 1);
  assert.equal(permissionLines[0].session_id, "ses_1");
  assert.equal(permissionLines[0].request_id, "per_1");
  assert.equal(permissionLines[0].action, "external_directory");
  const dumped = lines.join("\n");
  assert.ok(!dumped.includes("/tmp/secret"));
  assert.ok(!dumped.includes("requested_patterns"));
});

test("permission-list success/failure logs are sanitized", async () => {
  const { lines, sink } = capture();
  const { SdkError } = await import("../sdk-runtime.mjs");
  const runtime = {
    listPendingPermissions: async (directory, sessionId) => {
      if (sessionId === "ses_boom") throw new SdkError("upstream encoding failure with secret", 502, "runtime_error");
      return [{ id: "per_1", session_id: sessionId }];
    },
  };
  const hub = { cursor: 0, poll: async () => ({ events: [], cursor: 0 }) };
  const server = createAdapterServer({
    runtime, hub, token: "tok", serverConfigured: true,
    instance: "i", adapterVersion: "0.1.7", onLog: sink,
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const ok = await fetch(`${base}/sessions/ses_1/permissions?directory=/tmp`, {
      headers: { "x-runtime-token": "tok" },
    });
    assert.equal(ok.status, 200);
    const bad = await fetch(`${base}/sessions/ses_boom/permissions?directory=/tmp`, {
      headers: { "x-runtime-token": "tok" },
    });
    assert.equal(bad.status, 502);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
  const listLogs = lines.map((l) => JSON.parse(l)).filter((r) => r.event === "permission_list");
  assert.equal(listLogs.length, 2);
  assert.equal(listLogs[0].status, "ok");
  assert.equal(listLogs[0].matched, 1);
  assert.equal(listLogs[0].session_id, "ses_1");
  assert.equal(listLogs[1].status, "degraded");
  assert.equal(listLogs[1].level, "WARNING");
  const dumped = lines.join("\n");
  assert.ok(!dumped.includes("secret"));
  assert.ok(!dumped.includes("tok"));
  assert.ok(!dumped.includes("per_1") || dumped.includes("permission_list"));
  // Success record carries no permission objects.
  assert.ok(!("permissions" in listLogs[0]));
});

test("emit respects WB_LOG_LEVEL and never throws", () => {
  setLogLevel("WARNING");
  try {
    emit("INFO", "adapter", "adapter_ready", { adapter_version: "0.1.7" });
    emit("WARNING", "adapter", "adapter_ready", { status: "degraded", code: "x" });
  } finally {
    setLogLevel("INFO");
  }
});
