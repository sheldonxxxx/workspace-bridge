// Native macOS entrypoint for the Pi host adapter.
//
// Runs natively as the normal macOS user (not Docker, not root) and owns
// read-only `pi --mode rpc` subprocesses. At startup the Pi binary/version is
// checked safely: a missing or unusable Pi means degraded health and session
// creation fails closed. Owned children get a bounded grace on shutdown;
// only this process's own children are ever signalled.
import { randomUUID } from "node:crypto";

import { PiAdapter } from "./adapter.mjs";
import { checkPiBinary, isAgentDirAllowed, loadConfig } from "./config.mjs";
import { canonicalizeProjectsDir } from "./paths.mjs";
import { RPC_TIMEOUT_MS } from "./rpc.mjs";
import { createPiAdapterServer } from "./server.mjs";

const SHUTDOWN_GRACE_MS = 3000;

function log(level, component, event, fields = {}) {
  const record = { ts: new Date().toISOString(), level, component, event, ...fields };
  const line = JSON.stringify(record);
  if (level === "WARNING" || level === "ERROR") process.stderr.write(line + "\n");
  else process.stdout.write(line + "\n");
}

export function buildMain(env = process.env) {
  const config = loadConfig(env);
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
  return { config, piCheck, projectsRoot, projectsError, agentDirOk, usable };
}

export function startServer({ config, projectsRoot, piCheck, agentDirOk, usable }) {
  const adapter = projectsRoot
    ? new PiAdapter({
        projectsRoot,
        piBinary: config.piBinary,
        agentDir: config.agentDir,
        timeoutMs: RPC_TIMEOUT_MS,
        piUsable: piCheck.usable && agentDirOk,
        piVersion: piCheck.version,
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
    onLog: log,
  });
  return { adapter, server, instance };
}

const isEntry = typeof process.argv[1] === "string" && process.argv[1].endsWith("main.mjs");

if (isEntry) {
  const built = buildMain(process.env);
  const { adapter, server, instance } = startServer(built);
  const { config, piCheck } = built;
  server.listen(config.port, config.host, () => {
    // Structured startup record: booleans/version only, never tokens,
    // roots, or full paths.
    log("INFO", "pi-adapter", "pi_adapter_ready", {
      adapter_version: config.adapterVersion,
      instance,
      locked: config.locked,
      pi_usable: piCheck.usable,
      ...(piCheck.version ? { pi_version: piCheck.version } : {}),
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
      adapter.shutdown({ graceMs: SHUTDOWN_GRACE_MS }).catch(() => {});
    }
    setTimeout(() => process.exit(0), SHUTDOWN_GRACE_MS + 2000).unref();
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}
