// Native macOS entrypoint for the Pi host adapter.
//
// Runs natively as the normal macOS user (not Docker, not root) and owns
// in-process AgentSession instances (Pi 0.87.0 SDK) for managed Bridge
// sessions. At startup the Pi binary/version is checked safely as a
// deployment signal: a missing or unusable Pi means degraded health and
// session creation fails closed. Shutdown disposes owned sessions; only
// this process's own sessions are ever touched.
//
// Operational logging: one-line JSON records with `timestamp`, `level`,
// `component`, `event`, plus allowlisted bounded scalar fields only
// (see logging.mjs). WB_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR, default INFO)
// filters records before writing; DEBUG/INFO go stdout, WARNING/ERROR go
// stderr. An invalid nonblank WB_LOG_LEVEL fails startup safely without
// exposing env values, tokens, or paths.
import { randomUUID } from "node:crypto";

import { PiAdapter } from "./adapter.mjs";
import { checkPiBinary, isAgentDirAllowed, loadConfig } from "./config.mjs";
import { createLogger, sanitizedErrorCode } from "./logging.mjs";
import { canonicalizeProjectsDir } from "./paths.mjs";
import { createPiAdapterServer } from "./server.mjs";

export function createLog(env = process.env) {
  // loadConfig validates WB_LOG_LEVEL; the logger then enforces it.
  const level = loadConfig(env).logLevel;
  return createLogger({ level });
}

export function buildMain(env = process.env) {
  const config = loadConfig(env);
  const log = createLogger({ level: config.logLevel });
  const piCheck = checkPiBinary(config.piBinary);
  let projectsRoot = null;
  let projectsError = null;
  try {
    projectsRoot = canonicalizeProjectsDir(config.projectsDirRaw);
  } catch (error) {
    projectsError = error;
  }
  const agentDirOk = isAgentDirAllowed(config.agentDir);
  const usable = piCheck.usable && projectsRoot !== null && agentDirOk;
  return { config, piCheck, projectsRoot, projectsError, agentDirOk, usable, log };
}

// Fatal HTTP server/listen error: one sanitized structured ERROR record,
// then nonzero termination. Never logs message/stack/address/port/path/
// env/token. Never swallows: the process always exits nonzero via exitFn
// (injectable so tests never kill the runner). No broad
// uncaughtException/unhandledRejection handling here: Node's default fatal
// semantics remain for those.
export function handleFatalServerError(log, error, exitFn = (code) => process.exit(code)) {
  try {
    log("ERROR", "pi-adapter", "process_error", {
      code: sanitizedErrorCode(error),
      source: "http_server",
    });
  } catch { /* error-path logging must never throw */ }
  exitFn(1);
}

export function attachServerErrorHandler(server, log, exitFn) {
  server.on("error", (error) => handleFatalServerError(log, error, exitFn));
  return server;
}

export function startServer({ config, projectsRoot, piCheck, agentDirOk, usable, log }) {
  const onLog = log || createLogger({ level: config.logLevel });
  const adapter = projectsRoot
    ? new PiAdapter({
        projectsRoot,
        agentDir: config.agentDir,
        piUsable: piCheck.usable && agentDirOk,
        piVersion: piCheck.version,
        onLog,
      })
    : null;
  const instance = randomUUID();
  const server = createPiAdapterServer({
    adapter,
    token: config.token,
    adapterVersion: config.adapterVersion,
    instance,
    piUsable: usable,
    piVersion: piCheck.version,
    onLog,
  });
  return { adapter, server, instance };
}

const isEntry = typeof process.argv[1] === "string" && process.argv[1].endsWith("main.mjs");

if (isEntry) {
  let built;
  try {
    built = buildMain(process.env);
  } catch (error) {
    // Startup config failure: safe one-line record without env values,
    // tokens, or paths, then a nonzero exit.
    try {
      const fallback = createLogger({ level: "INFO" });
      fallback("ERROR", "pi-adapter", "adapter_config_error", { code: "invalid_log_level" });
    } catch { /* never throw from startup logging */ }
    process.exit(1);
  }
  const { adapter, server, instance } = startServer(built);
  const { config, piCheck } = built;
  const log = built.log;
  attachServerErrorHandler(server, log);
  server.listen(config.port, config.host, () => {
    // Structured startup record: booleans/version only, never tokens,
    // roots, or full paths.
    log("INFO", "pi-adapter", "pi_adapter_ready", {
      adapter_version: config.adapterVersion,
      instance,
      locked: config.locked,
      pi_usable: piCheck.usable,
      ...(piCheck.version ? { pi_version: piVersionSafe(piCheck.version) } : {}),
      projects_configured: built.projectsRoot !== null,
      agent_dir_explicit: config.agentDirExplicit,
      agent_dir_allowed: built.agentDirOk,
    });
    if (built.projectsError) {
      log("WARNING", "pi-adapter", "projects_parent_unavailable", {
        code: built.projectsError.code || "not_configured",
      });
    }
    if (!built.agentDirOk) {
      log("ERROR", "pi-adapter", "agent_dir_rejected", {});
    }
  });

  const shutdown = () => {
    server.close(() => process.exit(0));
    if (adapter) {
      adapter.shutdown().catch(() => {});
    }
    setTimeout(() => process.exit(0), 5000).unref();
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

function piVersionSafe(version) {
  return String(version || "").slice(0, 40);
}
