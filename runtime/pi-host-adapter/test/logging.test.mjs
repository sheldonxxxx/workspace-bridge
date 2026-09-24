import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ALLOWED_LEVELS,
  DEFAULT_LOG_LEVEL,
  buildRecord,
  createLogger,
  logLevelFromEnv,
  parseLogLevel,
  sanitizedErrorCode,
  shouldLog,
} from "../logging.mjs";
import { loadConfig, parseLogLevel as parseConfigLevel } from "../config.mjs";
import { buildMain } from "../main.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function capturingLogger(level) {
  const stdout = [];
  const stderr = [];
  const log = createLogger({
    level,
    writeStdout: (line) => stdout.push(line),
    writeStderr: (line) => stderr.push(line),
  });
  return { log, stdout, stderr };
}

test("log level parsing supports four levels with INFO default", () => {
  assert.deepEqual(ALLOWED_LEVELS, ["DEBUG", "INFO", "WARNING", "ERROR"]);
  assert.equal(DEFAULT_LOG_LEVEL, "INFO");
  assert.equal(logLevelFromEnv({}), "INFO");
  assert.equal(logLevelFromEnv({ WB_LOG_LEVEL: "" }), "INFO");
  assert.equal(logLevelFromEnv({ WB_LOG_LEVEL: "   " }), "INFO");
  assert.equal(logLevelFromEnv({ WB_LOG_LEVEL: "debug" }), "DEBUG");
  assert.equal(parseLogLevel("WARNING"), "WARNING");
  assert.equal(parseConfigLevel("error"), "ERROR");
  assert.throws(() => parseLogLevel("VERBOSE"), /WB_LOG_LEVEL must be one of/);
  assert.throws(() => parseConfigLevel("nope"), /WB_LOG_LEVEL must be one of/);
  // Invalid messages never echo the raw value.
  try {
    parseLogLevel("sk-secret-VALUE");
    assert.fail("expected throw");
  } catch (error) {
    assert.ok(!String(error.message).includes("sk-secret-VALUE"));
  }
});

test("DEBUG suppressed at INFO but emitted at DEBUG; WARNING/ERROR retained", () => {
  const atInfo = capturingLogger("INFO");
  atInfo.log("DEBUG", "pi-adapter", "sdk_tool_event_trace", { stage: "adapter_received" });
  atInfo.log("INFO", "pi-adapter", "pi_adapter_ready", { adapter_version: "0.4.0" });
  atInfo.log("WARNING", "pi-adapter", "request_rejected", { code: "rejected" });
  atInfo.log("ERROR", "pi-adapter", "agent_dir_rejected", {});
  assert.equal(atInfo.stdout.length, 1);
  assert.equal(atInfo.stderr.length, 2);

  const atDebug = capturingLogger("DEBUG");
  atDebug.log("DEBUG", "pi-adapter", "sdk_tool_event_trace", { stage: "adapter_received" });
  assert.equal(atDebug.stdout.length, 1);
  const parsed = JSON.parse(atDebug.stdout[0]);
  assert.equal(parsed.level, "DEBUG");
  assert.equal(parsed.event, "sdk_tool_event_trace");
});

test("records use timestamp key with allowlisted bounded scalars only", () => {
  const { log, stdout, stderr } = capturingLogger("DEBUG");
  log("INFO", "pi-adapter", "session_create", {
    status: "ok",
    session_id: "ses_123",
    directory: "/tmp/secret-scratch",
    title: "review everything",
    model: "provider/model",
    token: "tok_secret_XYZ",
    nested: { a: 1 },
    list: [1, 2],
  });
  assert.equal(stdout.length, 1);
  assert.equal(stderr.length, 0);
  const parsed = JSON.parse(stdout[0]);
  assert.ok(parsed.timestamp);
  assert.ok(!("ts" in parsed));
  assert.equal(parsed.level, "INFO");
  assert.equal(parsed.component, "pi-adapter");
  assert.equal(parsed.event, "session_create");
  assert.equal(parsed.session_id, "ses_123");
  const dumped = JSON.stringify(parsed);
  assert.ok(!dumped.includes("/tmp/secret-scratch"));
  assert.ok(!dumped.includes("tok_secret_XYZ"));
  assert.ok(!("directory" in parsed));
  assert.ok(!("title" in parsed));
  assert.ok(!("token" in parsed));
  assert.ok(!("nested" in parsed));
  // Unknown events are dropped, never emitted.
  log("INFO", "pi-adapter", "prompt_text", { status: "ok" });
  assert.equal(stdout.length, 1);
  // One record per line.
  assert.ok(!stdout[0].includes("\n"));
});

test("long strings are bounded and logging failures never throw", () => {
  const record = buildRecord("pi-adapter", "session_create", "INFO", {
    status: "ok",
    session_id: "x".repeat(5000),
  });
  assert.ok(record.session_id.length <= 200);
  const log = createLogger({
    level: "INFO",
    writeStdout: () => { throw new Error("stdout blew up"); },
    writeStderr: () => { throw new Error("stderr blew up"); },
  });
  log("INFO", "pi-adapter", "pi_adapter_ready", { adapter_version: "0.4.0" });
  log("ERROR", "pi-adapter", "agent_dir_rejected", {});
});

test("routine SDK trace stages are DEBUG, anomalies are WARNING", async () => {
  const { PiAdapter } = await import("../adapter.mjs");
  const seen = [];
  const adapter = new PiAdapter({
    projectsRoot: "/tmp",
    agentDir: "/tmp/agent",
    onLog: (level, component, event, fields) => seen.push({ level, component, event, fields }),
  });
  const stages = [
    "extension_dispatch_start", "extension_dispatch_end", "session_subscriber",
    "adapter_received", "journal_started", "journal_completed",
  ];
  for (const stage of stages) {
    adapter._sdkDiagnostic({ stage, sessionId: "ses_1", eventType: "tool_execution_start" });
  }
  assert.ok(seen.length === stages.length);
  assert.ok(seen.every((r) => r.level === "DEBUG" && r.event === "sdk_tool_event_trace"));
  seen.length = 0;
  for (const stage of ["extension_dispatch_stalled", "adapter_event_error", "journal_missing_start"]) {
    adapter._sdkDiagnostic({ stage, sessionId: "ses_1", eventType: "tool_execution_end" });
  }
  assert.ok(seen.length === 3);
  assert.ok(seen.every((r) => r.level === "WARNING"));
  // No routine trace at default INFO through the real logger path.
  const captured = capturingLogger("INFO");
  const quiet = new PiAdapter({
    projectsRoot: "/tmp",
    agentDir: "/tmp/agent",
    onLog: captured.log,
  });
  quiet._sdkDiagnostic({ stage: "adapter_received", sessionId: "ses_1" });
  quiet._sdkDiagnostic({ stage: "extension_dispatch_stalled", sessionId: "ses_1" });
  assert.equal(captured.stdout.length, 0);
  assert.equal(captured.stderr.length, 1);
});

test("config stores log level and main filters through it", () => {
  assert.equal(loadConfig({}, "/home/tester").logLevel, "INFO");
  assert.equal(loadConfig({ WB_LOG_LEVEL: "debug" }, "/home/tester").logLevel, "DEBUG");
  assert.throws(() => loadConfig({ WB_LOG_LEVEL: "VERBOSE" }, "/home/tester"),
    /WB_LOG_LEVEL must be one of/);
  const built = buildMain({ WB_PI_PROJECTS_DIR: "", WB_LOG_LEVEL: "WARNING" });
  assert.equal(built.config.logLevel, "WARNING");
  assert.equal(built.log.level, "WARNING");
});

test("invalid adapter WB_LOG_LEVEL fails startup safely", () => {
  assert.throws(() => buildMain({ WB_LOG_LEVEL: "sk-secret-VALUE" }), /WB_LOG_LEVEL must be one of/);
  try {
    buildMain({ WB_LOG_LEVEL: "sk-secret-VALUE" });
    assert.fail("expected throw");
  } catch (error) {
    assert.ok(!String(error.message).includes("sk-secret-VALUE"));
    assert.ok(!String(error.message).includes("/tmp"));
  }
});

test("sanitizedErrorCode derives type names only, never messages", () => {
  assert.equal(sanitizedErrorCode(new Error("raw body tok_secret_XYZ /tmp/x")), "Error");
  assert.equal(sanitizedErrorCode(new TypeError("bad")), "TypeError");
  assert.equal(sanitizedErrorCode(null), "error");
  assert.ok(!sanitizedErrorCode(new Error("secret")).includes("secret"));
  const record = buildRecord("pi-adapter", "request_error", "ERROR", { code: "Error" });
  assert.ok(record);
  assert.equal(record.event, "request_error");
  assert.equal(record.level, "ERROR");
});

test("launchd template sets WB_LOG_LEVEL=INFO", () => {
  const text = fs.readFileSync(
    path.join(HERE, "..", "launchd", "com.workspace-bridge.pi-host-adapter.plist"), "utf8");
  assert.ok(text.includes("<key>WB_LOG_LEVEL</key>"));
  assert.ok(text.includes("<string>INFO</string>"));
});

test("fatal server error emits ERROR process_error and exits nonzero", async () => {
  const http = await import("node:http");
  const { attachServerErrorHandler, handleFatalServerError } = await import("../main.mjs");
  const secret = `tok_secret_${"Y".repeat(40)} 127.0.0.1:9999 /tmp/secret-scratch-ZZZ`;
  // Direct helper: injectable exit keeps the runner alive.
  {
    const seen = [];
    let exitCode = null;
    handleFatalServerError(
      (level, component, event, fields) => seen.push({ level, component, event, fields }),
      new TypeError(`boom raw ${secret}`),
      (code) => { exitCode = code; },
    );
    assert.equal(seen.length, 1);
    assert.equal(seen[0].level, "ERROR");
    assert.equal(seen[0].event, "process_error");
    assert.equal(seen[0].fields.code, "TypeError");
    assert.equal(seen[0].fields.source, "http_server");
    assert.equal(exitCode, 1);
    const dumped = JSON.stringify(seen);
    assert.ok(!dumped.includes("boom raw"));
    assert.ok(!dumped.includes(secret.slice(0, 20)));
    assert.ok(!dumped.includes("/tmp/secret-scratch-ZZZ"));
    assert.ok(!dumped.includes("127.0.0.1:9999"));
  }
  // Wired listener on a real HTTP server: emitted 'error' is not swallowed.
  {
    const seen = [];
    let exitCode = null;
    const server = http.createServer(() => {});
    attachServerErrorHandler(
      server,
      (level, component, event, fields) => seen.push({ level, component, event, fields }),
      (code) => { exitCode = code; },
    );
    server.emit("error", new Error(`listen boom ${secret}`));
    assert.equal(seen.length, 1);
    assert.equal(seen[0].level, "ERROR");
    assert.equal(seen[0].event, "process_error");
    assert.equal(seen[0].fields.code, "Error");
    assert.equal(exitCode, 1);
    assert.ok(!JSON.stringify(seen).includes("listen boom"));
  }
});
