// Package-owned trusted Pi permission extension (milestone 3C1, policy v3).
//
// Loaded explicitly via `-e <this file>` for EVERY managed v3 session --
// including read-only sessions, so read/grep/find/ls policy, protected
// patterns, and external rules are enforced in both modes. Never discovered
// from project/global locations (--no-extensions stays on). Pi itself has
// no sandbox: this is pre-tool policy/approval with exact suspended-call
// resume, not OS containment. Shell Allow runs with native macOS-user
// authority and can bypass structured file path controls.
//
// Protocol with the adapter (over Pi RPC UI):
// - tool_call pre-execution interception uses the shared evaluator.
// - deny => {block: true, reason}; allow => continue.
// - ask => await ctx.ui.select(markerTitle, OPTIONS). The marker title is
//   EXACTLY `WB_PERMISSION_V1:<toolCallId>` (opaque: no tool, resource,
//   command, or path text) so the adapter can correlate the
//   extension_ui_request to the exact preflighted call. All human-readable
//   permission metadata comes ONLY from adapter preflight/evaluator state,
//   never from this title. The suspended invocation resumes with the UI
//   response; the LLM never retries.
// - Options are shared constants below. "always" is offered only when the
//   immutable session policy has allow_session_always=true. For bash,
//   always stays session-local and exact-command scoped (hash + timeout).
// - Grants are in-memory exact grantKeys only (file tools: action + exact
//   target; bash: exact command hash + verified timeout); never persisted
//   to disk.
//
// v3 has no fixed filesystem denies and no command rule list. Any internal
// failure fails closed (blocks the call).
import { evaluateToolCall, validatePolicy } from "./policy.mjs";

export const MARKER_PREFIX = "WB_PERMISSION_V1:";
export const OPTION_ONCE = "Allow once";
export const OPTION_ALWAYS = "Always allow exact target this session";
export const OPTION_REJECT = "Reject";

function loadSnapshot() {
  const raw = process.env.WB_PI_POLICY_JSON || "";
  if (!raw) return null;
  try {
    return validatePolicy(JSON.parse(raw));
  } catch {
    return null;
  }
}

function markerTitle(toolCallId) {
  // Opaque correlation marker: prefix + toolCallId only. No tool, action,
  // resource, or path text ever rides the UI protocol.
  const id = String(toolCallId || "").slice(0, 200);
  return `${MARKER_PREFIX}${id}`;
}

export default function (pi) {
  const policy = loadSnapshot();
  const sessionCwd = process.cwd();
  // Exact in-memory grants for this session only: grantKey strings from
  // the shared evaluator (file tools "<tool>\n<target>"; bash
  // "bash\n<commandHash>\n<timeoutMs>").
  const grants = new Set();

  pi.on("tool_call", async (event, ctx) => {
    try {
      const toolName = event && typeof event.toolName === "string" ? event.toolName : "";
      const toolCallId = event && typeof event.toolCallId === "string" ? event.toolCallId : "";
      const input = event && typeof event.input === "object" && event.input !== null ? event.input : null;
      if (!policy) {
        return { block: true, reason: "Permission policy unavailable; failing closed" };
      }
      if (!toolCallId) {
        return { block: true, reason: "Permission correlation unavailable; failing closed" };
      }
      // v3: no fixed filesystem denies, no command rules; ordinary
      // configurable policy + shell_mode govern.
      const base = { cwd: sessionCwd, policy, toolName, input };
      const verdict = evaluateToolCall(base);
      if (verdict.effect === "deny") {
        return { block: true, reason: verdict.reason || "Blocked by permission policy" };
      }
      if (verdict.effect === "allow") {
        return undefined;
      }
      if (verdict.effect !== "ask") {
        return { block: true, reason: "Permission policy unavailable; failing closed" };
      }
      // ask: exact session grant short-circuit with TOCTOU re-evaluation.
      if (verdict.grantKey && grants.has(verdict.grantKey)) {
        const recheck = evaluateToolCall(base);
        if (recheck.effect === "ask"
            && recheck.grantKey === verdict.grantKey
            && recheck.resource === verdict.resource) {
          return undefined;
        }
        return { block: true, reason: "Approval no longer matches; failing closed" };
      }
      const options = policy.allow_session_always
        ? [OPTION_ONCE, OPTION_ALWAYS, OPTION_REJECT]
        : [OPTION_ONCE, OPTION_REJECT];
      const title = markerTitle(toolCallId);
      let choice;
      try {
        choice = await ctx.ui.select(title, options);
      } catch {
        return { block: true, reason: "Approval unavailable; failing closed" };
      }
      if (choice !== OPTION_ONCE && choice !== OPTION_ALWAYS && choice !== OPTION_REJECT) {
        // Reject, cancel, timeout, or unknown: block the exact call.
        return { block: true, reason: "Rejected" };
      }
      if (choice === OPTION_REJECT) {
        return { block: true, reason: "Rejected" };
      }
      if (choice === OPTION_ALWAYS && !policy.allow_session_always) {
        return { block: true, reason: "Rejected" };
      }
      // Re-evaluate immediately after approval before allowing execution;
      // a changed resource/effect/grantKey denies the call.
      const after = evaluateToolCall(base);
      if (after.effect !== "ask"
          || after.grantKey !== verdict.grantKey
          || after.resource !== verdict.resource) {
        return { block: true, reason: "Approval no longer matches; failing closed" };
      }
      if (choice === OPTION_ALWAYS) {
        grants.add(verdict.grantKey);
      }
      return undefined;
    } catch {
      return { block: true, reason: "Permission check failed; failing closed" };
    }
  });
}
