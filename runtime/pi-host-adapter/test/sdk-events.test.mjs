// In-process SDK event normalization: bounded permission metadata plus
// separate audit evidence, oversized-payload safety, and malformed-input
// fail-closed behavior. Pure unit tests over sdk-events.mjs (no
// transport); the adapter feeds raw session.subscribe() events through
// these helpers.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isManagedTool,
  normalizeToolEnd,
  normalizeToolStart,
  normalizeToolUpdate,
} from "../sdk-events.mjs";

test("managed tool classification covers built-ins only", () => {
  for (const name of ["read", "grep", "find", "ls", "edit", "write", "bash"]) {
    assert.equal(isManagedTool(name), true);
  }
  assert.equal(isManagedTool("alpha-tool"), false);
  assert.equal(isManagedTool(""), false);
});

test("tool start events are normalized to path-only metadata plus separate audit", () => {
  const secret = "sk-secret-" + "x".repeat(5000);
  const seen = [
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "c1", toolName: "write",
      args: { path: "notes.txt", content: secret } }),
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "c2", toolName: "grep",
      args: { pattern: "benign-search", path: "sub" } }),
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "c3", toolName: "bash",
      args: { command: "rm -rf /" } }),
    normalizeToolEnd({ type: "tool_execution_end", toolCallId: "c1", toolName: "write",
      result: { content: secret } }),
  ];
  assert.equal(seen.length, 4);
  assert.ok(seen.every(Boolean));
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
});

test("bash permission input keeps only bounded exact command+timeout", async () => {
  // Valid bash with seconds timeout plus unrelated raw fields that must
  // never enter permission metadata.
  const first = normalizeToolStart({ type: "tool_execution_start", toolCallId: "b1",
    toolName: "bash",
    args: { command: "echo hi", timeout: 20, env: { SECRET: "x" }, shell: "bash", extra: "drop" } });
  // Alias + ms-timeout canonicalization uses the same single parser.
  const second = normalizeToolStart({ type: "tool_execution_start", toolCallId: "b2",
    toolName: "bash", args: { cmd: "echo hi", timeoutMs: 20000 } });
  // Exact authority fields only: no env, no arbitrary args, no audit keys.
  assert.deepEqual(first.input, { command: "echo hi", timeoutMs: 20000 });
  assert.deepEqual(Object.keys(first.input).sort(), ["command", "timeoutMs"]);
  assert.deepEqual(second.input, { command: "echo hi", timeoutMs: 20000 });
  assert.ok(!JSON.stringify([first.input, second.input]).includes("SECRET"));
  assert.ok(!JSON.stringify([first.input, second.input]).includes("extra"));
  assert.ok(!JSON.stringify([first.input, second.input]).includes("command_sha256"));
  // Evaluator computes the same hash/timeout from the normalized form as
  // from the original Pi args.
  const { evaluateToolCall, extractBashCommand } = await import("../policy.mjs");
  const fromOrig = extractBashCommand({ command: "echo hi", timeout: 20 });
  const fromNorm = extractBashCommand(first.input);
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
    input: first.input });
  assert.equal(vNorm.effect, "allow");
  assert.equal(vNorm.effect, vOrig.effect);
  assert.equal(vNorm.commandHash, vOrig.commandHash);
  assert.equal(vNorm.timeoutMs, vOrig.timeoutMs);
});

test("malformed and truncated bash inputs fail closed in permission metadata", async () => {
  const seen = [
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "m1", toolName: "bash", args: {} }),
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "m2", toolName: "bash",
      args: { command: "" } }),
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "m3", toolName: "bash",
      args: { command: "x".repeat(20000) } }),
    normalizeToolStart({ type: "tool_execution_start", toolCallId: "m4", toolName: "bash" }),
  ];
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
});

test("update normalization keeps bounded previews without raw payloads", () => {
  const update = normalizeToolUpdate({ type: "tool_execution_update", toolCallId: "u1",
    toolName: "bash", args: { command: "echo hi" },
    partialResult: { content: [{ type: "text", text: "partial output" }] } });
  assert.ok(update);
  assert.equal(update.toolCallId, "u1");
  assert.ok(update.auditUpdate);
  const secret = "token-" + "s".repeat(9000);
  const ext = normalizeToolUpdate({ type: "tool_execution_update", toolCallId: "u2",
    toolName: "alpha-tool", args: { query: "q" },
    partialResult: { content: [{ type: "text", text: secret }] } });
  assert.ok(ext);
  assert.ok(!JSON.stringify(ext.auditUpdate).includes(secret.slice(0, 60)) || ext.auditUpdate.truncated === true);
  assert.equal(normalizeToolUpdate(null), null);
  assert.equal(normalizeToolUpdate({ type: "tool_execution_update", toolCallId: "x".repeat(201) }), null);
});

test("oversized tool results summarize to bounded evidence, never raw", () => {
  // A 2 MiB tool result (twice the old RPC line ceiling) normalizes to
  // bounded evidence instead of killing anything.
  const big = "y".repeat(2 * 1024 * 1024);
  const end = normalizeToolEnd({ type: "tool_execution_end", toolCallId: "big-1",
    toolName: "bash", result: { content: [{ type: "text", text: big }] }, isError: false });
  assert.ok(end);
  assert.ok(JSON.stringify(end.auditResult).length < 100 * 1024);
  assert.equal(end.auditResult.truncated, true);
  assert.equal(normalizeToolEnd(null), null);
  assert.equal(normalizeToolStart({ type: "tool_execution_start", toolCallId: "", toolName: "read" }), null);
});
