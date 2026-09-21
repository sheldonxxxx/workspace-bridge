// Operational container logging contract (Node side).
//
// One-line JSON records for Docker json-file logs. No dependencies.
// Same security boundary as workspace_bridge/oplog.py: only explicit
// allowlisted scalar fields are emitted. Server URLs, usernames, passwords,
// tokens, event contents/metadata, permission objects/patterns and raw
// error bodies are never accepted into a record. Unknown fields are dropped.

export const DEFAULT_LOG_LEVEL = "INFO";
export const ALLOWED_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

export const EVENTS = new Set([
  "adapter_ready",
  "eventhub_transition",
  "eventhub_error",
  "eventhub_counts",
  "permission_list",
  "permission_event",
  "question_list",
]);

const ALLOWED_FIELDS = new Set([
  "run_id", "session_id", "job_id", "workspace_id", "request_id",
  "state", "reason", "code", "status", "action", "source", "decision",
  "generation",
  "matched", "count", "examined", "duration_ms", "transitions",
  "consecutive_failures", "version", "adapter_version", "runtime_configured",
  "server_configured", "locked", "workspace_count", "enabled_count",
  "model", "session_reused", "message_count", "pending_count",
  "raw_event_count", "control_event_count", "functional_event_count",
]);

const MAX_STR = 200;

const LEVEL_RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 };

let currentLevel = parseLogLevel(process.env.WB_LOG_LEVEL ?? DEFAULT_LOG_LEVEL, true);

export function parseLogLevel(raw, fallback = false) {
  const text = String(raw ?? DEFAULT_LOG_LEVEL).trim().toUpperCase();
  if (ALLOWED_LEVELS.includes(text)) return text;
  if (fallback) return DEFAULT_LOG_LEVEL;
  throw new Error(`WB_LOG_LEVEL must be one of ${ALLOWED_LEVELS.join("/")} (default ${DEFAULT_LOG_LEVEL})`);
}

export function setLogLevel(level) {
  currentLevel = parseLogLevel(level);
}

export function getLogLevel() {
  return currentLevel;
}

// Invalid WB_LOG_LEVEL safely falls back to INFO with one warning line;
// the adapter never crashes on a logging-only setting.
export function initFromEnv(env = process.env) {
  const raw = env?.WB_LOG_LEVEL;
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    currentLevel = DEFAULT_LOG_LEVEL;
    return currentLevel;
  }
  const text = String(raw).trim().toUpperCase();
  if (ALLOWED_LEVELS.includes(text)) {
    currentLevel = text;
    return currentLevel;
  }
  currentLevel = DEFAULT_LOG_LEVEL;
  emit("WARNING", "adapter", "adapter_ready", {
    status: "degraded", reason: "invalid_log_level", code: "invalid_log_level",
  });
  return currentLevel;
}

function cleanValue(value) {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return undefined;
    return Math.max(-1e12, Math.min(1e12, Math.round(value * 1000) / 1000));
  }
  if (typeof value === "string") return value.slice(0, MAX_STR);
  return undefined;
}

export function buildRecord(level, component, event, fields = {}) {
  if (!EVENTS.has(event)) return null;
  const record = {
    timestamp: new Date().toISOString(),
    level: ALLOWED_LEVELS.includes(level) ? level : "INFO",
    component: String(component ?? "adapter").slice(0, 64) || "adapter",
    event,
  };
  for (const [key, value] of Object.entries(fields)) {
    if (!ALLOWED_FIELDS.has(key)) continue;
    if (value === null) {
      record[key] = null;
      continue;
    }
    const cleaned = cleanValue(value);
    if (cleaned === undefined) continue;
    record[key] = cleaned;
  }
  return record;
}

export function formatRecord(record) {
  const keys = Object.keys(record).sort();
  const sorted = {};
  for (const key of keys) sorted[key] = record[key];
  return JSON.stringify(sorted);
}

function shouldEmit(level) {
  return (LEVEL_RANK[level] ?? 20) >= (LEVEL_RANK[currentLevel] ?? 20);
}

export function emit(level, component, event, fields = {}) {
  try {
    if (!shouldEmit(ALLOWED_LEVELS.includes(level) ? level : "INFO")) return;
    const record = buildRecord(level, component, event, fields);
    if (!record) return;
    process.stdout.write(formatRecord(record) + "\n");
  } catch {
    // Logging must never break the adapter.
  }
}

// Bounded error class/code only; never raw messages or bodies.
export function errorCode(error) {
  if (!error) return "unknown";
  if (typeof error.code === "string" && error.code) {
    return String(error.code).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 80) || "error";
  }
  const status = error.status ?? error.statusCode ?? error.response?.status;
  if (Number.isFinite(status)) return `http_${status}`;
  const name = typeof error.name === "string" && error.name ? error.name : "error";
  return String(name).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 80) || "error";
}
