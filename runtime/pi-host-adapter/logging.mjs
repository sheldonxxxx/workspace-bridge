// Dedicated structured logging for the native Pi host adapter.
//
// One-line JSON records with `timestamp` (not `ts`), `level`, `component`,
// `event`, plus allowlisted bounded scalar fields only. Unknown fields and
// non-scalar values are dropped, never serialized.
//
// Shared level semantics with the Python Bridge:
// - DEBUG: high-frequency/repeated internals and normal SDK tool-event
//   tracing. Temporary troubleshooting only.
// - INFO: healthy/expected lifecycle transitions (adapter ready, session
//   created). Normal production level.
// - WARNING: recoverable degradation, bounded adapter/path/policy
//   rejections, startup projects/agent-dir problems, SDK dispatch
//   stall/journal anomaly. Alert candidates.
// - ERROR: unexpected internal/runtime exception or unsafe startup
//   condition requiring operator action. Alert candidates.
//
// Supported levels are DEBUG/INFO/WARNING/ERROR with INFO default.
// Logging failures never throw and never alter request/session state.

export const LOG_LEVEL_ENV = "WB_LOG_LEVEL";
export const DEFAULT_LOG_LEVEL = "INFO";
export const ALLOWED_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
const LEVEL_RANK = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };

// Stable adapter events currently needed. Anything else is dropped rather
// than emitted with an unknown name.
export const EVENTS = new Set([
  "pi_adapter_ready",
  "adapter_config_error",
  "projects_parent_unavailable",
  "agent_dir_rejected",
  "session_create",
  "request_rejected",
  "request_error",
  "process_error",
  "sdk_tool_event_trace",
]);

// Bounded scalar fields only. IDs/state names/counts/durations/sanitized
// codes and boolean health flags are acceptable; everything else is dropped.
// Never add directory/title/model/prompt/tool args/results/token/policy
// bodies here.
export const ALLOWED_FIELDS = new Set([
  "adapter_version",
  "agent_dir_allowed",
  "agent_dir_explicit",
  "code",
  "status",
  "source",
  "reason",
  "session_id",
  "instance",
  "locked",
  "pi_usable",
  "pi_version",
  "projects_configured",
  // sdk_tool_event_trace correlation fields (bounded identifiers/counts).
  "stage",
  "event_type",
  "tool_call_id",
  "pending_tool_count",
  "journal_state",
  "journal_update_seq",
  "duration_ms",
]);

const MAX_STR = 200;

export function parseLogLevel(raw) {
  const text = String(raw ?? DEFAULT_LOG_LEVEL).trim().toUpperCase();
  if (!ALLOWED_LEVELS.includes(text)) {
    // Never echo the raw value: it could carry secrets in a misconfigured env.
    throw new Error(
      `${LOG_LEVEL_ENV} must be one of ${ALLOWED_LEVELS.join(", ")} (default ${DEFAULT_LOG_LEVEL})`,
    );
  }
  return text;
}

export function logLevelFromEnv(env = process.env) {
  const raw = env ? env[LOG_LEVEL_ENV] : undefined;
  if (raw === undefined || raw === null) return DEFAULT_LOG_LEVEL;
  if (typeof raw === "string" && raw.trim() === "") return DEFAULT_LOG_LEVEL;
  return parseLogLevel(raw);
}

export function levelRank(level) {
  return LEVEL_RANK[level] ?? LEVEL_RANK.INFO;
}

export function shouldLog(recordLevel, configuredLevel) {
  return levelRank(recordLevel) >= levelRank(configuredLevel);
}

function cleanValue(value) {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return undefined;
    if (Number.isInteger(value)) {
      return Math.max(-1e12, Math.min(1e12, value));
    }
    return Math.round(value * 1000) / 1000;
  }
  if (typeof value === "string") return value.slice(0, MAX_STR);
  return undefined;
}

export function buildRecord(component, event, level = "INFO", fields = {}) {
  if (!EVENTS.has(event)) return null;
  const safeLevel = ALLOWED_LEVELS.includes(level) ? level : "INFO";
  const record = {
    timestamp: new Date().toISOString(),
    level: safeLevel,
    component: String(component || "pi-adapter").slice(0, 64) || "pi-adapter",
    event,
  };
  if (fields && typeof fields === "object") {
    for (const [key, value] of Object.entries(fields)) {
      if (!ALLOWED_FIELDS.has(key)) continue;
      if (value === null || value === undefined) {
        if (value === null) record[key] = null;
        continue;
      }
      const cleaned = cleanValue(value);
      if (cleaned === undefined) continue;
      record[key] = cleaned;
    }
  }
  return record;
}

export function formatRecord(record) {
  return JSON.stringify(record);
}

// Sanitized error code/category only; never the raw message, stack, or
// arbitrary values. Mirrors the Python bridge `oplog.error_code`
// contract: known error codes pass through bounded, otherwise the
// constructor name is sanitized to alphanumerics/underscore.
export function sanitizedErrorCode(error) {
  // Type-derived code only (mirrors Python `oplog.error_code` for
  // non-BridgeError): never error.message, error.code, stacks, or
  // arbitrary values.
  try {
    const name = error?.constructor?.name || "error";
    const clean = String(name).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 80);
    return clean || "error";
  } catch {
    return "error";
  }
}

// Create a level-filtered logger. `writeStdout`/`writeStderr` default to the
// process streams; WARNING/ERROR go stderr, DEBUG/INFO go stdout. The level
// threshold is enforced before writing. Logging failures never throw and
// never alter request/session state.
export function createLogger({
  level = DEFAULT_LOG_LEVEL,
  writeStdout = (line) => process.stdout.write(`${line}\n`),
  writeStderr = (line) => process.stderr.write(`${line}\n`),
} = {}) {
  const configured = parseLogLevel(level);
  function log(recordLevel, component, event, fields = {}) {
    try {
      const safeLevel = ALLOWED_LEVELS.includes(recordLevel) ? recordLevel : "INFO";
      if (!shouldLog(safeLevel, configured)) return;
      const record = buildRecord(component, event, safeLevel, fields);
      if (!record) return;
      const line = formatRecord(record);
      if (safeLevel === "WARNING" || safeLevel === "ERROR") writeStderr(line);
      else writeStdout(line);
    } catch {
      // Logging must never change request/session state.
    }
  }
  log.level = configured;
  return log;
}
