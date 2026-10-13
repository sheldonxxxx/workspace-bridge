// Persisted execution ledger support: bounded audit normalization + journal.
// Milestone 3C1.
//
// Separate audit normalization path from Pi tool_execution_start/update/end.
// Never retains raw event objects after normalization. Tool-specific
// evidence is bounded per the 3C1 contract; unknown tools fail safely
// rather than dumping arbitrary args. No thinking/reasoning/provider
// payloads, environment capture, or fullOutputPath are ever persisted.
//
// Bash command/output can contain sensitive data: list/summary surfaces
// must never include result output; only exact execution detail does.
// This module does not claim perfect secret detection.
import { createHash } from "node:crypto";

import { extractBashCommand } from "./policy.mjs";

export const MAX_COMMAND_CHARS = 16384;
export const MAX_OUTPUT_CHARS = 32768;
export const MAX_EDIT_PREVIEW_CHARS = 16384;
export const MAX_LIST_PREVIEW_CHARS = 8192;
export const MAX_TARGET_CHARS = 1024;
export const MAX_QUERY_CHARS = 2048;
export const MAX_JOURNAL_RECORDS = 500;
export const MAX_READ_LIMIT = 100;
// Generic third-party extension-tool audit bounds (3C2): input carries a
// canonical args hash + size + top-level keys plus only bounded safe
// selector fields; result carries a bounded ToolResult text preview (~8
// KiB). Never full args, fullOutputPath, env, auth, provider payloads,
// reasoning, cookies, tokens, or arbitrary nested objects.
export const MAX_EXTENSION_ARGS_BYTES = 16384;
export const MAX_EXTENSION_KEYS = 20;
export const MAX_EXTENSION_KEY_CHARS = 80;
export const MAX_EXTENSION_SELECTOR_CHARS = 500;
export const MAX_EXTENSION_PREVIEW_CHARS = 8192;
export const EXTENSION_SELECTOR_FIELDS = ["query", "url", "server", "tool", "name", "method", "path"];

// Conservative best-effort credential redaction for extension audit
// strings (3C2 corrective). Extension results/selectors can themselves
// print credentials (URLs with userinfo/query tokens, Bearer values,
// key=value pairs), so verbatim storage would persist secrets. This is
// structural hygiene plus pattern redaction, NOT perfect secret
// detection: detail views remain labeled potentially sensitive. Hash/size
// evidence always describes the ORIGINAL full args, never redacted text.
const SENSITIVE_PARAM_RE = /^(?:.*?(?:token|secret|passwd|password|auth|cookie|session|api[_-]?key|apikey|client[_-]?secret)|key|.*?[{_-]key)$/i;

export function redactExtensionText(value) {
  if (typeof value !== "string" || !value) return "";
  let out = value;
  // URL userinfo: scheme://user:pass@host -> scheme://[redacted]@host.
  out = out.replace(/([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)[^/\s?#@]+@/g, "$1[redacted]@");
  // Sensitive URL query/form params: ?token=abc&next=x -> ?token=[redacted]&next=x.
  out = out.replace(/([?&])([^?&#=\s;]+)=([^&#\s;]*)/g, (match, sep, name, val) => {
    if (!val) return match;
    return SENSITIVE_PARAM_RE.test(name) ? `${sep}${name}=[redacted]` : match;
  });
  // Bearer/Basic authorization forms.
  out = out.replace(/\bBearer\s+[A-Za-z0-9\-._~+/=]+/g, "Bearer [redacted]");
  out = out.replace(/\bBasic\s+[A-Za-z0-9+/=]+/g, "Basic [redacted]");
  // Obvious key=value / key: value credential patterns.
  out = out.replace(/\b(password|passwd|secret|token|session|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s"'{},;&#]+)/gi,
    "$1$2[redacted]");
  return out;
}

function sha256Hex(text) {
  return createHash("sha256").update(String(text ?? ""), "utf8").digest("hex");
}

function boundedStr(value, limit) {
  if (typeof value !== "string") return "";
  return value.slice(0, limit);
}

function byteLen(text) {
  return Buffer.byteLength(String(text ?? ""), "utf8");
}

function lineCount(text) {
  const s = String(text ?? "");
  if (!s) return 0;
  return s.split("\n").length;
}

// Summarize tool input into bounded evidence. Returns {ok, summary}.
// File audit never persists read contents or complete edit/write inputs:
// edit carries old/new byte counts + sha256; write carries byte/line
// counts + sha256; read carries target + range/options only.
export function summarizeInput(tool, args) {
  const input = args && typeof args === "object" && !Array.isArray(args) ? args : {};
  try {
    if (tool === "bash") {
      const extracted = extractBashCommand(input);
      if (!extracted.ok) return { ok: false, summary: { error: "malformed_input" } };
      return {
        ok: true,
        summary: {
          command: extracted.command,
          command_sha256: extracted.commandHash,
          command_bytes: byteLen(input.command ?? input.cmd ?? input.script ?? input.code ?? ""),
          timeout_ms: extracted.timeoutMs,
          truncated: Boolean(extracted.truncated),
        },
      };
    }
    if (tool === "read") {
      const target = boundedStr(input.path, MAX_TARGET_CHARS);
      if (!target) return { ok: false, summary: { error: "malformed_input" } };
      const summary = { target };
      if (input.range !== undefined) summary.range = boundedStr(String(input.range), 200);
      if (input.offset !== undefined) {
        const n = Number(input.offset);
        if (Number.isFinite(n)) summary.offset = Math.max(0, Math.floor(n));
      }
      if (input.limit !== undefined) {
        const n = Number(input.limit);
        if (Number.isFinite(n)) summary.limit = Math.max(0, Math.min(Math.floor(n), 10000));
      }
      if (input.encoding !== undefined) summary.encoding = boundedStr(String(input.encoding), 40);
      return { ok: true, summary };
    }
    if (tool === "grep" || tool === "find" || tool === "ls") {
      const target = boundedStr(input.path ?? ".", MAX_TARGET_CHARS);
      const summary = { target };
      if (input.pattern !== undefined) summary.query = boundedStr(String(input.pattern), MAX_QUERY_CHARS);
      if (input.query !== undefined) summary.query = boundedStr(String(input.query), MAX_QUERY_CHARS);
      if (input.glob !== undefined) summary.query = boundedStr(String(input.glob), MAX_QUERY_CHARS);
      if (input.options !== undefined) {
        summary.options = boundedStr(
          typeof input.options === "string" ? input.options : JSON.stringify(input.options).slice(0, 500),
          500);
      }
      if (input.limit !== undefined) {
        const n = Number(input.limit);
        if (Number.isFinite(n)) summary.limit = Math.max(0, Math.min(Math.floor(n), 10000));
      }
      if (input.recursive !== undefined) summary.recursive = Boolean(input.recursive);
      return { ok: true, summary };
    }
    if (tool === "edit") {
      const target = boundedStr(input.path, MAX_TARGET_CHARS);
      if (!target) return { ok: false, summary: { error: "malformed_input" } };
      const oldText = typeof input.oldText === "string" ? input.oldText
        : typeof input.old_string === "string" ? input.old_string
        : typeof input.old === "string" ? input.old : null;
      const newText = typeof input.newText === "string" ? input.newText
        : typeof input.new_string === "string" ? input.new_string
        : typeof input.new === "string" ? input.new : null;
      const summary = { target };
      if (oldText !== null) {
        summary.old_bytes = byteLen(oldText);
        summary.old_sha256 = sha256Hex(oldText);
      }
      if (newText !== null) {
        summary.new_bytes = byteLen(newText);
        summary.new_sha256 = sha256Hex(newText);
      }
      return { ok: true, summary };
    }
    if (tool === "write") {
      const target = boundedStr(input.path, MAX_TARGET_CHARS);
      if (!target) return { ok: false, summary: { error: "malformed_input" } };
      const content = typeof input.content === "string" ? input.content
        : typeof input.text === "string" ? input.text
        : typeof input.data === "string" ? input.data : "";
      return {
        ok: true,
        summary: {
          target,
          content_bytes: byteLen(content),
          content_lines: lineCount(content),
          content_sha256: sha256Hex(content),
        },
      };
    }
    // Unknown tools fail audit normalization safely.
    return { ok: false, summary: { error: "unknown_tool" } };
  } catch {
    return { ok: false, summary: { error: "malformed_input" } };
  }
}

// Bounded generic audit input for explicitly enabled third-party
// extension tools (3C2). Canonical args SHA-256 + byte size + top-level
// keys, plus only bounded safe selector fields when present as strings.
// Selector strings get best-effort credential redaction; the hash/size
// still describe the original full args. Never arbitrary full args or
// nested objects. Redaction is best-effort, not perfect detection.
export function summarizeExtensionInput(args) {
  try {
    const input = args && typeof args === "object" && !Array.isArray(args) ? args : {};
    let canonical = "";
    try {
      canonical = JSON.stringify(sortKeysDeep(input)).slice(0, MAX_EXTENSION_ARGS_BYTES);
    } catch {
      canonical = "";
    }
    const full = (() => {
      try {
        return JSON.stringify(sortKeysDeep(input));
      } catch {
        return "";
      }
    })();
    const keys = Object.keys(input).slice(0, MAX_EXTENSION_KEYS)
      .filter((k) => typeof k === "string").map((k) => k.slice(0, MAX_EXTENSION_KEY_CHARS));
    const summary = {
      args_sha256: sha256Hex(full),
      args_bytes: byteLen(full),
      top_keys: keys,
    };
    for (const field of EXTENSION_SELECTOR_FIELDS) {
      const value = input[field];
      if (typeof value === "string" && value) {
        summary[field] = redactExtensionText(value).slice(0, MAX_EXTENSION_SELECTOR_CHARS);
      }
    }
    return { ok: true, summary };
  } catch {
    return { ok: false, summary: { error: "malformed_input" } };
  }
}

// Bounded generic audit result for explicitly enabled third-party
// extension tools (3C2): verified Pi ToolResult text content preview
// bounded to ~8 KiB plus truncation/error, with best-effort credential
// redaction (a result body can itself print secrets). Never
// fullOutputPath, env, auth headers, provider payloads, reasoning,
// cookies, or tokens in structural form. Redaction is best-effort, not
// perfect detection: detail views stay labeled potentially sensitive.
export function summarizeExtensionResult(result, isError) {
  const out = { is_error: Boolean(isError) };
  try {
    const text = extractResultText(result);
    const truncated = text.length > MAX_EXTENSION_PREVIEW_CHARS;
    const redacted = redactExtensionText(text);
    if (redacted) out.preview = redacted.slice(0, MAX_EXTENSION_PREVIEW_CHARS);
    out.preview_bytes = byteLen(text);
    out.truncated = truncated;
    const details = extractResultDetails(result);
    if (detailsTruncated(details)) out.truncated = true;
    return out;
  } catch {
    return { is_error: Boolean(isError) };
  }
}

function sortKeysDeep(value) {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeysDeep(value[key]);
    return out;
  }
  return value;
}

// Verified against installed Pi 0.86.1 (bundle chunk-CMRUVXTE):
// - tool_execution_start: {toolCallId, toolName, args} (bash args =
//   {command, timeout?} with timeout in SECONDS, optional).
// - tool_execution_update: {toolCallId, toolName, args, partialResult}
//   where partialResult = {content:[{type:"text",text}], details?}.
// - tool_execution_end: {toolCallId, toolName, result, isError} where
//   result (ToolResult) = {content:[{type:"text",text}], details?}.
// - bash details on truncation: {truncation:{truncated:true,...},
//   fullOutputPath:"/tmp/..."}; success details is otherwise undefined.
// - bash failures (non-zero exit/timeout/abort) surface as thrown Errors
//   converted to isError:true content text; no positive exitCode/cancelled
//   boolean exists on the ToolResult itself.
// Only positively verified fields are retained below (content text,
// details.truncation, explicit numeric exit/status, explicit cancelled
// booleans where present). Synthetic top-level output/stdout/text/preview
// fallbacks are kept only for scripted fakes/tests. fullOutputPath,
// environment, provider payloads and reasoning are never retained.
// Unknown future shapes yield bounded status/is_error only.
function extractResultText(result) {
  if (typeof result === "string") return result;
  if (Array.isArray(result)) {
    return result.filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text).join("");
  }
  if (!result || typeof result !== "object") return "";
  const content = result.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text).join("");
  }
  return "";
}

function extractResultDetails(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return {};
  const details = result.details;
  if (details && typeof details === "object" && !Array.isArray(details)) return details;
  return {};
}

function detailsTruncated(details) {
  if (!details || typeof details !== "object") return false;
  if (details.truncated === true) return true;
  const nested = details.truncation;
  if (nested && typeof nested === "object" && nested.truncated === true) return true;
  return false;
}

// Summarize a tool result into bounded evidence. Bash carries a bounded
// final output preview (32 KiB) + isError + only positively verified
// exit/cancel/truncation fields; never fullOutputPath. Read carries
// status/count/truncation metadata only, no contents. Edit carries a
// bounded diff/result preview (<=16 KiB). Write carries status/result
// metadata. Grep/find/ls carry a bounded reviewable preview with
// truncation flag.
export function summarizeResult(tool, result, isError) {
  const out = { is_error: Boolean(isError) };
  try {
    const res = result && typeof result === "object" && !Array.isArray(result) ? result : {};
    const details = extractResultDetails(result);
    // exit/cancelled only when positively present as a finite number/true.
    const exitCandidate = (typeof res.exitCode === "number" && Number.isFinite(res.exitCode))
      ? res.exitCode
      : (typeof res.exit_code === "number" && Number.isFinite(res.exit_code))
        ? res.exit_code
        : (typeof details.exitCode === "number" && Number.isFinite(details.exitCode))
          ? details.exitCode : null;
    if (exitCandidate !== null) out.exit_code = Math.trunc(exitCandidate);
    if (res.cancelled === true || details.cancelled === true) out.cancelled = true;
    if (res.truncated === true || detailsTruncated(details) || detailsTruncated(res)) {
      out.truncated = true;
    }
    const verifiedText = extractResultText(result);
    if (tool === "bash") {
      const rawOutput = verifiedText
        || (typeof res.output === "string" ? res.output : "")
        || (typeof res.stdout === "string" ? res.stdout : "")
        || (typeof res.text === "string" ? res.text : "")
        || (typeof result === "string" ? result : "");
      const truncated = rawOutput.length > MAX_OUTPUT_CHARS;
      out.output_preview = rawOutput.slice(0, MAX_OUTPUT_CHARS);
      out.output_bytes = byteLen(rawOutput);
      out.truncated = Boolean(out.truncated || truncated);
      return out;
    }
    if (tool === "read") {
      if (typeof res.count === "number" && Number.isFinite(res.count)) out.count = Math.max(0, Math.floor(res.count));
      if (typeof res.bytes === "number" && Number.isFinite(res.bytes)) out.bytes = Math.max(0, Math.floor(res.bytes));
      if (typeof res.status === "string") out.status = boundedStr(res.status, 80);
      return out;
    }
    if (tool === "edit") {
      const preview = verifiedText
        || (typeof res.diff === "string" ? res.diff : "")
        || (typeof res.preview === "string" ? res.preview : "")
        || (typeof res.message === "string" ? res.message : "")
        || (typeof result === "string" ? result : "");
      const truncated = preview.length > MAX_EDIT_PREVIEW_CHARS;
      if (preview) out.preview = preview.slice(0, MAX_EDIT_PREVIEW_CHARS);
      if (typeof res.status === "string") out.status = boundedStr(res.status, 80);
      out.truncated = Boolean(out.truncated || truncated);
      return out;
    }
    if (tool === "write") {
      if (typeof res.status === "string") out.status = boundedStr(res.status, 80);
      if (typeof res.bytes === "number" && Number.isFinite(res.bytes)) out.bytes = Math.max(0, Math.floor(res.bytes));
      if (typeof res.message === "string") out.message = boundedStr(res.message, 500);
      return out;
    }
    if (tool === "grep" || tool === "find" || tool === "ls") {
      const preview = verifiedText
        || (typeof res.preview === "string" ? res.preview : "")
        || (typeof res.output === "string" ? res.output : "")
        || (typeof res.text === "string" ? res.text : "")
        || (typeof result === "string" ? result : "");
      const truncated = preview.length > MAX_LIST_PREVIEW_CHARS;
      if (preview) out.preview = preview.slice(0, MAX_LIST_PREVIEW_CHARS);
      if (typeof res.count === "number" && Number.isFinite(res.count)) out.count = Math.max(0, Math.floor(res.count));
      out.truncated = Boolean(out.truncated || truncated);
      return out;
    }
    if (typeof res.status === "string") out.status = boundedStr(res.status, 80);
    else if (typeof res.message === "string") out.message = boundedStr(res.message, 500);
    return out;
  } catch {
    return { is_error: Boolean(isError) };
  }
}

const INPUT_SUMMARY_KEYS = new Set([
  "command", "command_sha256", "command_bytes", "timeout_ms", "truncated",
  "target", "range", "offset", "limit", "encoding", "query", "options", "recursive",
  "old_bytes", "old_sha256", "new_bytes", "new_sha256", "content_bytes", "content_lines", "content_sha256",
  "args_sha256", "args_bytes", "top_keys", ...EXTENSION_SELECTOR_FIELDS,
  "error",
]);
const RESULT_SUMMARY_KEYS = new Set([
  "is_error", "exit_code", "cancelled", "truncated", "output_preview", "output_bytes",
  "count", "bytes", "status", "preview", "message", "preview_bytes",
]);
const INPUT_STRING_KEYS = new Set(["command", "command_sha256", "target", "range", "encoding", "query", "options", "old_sha256", "new_sha256", "content_sha256", "args_sha256", ...EXTENSION_SELECTOR_FIELDS, "error"]);
const INPUT_NUMBER_KEYS = new Set(["command_bytes", "timeout_ms", "offset", "limit", "old_bytes", "new_bytes", "content_bytes", "content_lines", "args_bytes"]);
const RESULT_STRING_KEYS = new Set(["output_preview", "status", "preview", "message"]);
const RESULT_NUMBER_KEYS = new Set(["exit_code", "output_bytes", "count", "bytes", "preview_bytes"]);

function cloneSummary(value, allowed, { input = false, tool = "" } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid execution summary");
  const out = {};
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) continue;
    const v = value[key];
    if ((input ? INPUT_STRING_KEYS : RESULT_STRING_KEYS).has(key) && typeof v === "string") {
      const max = input
        ? ({ command: MAX_COMMAND_CHARS, target: MAX_TARGET_CHARS, range: 200, encoding: 40,
          query: MAX_QUERY_CHARS, options: 500, error: 80, command_sha256: 64,
          old_sha256: 64, new_sha256: 64, content_sha256: 64, args_sha256: 64,
          ...Object.fromEntries(EXTENSION_SELECTOR_FIELDS.map((k) => [k, MAX_EXTENSION_SELECTOR_CHARS])) })[key] ?? 500
        : key === "output_preview" ? MAX_OUTPUT_CHARS
          : key === "preview" ? tool === "edit" ? MAX_EDIT_PREVIEW_CHARS
            : ["grep", "find", "ls"].includes(tool) ? MAX_LIST_PREVIEW_CHARS : MAX_EXTENSION_PREVIEW_CHARS
            : key === "message" ? 500 : key === "status" ? 80 : 500;
      out[key] = redactExtensionText(v).slice(0, max);
    } else if (["truncated", "recursive", "is_error", "cancelled"].includes(key) && typeof v === "boolean") out[key] = v;
    else if ((input ? INPUT_NUMBER_KEYS : RESULT_NUMBER_KEYS).has(key)
      && typeof v === "number" && Number.isSafeInteger(v)
      && (key === "exit_code" || v >= 0)) out[key] = v;
    else if (input && key === "top_keys" && Array.isArray(v)
      && v.every((x) => typeof x === "string")) {
      out[key] = v.slice(0, MAX_EXTENSION_KEYS).filter((x) => typeof x === "string")
        .map((x) => redactExtensionText(x).slice(0, MAX_EXTENSION_KEY_CHARS));
    } else {
      throw new Error("invalid execution summary field");
    }
  }
  return out;
}

function hasOnlyKeys(value, allowed) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).every((key) => allowed.has(key));
}

const EXECUTION_RECORD_FIELDS = new Set([
  "seq", "start_seq", "start_order", "update_seq", "tool_call_id", "tool", "state",
  "started_at", "ended_at", "duration_ms", "input_summary", "result_summary", "is_error",
  "permission_effect", "permission_decision", "truncated",
]);

export function sanitizeExecutionSummary(value, { input = false, tool = "" } = {}) {
  return cloneSummary(value ?? {}, input ? INPUT_SUMMARY_KEYS : RESULT_SUMMARY_KEYS, { input, tool });
}

export function normalizeExecutionRecord(record) {
  if (!hasOnlyKeys(record, EXECUTION_RECORD_FIELDS) || Object.keys(record).length !== EXECUTION_RECORD_FIELDS.size
    || !hasOnlyKeys(record.input_summary, INPUT_SUMMARY_KEYS)
    || !hasOnlyKeys(record.result_summary, RESULT_SUMMARY_KEYS)
    || typeof record.tool_call_id !== "string" || !record.tool_call_id || record.tool_call_id.length > 200
    || typeof record.tool !== "string" || !record.tool || record.tool.length > 120
    || !["started", "completed", "interrupted"].includes(record.state)
    || !Number.isSafeInteger(record.start_seq) || record.start_seq < 1
    || !Number.isSafeInteger(record.update_seq) || record.update_seq < record.start_seq
    || !Number.isSafeInteger(record.start_order) || record.start_order < 1
    || typeof record.started_at !== "string" || !Number.isFinite(Date.parse(record.started_at))
    || (record.ended_at !== null && (typeof record.ended_at !== "string" || !Number.isFinite(Date.parse(record.ended_at))))
    || (record.duration_ms !== null && (!Number.isSafeInteger(record.duration_ms) || record.duration_ms < 0))
    || !["", "once", "always", "reject"].includes(record.permission_decision)
    || typeof record.permission_effect !== "string" || record.permission_effect.length > 80
    || typeof record.is_error !== "boolean" || typeof record.truncated !== "boolean"
    || (record.state === "started" && (record.ended_at !== null || record.duration_ms !== null))
    || (record.state !== "started" && record.ended_at === null)) {
    throw new Error("invalid execution record");
  }
  if (record.seq !== record.update_seq) throw new Error("invalid execution record");
  const inputSummary = sanitizeExecutionSummary(record.input_summary, { input: true, tool: record.tool });
  const resultSummary = sanitizeExecutionSummary(record.result_summary, { tool: record.tool });
  return {
    seq: record.update_seq,
    start_seq: record.start_seq,
    start_order: record.start_order,
    update_seq: record.update_seq,
    tool_call_id: record.tool_call_id,
    tool: record.tool,
    state: record.state,
    started_at: record.started_at,
    ended_at: record.ended_at,
    duration_ms: record.duration_ms,
    input_summary: inputSummary,
    result_summary: resultSummary,
    is_error: record.is_error,
    permission_effect: record.permission_effect,
    permission_decision: record.permission_decision,
    truncated: record.truncated,
  };
}

export function normalizeExecutionSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)
    || Object.keys(snapshot).length !== 6
    || snapshot.version !== 1 || !Number.isSafeInteger(snapshot.maxRecords)
    || snapshot.maxRecords < 1 || snapshot.maxRecords > MAX_JOURNAL_RECORDS
    || !Number.isSafeInteger(snapshot.startOrder) || snapshot.startOrder < 0
    || !Number.isSafeInteger(snapshot.updateSeq) || snapshot.updateSeq < 0
    || !Number.isSafeInteger(snapshot.evictedThrough) || snapshot.evictedThrough < 0
    || snapshot.evictedThrough > snapshot.updateSeq
    || !Array.isArray(snapshot.records) || snapshot.records.length > snapshot.maxRecords) {
    throw new Error("invalid execution journal snapshot");
  }
  const ids = new Set();
  const updateSeqs = new Set();
  const startSeqs = new Set();
  const startOrders = new Set();
  const records = snapshot.records.map((raw) => {
    const record = normalizeExecutionRecord(raw);
    if (ids.has(record.tool_call_id) || updateSeqs.has(record.update_seq)
      || startSeqs.has(record.start_seq) || startOrders.has(record.start_order)
      || record.update_seq > snapshot.updateSeq || record.start_order > snapshot.startOrder) {
      throw new Error("invalid execution journal snapshot");
    }
    ids.add(record.tool_call_id);
    updateSeqs.add(record.update_seq);
    startSeqs.add(record.start_seq);
    startOrders.add(record.start_order);
    return record;
  });
  return { version: 1, maxRecords: snapshot.maxRecords, startOrder: snapshot.startOrder,
    updateSeq: snapshot.updateSeq, evictedThrough: snapshot.evictedThrough, records };
}

function cloneRecord(record) {
  return {
    seq: record.update_seq,
    start_seq: record.start_seq,
    start_order: record.start_order,
    update_seq: record.update_seq,
    tool_call_id: record.tool_call_id,
    tool: record.tool,
    state: record.state,
    started_at: record.started_at,
    ended_at: record.ended_at,
    duration_ms: record.duration_ms,
    input_summary: sanitizeExecutionSummary(record.input_summary, { input: true, tool: record.tool }),
    result_summary: sanitizeExecutionSummary(record.result_summary, { tool: record.tool }),
    is_error: Boolean(record.is_error),
    permission_effect: String(record.permission_effect || "").slice(0, 80),
    permission_decision: ["once", "always", "reject"].includes(record.permission_decision) ? record.permission_decision : "",
    truncated: Boolean(record.truncated),
  };
}

// Bounded per-session monotonic execution journal with ONE comparable
// sequence space (the global update cursor).
//
// Deployment-critical invariant: execution_floor (Bridge run floor),
// per-record stable start cursor, per-record latest update cursor, and
// adapter head ALL live in the same global update-sequence space.
// - Every externally observable mutation (start, update, end,
//   interrupted, permission decision) advances the global update cursor.
// - Each record stores start_seq = global update cursor assigned AT ITS
//   START, unchanged for the life of the record. Run ownership is
//   start_seq > execution_floor.
// - update_seq = latest mutation cursor, advancing on every mutation.
// - start_order is a human-friendly start ordinal for display/debug ONLY;
//   it is NEVER compared to execution_floor and carries no ownership
//   meaning (a separate ordinal would diverge from the update cursor as
//   soon as any tool receives updates).
// - head = global update cursor. read(after) returns current snapshots
//   whose update_seq > after ordered by update_seq.
// Eviction tracks explicit coverage (evictedThrough) so evicted update
// history reports audit_gap instead of silent completeness. Bounded
// record memory.
export class ExecutionJournal {
  constructor({ maxRecords = MAX_JOURNAL_RECORDS, snapshot = null, onCommit = null } = {}) {
    this.maxRecords = Math.max(1, Math.min(maxRecords, MAX_JOURNAL_RECORDS));
    this.startOrder = 0;
    this.updateSeq = 0;
    this.records = new Map();
    this.evictedThrough = 0;
    this.onCommit = typeof onCommit === "function" ? onCommit : null;
    this.failed = false;
    if (snapshot !== null) this._restore(snapshot);
  }

  _restore(snapshot) {
    const safe = normalizeExecutionSnapshot(snapshot);
    this.maxRecords = safe.maxRecords;
    this.startOrder = safe.startOrder;
    this.updateSeq = safe.updateSeq;
    this.evictedThrough = safe.evictedThrough;
    this.records = new Map(safe.records.map((record) => [record.tool_call_id, record]));
  }

  assertHealthy() {
    if (this.failed) throw new Error("execution journal unavailable");
  }

  _commit() {
    this.assertHealthy();
    if (!this.onCommit) return;
    try {
      this.onCommit(this.snapshot());
    } catch {
      this.failed = true;
      throw new Error("execution journal unavailable");
    }
  }

  snapshot() {
    this.assertHealthy();
    return normalizeExecutionSnapshot({
      version: 1,
      maxRecords: this.maxRecords,
      startOrder: this.startOrder,
      updateSeq: this.updateSeq,
      evictedThrough: this.evictedThrough,
      records: [...this.records.values()].map(cloneRecord),
    });
  }

  get head() {
    this.assertHealthy();
    return this.updateSeq;
  }

  get oldest() {
    this.assertHealthy();
    if (!this.records.size) return this.updateSeq + 1;
    let min = Infinity;
    for (const record of this.records.values()) {
      if (record.update_seq < min) min = record.update_seq;
    }
    return min === Infinity ? this.updateSeq + 1 : min;
  }

  _bump() {
    this.updateSeq += 1;
    return this.updateSeq;
  }

  _finishTiming(record) {
    record.ended_at = new Date().toISOString();
    try {
      const start = Date.parse(record.started_at);
      const end = Date.parse(record.ended_at);
      if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
        record.duration_ms = end - start;
      } else {
        record.duration_ms = null;
      }
    } catch {
      record.duration_ms = null;
    }
  }

  start({ toolCallId, tool, inputSummary, permissionEffect }) {
    this.assertHealthy();
    const id = String(toolCallId || "").slice(0, 200);
    if (!id || !tool) return null;
    if (this.records.has(id)) return this.records.get(id);
    const toolName = String(tool).slice(0, 120);
    const safeInput = sanitizeExecutionSummary(inputSummary, { input: true, tool: toolName });
    this.startOrder += 1;
    const cursor = this._bump();
    const record = {
      seq: cursor,
      start_seq: cursor,
      start_order: this.startOrder,
      update_seq: cursor,
      tool_call_id: id,
      tool: toolName,
      state: "started",
      started_at: new Date().toISOString(),
      ended_at: null,
      duration_ms: null,
      input_summary: safeInput,
      result_summary: {},
      is_error: false,
      permission_effect: String(permissionEffect || ""),
      permission_decision: "",
      truncated: false,
    };
    this.records.set(id, record);
    this._evict();
    this._commit();
    return record;
  }

  update({ toolCallId, resultPreview }) {
    this.assertHealthy();
    const record = this.records.get(String(toolCallId || ""));
    if (!record || record.state !== "started") return null;
    if (resultPreview && typeof resultPreview === "object") {
      record.result_summary = sanitizeExecutionSummary({ ...record.result_summary, ...resultPreview }, { tool: record.tool });
    }
    record.update_seq = this._bump();
    this._commit();
    return record;
  }

  end({ toolCallId, resultSummary, isError }) {
    this.assertHealthy();
    const record = this.records.get(String(toolCallId || ""));
    if (!record) return null;
    if (record.state !== "started") return record;
    const safeResult = sanitizeExecutionSummary(resultSummary, { tool: record.tool });
    record.state = "completed";
    this._finishTiming(record);
    record.result_summary = safeResult;
    record.is_error = Boolean(isError);
    record.truncated = Boolean(
      record.input_summary?.truncated || record.result_summary?.truncated);
    record.update_seq = this._bump();
    this._commit();
    return record;
  }

  markInterrupted() {
    this.assertHealthy();
    let changed = false;
    for (const record of this.records.values()) {
      if (record.state === "started") {
        record.state = "interrupted";
        this._finishTiming(record);
        record.update_seq = this._bump();
        changed = true;
      }
    }
    if (changed) this._commit();
  }

  setPermissionDecision(toolCallId, decision) {
    this.assertHealthy();
    const record = this.records.get(String(toolCallId || ""));
    if (!record) return;
    if (["once", "always", "reject"].includes(decision)) {
      if (record.state !== "started" || record.permission_decision === decision) return;
      record.permission_decision = decision;
      record.update_seq = this._bump();
      this._commit();
    }
  }

  _evict() {
    while (this.records.size > this.maxRecords) {
      // Evict the record with the smallest update_seq (stalest snapshot).
      let victim = null;
      let victimSeq = Infinity;
      for (const [id, record] of this.records) {
        if (record.update_seq < victimSeq) {
          victimSeq = record.update_seq;
          victim = id;
        }
      }
      if (victim === null) break;
      const removed = this.records.get(victim);
      if (removed && removed.update_seq > this.evictedThrough) {
        this.evictedThrough = removed.update_seq;
      }
      this.records.delete(victim);
    }
  }

  // Paginated read on the UPDATE cursor: after=<update_seq> exclusive,
  // limit bounded. Returns {updates, next, head, oldest,
  // audit_gap, cursor_too_old}. Evicted update history is reported
  // explicitly via evicted coverage, never as silent completeness.
  read({ after = 0, limit = 50 } = {}) {
    this.assertHealthy();
    const afterSeq = Math.max(0, Math.floor(Number(after) || 0));
    const boundedLimit = Math.max(1, Math.min(Math.floor(Number(limit) || 50), MAX_READ_LIMIT));
    const head = this.updateSeq;
    const oldest = this.oldest;
    if (afterSeq < this.evictedThrough) {
      return {
        updates: [],
        next: this.evictedThrough,
        head,
        oldest,
        audit_gap: true,
        cursor_too_old: true,
      };
    }
    const ranked = [...this.records.values()]
      .filter((record) => record.update_seq > afterSeq)
      .sort((a, b) => a.update_seq - b.update_seq)
      .slice(0, boundedLimit);
    const updates = ranked.map((record) => publicRecord(record));
    const next = updates.length ? updates[updates.length - 1].update_seq : afterSeq;
    return { updates, next, head, oldest, audit_gap: false, cursor_too_old: false };
  }
}

// Public journal record: bounded, no raw args/contents beyond the
// tool-specific summaries above. seq AND update_seq are the
// cursor-bearing latest-update cursor in the global update-sequence
// space (Bridge polls after=<cursor>). start_seq is the stable
// started-update cursor IN THE SAME SPACE, assigned at start and never
// mutated; run ownership is start_seq > execution_floor. start_order is
// a display-only ordinal, never compared to floors. List views must
// strip result output; detail views may include the bash output preview.
export function publicRecord(record, { includeResult = true } = {}) {
  const base = {
    seq: record.update_seq,
    update_seq: record.update_seq,
    start_seq: record.start_seq,
    start_order: record.start_order,
    execution_id: `${record.tool_call_id}`,
    tool_call_id: record.tool_call_id,
    tool: record.tool,
    state: record.state,
    started_at: record.started_at,
    ended_at: record.ended_at,
    duration_ms: record.duration_ms,
    is_error: Boolean(record.is_error),
    permission_effect: record.permission_effect,
    permission_decision: record.permission_decision,
    truncated: Boolean(record.truncated),
    input_summary: sanitizeExecutionSummary(record.input_summary, { input: true, tool: record.tool }),
  };
  if (includeResult) base.result_summary = sanitizeExecutionSummary(record.result_summary, { tool: record.tool });
  return base;
}

// List/summary surface: id/sequence/tool/state/target-or-command
// preview/timing/duration/error/permission effect+decision/truncation.
// seq here is the STABLE start cursor (start_seq) in the global update
// space, so list ordering never shifts when a record is later updated.
// Never includes result output bodies or raw source content.
export function summaryRecord(record) {
  const input = record.input_summary || {};
  let target = "";
  if (record.tool === "bash") {
    target = String(input.command || "").slice(0, 200);
  } else if (typeof input.target === "string") {
    target = input.target.slice(0, 200);
  } else {
    // Extension-tool list surface: no result body; a bounded redacted
    // safe selector (or nothing) as the target preview.
    for (const field of EXTENSION_SELECTOR_FIELDS) {
      const candidate = input[field];
      if (typeof candidate === "string" && candidate) {
        target = redactExtensionText(candidate).slice(0, 200);
        break;
      }
    }
  }
  const startCursor = record.start_seq ?? record.seq;
  return {
    seq: startCursor,
    update_seq: record.update_seq ?? record.seq,
    start_seq: startCursor,
    start_order: record.start_order,
    execution_id: `${record.tool_call_id}`,
    tool_call_id: record.tool_call_id,
    tool: record.tool,
    state: record.state,
    target_preview: target,
    started_at: record.started_at,
    ended_at: record.ended_at,
    duration_ms: record.duration_ms,
    is_error: Boolean(record.is_error),
    permission_effect: record.permission_effect,
    permission_decision: record.permission_decision,
    truncated: Boolean(record.truncated),
  };
}
