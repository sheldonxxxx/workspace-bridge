// Entry point for the Pi agent plugin (Agent Protocol v2 over stdio).
//
// The supervising Node starts it with --stdio. It owns
// in-process AgentSession instances (Pi 1.4.0 SDK) for managed Bridge
// sessions. At startup the Pi binary/version is checked safely as a
// deployment signal: a missing or unusable Pi means degraded health and
// session creation fails closed. Shutdown disposes owned sessions; only
// this process's own sessions are ever touched.
//
// Operational logging: one-line JSON records with `timestamp`, `level`,
// `component`, `event`, plus allowlisted bounded scalar fields only
// (see logging.mjs). WB_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR, default INFO)
// filters records before writing. In stdio mode every record goes to stderr;
// stdout carries protocol lines only. An invalid nonblank WB_LOG_LEVEL fails
// startup safely without exposing env values or paths.
import { PiAdapter } from "./adapter.mjs";
import { checkPiBinary, isAgentDirAllowed, loadConfig } from "./config.mjs";
import { createLogger } from "./logging.mjs";
import { applyLoginPathResult, resolveLoginPathSync } from "./login-path.mjs";
import { canonicalizeProjectsDir } from "./paths.mjs";
import { serveStdio } from "./stdio.mjs";

export function createLog(env = process.env) {
  // loadConfig validates WB_LOG_LEVEL; the logger then enforces it.
  const level = loadConfig(env).logLevel;
  return createLogger({ level });
}

export function buildMain(env = process.env, options = {}) {
  const config = loadConfig(env);
  const log = createLogger({ level: config.logLevel });
  // Resolve the terminal-equivalent executable search path once, before Pi
  // binary probing, so `pi --version` and every AgentSession bash tool
  // inherit the same PATH a normal terminal would see. Only PATH is
  // imported; all other shell output/environment is discarded. On failure
  // the inherited service PATH is kept. Resolver and target are injectable
  // so unit tests stay deterministic without spawning a real login shell.
  const resolveFn = typeof options.resolveLoginPathFn === "function"
    ? options.resolveLoginPathFn
    : resolveLoginPathSync;
  const targetEnv = options.loginPathTargetEnv || process.env;
  let loginPath = {
    resolved: false, path: null, shell: "/bin/sh",
    shellBasename: "sh", entryCount: 0, code: "spawn_error",
  };
  try {
    const result = resolveFn();
    if (result && typeof result === "object") loginPath = result;
  } catch {
    // Keep the inherited service PATH on resolver failure.
  }
  try {
    applyLoginPathResult(loginPath, targetEnv);
  } catch {
    // Applying must never break startup; fallback is the inherited PATH.
  }
  const checkFn = typeof options.checkPiBinaryFn === "function"
    ? options.checkPiBinaryFn
    : checkPiBinary;
  const piCheck = checkFn(config.piBinary);
  let projectsRoot = null;
  let projectsError = null;
  try {
    projectsRoot = canonicalizeProjectsDir(config.projectsDirRaw);
  } catch (error) {
    projectsError = error;
  }
  const agentDirOk = isAgentDirAllowed(config.agentDir);
  const usable = piCheck.usable && projectsRoot !== null && agentDirOk;
  return { config, piCheck, projectsRoot, projectsError, agentDirOk, usable, log, loginPath };
}

// `--stdio`: Agent Protocol v2 for a supervising Workspace Bridge Node. The
// real stdout carries protocol lines only; every other stdout write, logs
// included, is sent to stderr.
export function buildStdioAdapter(params, env = process.env) {
  const settings = params.settings && typeof params.settings === "object" ? params.settings : {};
  const merged = { ...env, WB_PI_PROJECTS_DIR: String(params.projectsRoot || "") };
  if (typeof settings.pi_binary === "string") merged.WB_PI_BINARY = settings.pi_binary;
  if (typeof settings.agent_dir === "string") merged.PI_CODING_AGENT_DIR = settings.agent_dir;
  if (typeof settings.log_level === "string") merged.WB_LOG_LEVEL = settings.log_level;
  const built = buildMain(merged);
  if (!built.projectsRoot) {
    throw built.projectsError || new Error("Pi projects root is unavailable");
  }
  if (!built.agentDirOk) throw new Error("Pi agent directory is not allowed");
  return new PiAdapter({
    projectsRoot: built.projectsRoot,
    agentDir: built.config.agentDir,
    piUsable: built.piCheck.usable && built.agentDirOk,
    piVersion: built.piCheck.version,
    onLog: built.log,
  });
}

const isEntry = typeof process.argv[1] === "string" && (
  process.argv[1].endsWith("main.mjs")
  || process.argv[1].endsWith("pi-adapter.mjs")
  || process.argv[1].endsWith("workspace-bridge-pi-adapter")
);

const stdioMode = isEntry && process.argv.includes("--stdio");

if (stdioMode) {
  const protocolWrite = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, ...rest) => process.stderr.write(chunk, ...rest);
  serveStdio({
    write: (line) => protocolWrite(`${line}\n`),
    buildAdapter: (params) => buildStdioAdapter(params),
    onLog: createLogger({ level: process.env.WB_LOG_LEVEL || "INFO" }),
  });
}

if (isEntry && !stdioMode) {
  process.stderr.write("usage: workspace-bridge-pi-adapter --stdio\n"
    + "Started by a Workspace Bridge Node as an agent plugin; there is no HTTP mode.\n");
  process.exit(2);
}
