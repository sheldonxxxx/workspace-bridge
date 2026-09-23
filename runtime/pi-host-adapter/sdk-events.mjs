// In-process AgentSession event normalization for the Pi host adapter.
//
// The SDK delivers tool_execution_start/update/end events directly to
// session subscribers with the same field shapes the old RPC child sent
// over stdout ({toolCallId, toolName, args} for start; {partialResult}
// for update; {result, isError} for end). These helpers reduce each raw
// event to bounded permission metadata plus a SEPARATE bounded audit
// summary, exactly as the removed JSONL layer did. Raw event objects are
// never retained after normalization.
//
// There is no framing, no byte limit, and no session death here: a
// multi-megabyte tool result is summarized to bounded evidence while the
// session stays usable. That removes the old MAX_LINE_BYTES failure class
// entirely instead of raising it.
import { SHELL_TOOL, SUPPORTED_TOOLS, extractBashCommand } from "./policy.mjs";
import { summarizeExtensionInput, summarizeExtensionResult, summarizeInput, summarizeResult } from "./executions.mjs";

// Managed Bridge built-ins governed by file/shell policy. Any other tool
// name belongs to an explicitly enabled third-party extension (trusted
// native code, not constrained by structured file/shell policy).
export function isManagedTool(toolName) {
  return toolName === SHELL_TOOL || SUPPORTED_TOOLS.includes(toolName);
}

// Independent permission-metadata bounds: preflight correlation retains
// only these small fields, never tool payloads.
export const MAX_TOOL_CALL_ID_CHARS = 200;
export const MAX_TOOL_NAME_CHARS = 120;
export const MAX_PERMISSION_PATH_CHARS = 4096;

// Reduce a raw tool_execution_start to bounded permission metadata plus
// a SEPARATE bounded audit input summary. Permission keeps only the
// single path operand per known file-tool schema (read/edit/write path;
// grep/find/ls optional path); bash keeps ONLY the bounded exact command
// plus verified timeout ({command, timeoutMs}) so shell-policy evaluation
// sees the exact authority identity (hash + timeout) while audit carries
// the full bounded command evidence separately. A bash input that cannot
// preserve the exact command identity (malformed or truncated) normalizes
// to null so evaluation fails closed instead of judging a different
// command. Write content, edit old/new text, grep patterns, find globs,
// environment, and arbitrary args are NEVER retained in permission
// metadata; audit carries only hashes/counts/previews per executions.mjs.
// Raw event objects are never retained after normalization. Returns null
// when unusable.
export function normalizeToolStart(message) {
  if (!message || typeof message !== "object") return null;
  const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
  const toolName = typeof message.toolName === "string" ? message.toolName : "";
  if (!toolCallId || toolCallId.length > MAX_TOOL_CALL_ID_CHARS
      || !toolName || toolName.length > MAX_TOOL_NAME_CHARS) {
    return null;
  }
  const args = message.args;
  if (args !== undefined
      && (args === null || typeof args !== "object" || Array.isArray(args))) {
    // Present-but-malformed args: keep the call identity with an empty
    // input so evaluation fails closed as malformed (never allowed).
    // Audit records the failure safely without dumping args.
    return {
      type: "tool_execution_start", toolCallId, toolName, input: null,
      auditInput: { error: "malformed_input" },
    };
  }
  let input = {};
  if (SUPPORTED_TOOLS.includes(toolName) && args && typeof args === "object"
      && Object.prototype.hasOwnProperty.call(args, "path")) {
    const rawPath = args.path;
    // Never truncate paths: an oversized/invalid path becomes an explicit
    // null so evaluation denies instead of judging a different target.
    input = (typeof rawPath === "string" && rawPath && !rawPath.includes("\0")
        && rawPath.length <= MAX_PERMISSION_PATH_CHARS)
      ? { path: rawPath }
      : { path: null };
  } else if (toolName === SHELL_TOOL) {
    // Bounded exact-command permission metadata: reuse the single
    // extractBashCommand parser/bounds so the evaluator computes the same
    // command hash and verified timeout as from the original Pi args.
    // Only {command, timeoutMs} are retained; aliases (cmd/script/code)
    // canonicalize to command and second-based timeouts canonicalize to
    // verified milliseconds. Truncated (oversized) commands cannot
    // preserve the exact hash identity, so they fail closed as null
    // instead of authorizing the truncated prefix.
    try {
      const extracted = extractBashCommand(args ?? {});
      if (extracted.ok && !extracted.truncated) {
        input = { command: extracted.command, timeoutMs: extracted.timeoutMs };
      } else {
        input = null;
      }
    } catch {
      input = null;
    }
  }
  // Separate audit path: bounded tool-specific evidence, never raw args.
  // Managed tools keep their tool-specific summaries; explicitly enabled
  // third-party extension tools carry the bounded generic extension
  // summary (hash/size/keys + safe selectors), never arbitrary args.
  let auditInput = {};
  try {
    if (isManagedTool(toolName)) {
      const { ok, summary } = summarizeInput(toolName, args ?? {});
      auditInput = ok ? summary : summary;
      if (!ok && (toolName === SHELL_TOOL || SUPPORTED_TOOLS.includes(toolName))) {
        // Preserve the failure marker; evaluation already fails closed.
      } else if (!ok) {
        auditInput = { error: "unknown_tool" };
      }
    } else {
      const { ok, summary } = summarizeExtensionInput(args ?? {});
      auditInput = ok ? summary : { error: "malformed_input" };
    }
  } catch {
    auditInput = { error: "malformed_input" };
  }
  return { type: "tool_execution_start", toolCallId, toolName, input, auditInput };
}

// Separate audit path for tool_execution_update: bounded preview only,
// never raw payloads. Returns null when unusable; unknown tools fail
// safely.
export function normalizeToolUpdate(message) {
  // Verified Pi 0.86.1 shape: {toolCallId, toolName, args,
  // partialResult:{content, details}}. Synthetic {result,preview,data}
  // fallbacks stay for scripted fakes only.
  if (!message || typeof message !== "object") return null;
  const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
  if (!toolCallId || toolCallId.length > MAX_TOOL_CALL_ID_CHARS) return null;
  const toolName = typeof message.toolName === "string" ? message.toolName : "";
  let preview = {};
  try {
    const result = message.partialResult ?? message.result ?? message.preview ?? message.data ?? null;
    const isError = message.isError === true;
    if (toolName && isManagedTool(toolName)) {
      preview = summarizeResult(toolName, result, isError);
    } else if (toolName) {
      preview = summarizeExtensionResult(result, isError);
    } else {
      preview = { is_error: isError };
    }
  } catch {
    preview = {};
  }
  return { type: "tool_execution_update", toolCallId, toolName, auditUpdate: preview };
}

export function normalizeToolEnd(message) {
  if (!message || typeof message !== "object") return null;
  const toolCallId = typeof message.toolCallId === "string" ? message.toolCallId : "";
  if (!toolCallId || toolCallId.length > MAX_TOOL_CALL_ID_CHARS) return null;
  // Separate audit path: bounded result evidence only. Never forward raw
  // file contents, fullOutputPath, reasoning, or environment. fullOutputPath
  // is explicitly dropped here.
  const toolName = typeof message.toolName === "string" ? message.toolName : "";
  const isError = message.isError === true;
  let auditResult = { is_error: isError };
  try {
    if (toolName && isManagedTool(toolName)) {
      auditResult = summarizeResult(toolName, message.result, isError);
    } else if (toolName) {
      auditResult = summarizeExtensionResult(message.result, isError);
    } else {
      // Without a tool name the journal cannot attribute evidence;
      // record only the error bit, never the raw result.
      auditResult = { is_error: isError };
      if (message.cancelled === true) auditResult.cancelled = true;
      if (message.truncated === true) auditResult.truncated = true;
    }
  } catch {
    auditResult = { is_error: isError };
  }
  return { type: "tool_execution_end", toolCallId, toolName, isError, auditResult };
}
