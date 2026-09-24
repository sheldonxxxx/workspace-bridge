// In-process AgentSession transport for the Pi host adapter.
//
// Replaces the removed `pi --mode rpc` subprocess owner (rpc.mjs). Each
// Bridge session owns exactly one AgentSession created with Pi 0.87.0's
// direct Node SDK inside this process -- no child process, no stdio, no
// JSONL framing, and therefore no frame-size ceiling that can kill a
// session.
//
// Session creation is directory-bound and persisted: SessionManager files
// live under the isolated PI_CODING_AGENT_DIR session area (never the
// user's normal ~/.pi/agent tree, never in-memory), so session
// identity/history remain durable and future restart recovery is possible.
// A newly created Bridge session owns the exact created session id;
// continuation/recovery stays explicit and directory-bound.
//
// Resource/tool boundary (mirrors the old
// `--tools ... --no-approve --no-extensions -e <trusted> [-e <enabled>]`
// spawn contract):
// - cwd is the canonical workspace root; agentDir is the isolated profile.
// - SettingsManager is created with projectTrusted:false, the SDK
//   equivalent of --no-approve: project-local settings/extensions trust
//   can never widen the session.
// - DefaultResourceLoader runs with noExtensions/noSkills/
//   noPromptTemplates/noThemes/noContextFiles: no project or global
//   auto-discovery of extensions, skills, prompts, themes, or AGENTS.md
//   context files. Only additionalExtensionPaths (admin-enabled package
//   roots, resolved natively from logical IDs -- the Bridge never sends a
//   host path) plus the package-owned trusted permission extension loaded
//   as an inline factory with the per-session policy snapshot closed over
//   (no environment variable, so concurrent sessions never collide).
// - tools/excludeTools select the built-in loadout from the session
//   snapshot: read/grep/find/ls always, edit/write only when writable,
//   bash only when shell_mode != deny, powershell always excluded. With
//   enabled third-party packages no tools allowlist is passed (the SDK
//   allowlist would hide extension tools) and built-in exposure uses
//   excludeTools instead; the intended built-in set is then activated
//   explicitly with setActiveToolsByName alongside every registered
//   extension tool.
import path from "node:path";
import fs from "node:fs";

import {
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  createAgentSession,
} from "@earendil-works/pi-coding-agent";

import { createTrustedPermissionExtension } from "./trusted-permission-extension.mjs";

// Pinned SDK release this transport is built and verified against. The
// adapter package.json pins the same version exactly; the smoke test
// asserts they agree so a drifted install fails loudly instead of
// running against an unverified API surface.
export const SDK_VERSION = "0.87.0";
const SDK_EVENT_STALL_WARN_MS = 10_000;
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

// Built-in tool names known to the Pi SDK (createAllToolDefinitions).
// Everything else registered on a session is an extension/custom tool.
export const SDK_BUILTIN_TOOLS = new Set(
  ["read", "bash", "powershell", "edit", "write", "grep", "find", "ls"]);

// Session directory for a workspace under the isolated agent profile.
// Mirrors Pi's own default-session-dir encoding
// (`--<cwd without leading slash, /:\ mapped to ->--`) so layout stays
// recognizable, rooted at the isolated agentDir instead of ~/.pi/agent.
export function sessionDirFor(agentDir, cwd) {
  const resolved = path.resolve(cwd);
  const safe = `--${resolved.replace(/^[/\\]/, "").replace(/[/\\:]/g, "-")}--`;
  return path.join(agentDir, "sessions", safe);
}

// Settings + model/auth services for the isolated profile. projectTrusted
// is false (the SDK equivalent of --no-approve); auth.json/models.json
// resolve under the isolated agentDir, never the normal profile.
export async function createIsolatedServices({ cwd, agentDir }) {
  const settingsManager = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
  const modelRuntime = await ModelRuntime.create({
    authPath: path.join(agentDir, "auth.json"),
    modelsPath: path.join(agentDir, "models.json"),
  });
  return { settingsManager, modelRuntime };
}

// Create one owned AgentSession for a Bridge session. `policy` is the
// validated immutable permission snapshot (closed over by the trusted
// permission extension factory); `extensionPaths` are the resolved
// admin-enabled third-party package roots (may be empty); `tools` is the
// exact built-in allowlist for sessions without enabled extensions
// (null when extensions are enabled); `excludeTools` is the built-in
// denylist used with enabled extensions. `uiContext` is the adapter's
// permission-backed ExtensionUIContext; `onEvent` receives raw SDK agent
// events for audit/permission correlation. Returns the created session
// plus its services. Throws on any load failure (including an extension
// root that no longer resolves) -- enabled packages are never silently
// skipped.
export async function createSdkSession({
  cwd, agentDir, tools = null, excludeTools = null, extensionPaths = [],
  policy, uiContext, onEvent = null, onDiagnostic = null, resumeFile = null,
}) {
  const { settingsManager, modelRuntime } = await createIsolatedServices({ cwd, agentDir });
  const extra = Array.isArray(extensionPaths)
    ? extensionPaths.filter((p) => typeof p === "string" && p)
    : [];
  const loader = new DefaultResourceLoader({
    cwd,
    agentDir,
    settingsManager,
    additionalExtensionPaths: extra,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    extensionFactories: [
      {
        name: "trusted-permission",
        factory: createTrustedPermissionExtension({ policy, cwd }),
      },
    ],
  });
  await loader.reload();
  const errors = loader.getExtensions().errors;
  if (errors.length) {
    const detail = errors.map((e) => `${e.path}: ${e.error}`).join("; ").slice(0, 400);
    throw new Error(`Pi extension loading failed: ${detail}`);
  }
  const sessionDir = sessionDirFor(agentDir, cwd);
  if (resumeFile && (path.dirname(path.resolve(resumeFile)) !== sessionDir
      || !fs.lstatSync(resumeFile, { throwIfNoEntry: true })?.isFile()
      || fs.realpathSync(resumeFile) !== path.resolve(resumeFile))) {
    throw new Error("Pi session recovery path is invalid");
  }
  const sessionManager = resumeFile
    ? SessionManager.open(resumeFile, sessionDir, cwd)
    : SessionManager.create(cwd, sessionDir);
  const options = {
    cwd,
    agentDir,
    modelRuntime,
    resourceLoader: loader,
    sessionManager,
    settingsManager,
  };
  if (Array.isArray(tools) && tools.length) options.tools = [...tools];
  if (typeof excludeTools === "string" && excludeTools) {
    options.excludeTools = excludeTools.split(",").map((s) => s.trim()).filter(Boolean);
  }
  const { session } = await createAgentSession(options);
  await session.bindExtensions({ uiContext, mode: "rpc" });
  // With enabled extensions no tools allowlist is passed (it would hide
  // extension tools), so activate the intended built-in set explicitly
  // alongside every registered non-built-in tool. setActiveToolsByName
  // silently skips names the registry excluded, so excluded built-ins
  // can never be reactivated here.
  if (!options.tools && extra.length) {
    try {
      const names = session.getAllTools().map((t) => t && t.name).filter(Boolean);
      const extensionTools = names.filter((n) => !SDK_BUILTIN_TOOLS.has(n));
      const intended = new Set([...defaultBuiltinsForExclude(options.excludeTools), ...extensionTools]);
      session.setActiveToolsByName([...intended]);
    } catch {
      // Activation is loadout polish; the registry boundary above is the
      // enforcement point and already holds. Never fail creation here.
    }
  }
  // TEMPORARY ISSUE DIAGNOSTIC: remove this private-method wrapper after
  // the missing tool-completion issue is identified and fixed.
  // AgentSession 0.87.0 awaits extension event handlers before notifying
  // session.subscribe() listeners. Observe the boundary around that await
  // so a tool that has returned but is stuck in an extension event hook is
  // distinguishable from a tool whose execution is still pending. This is
  // intentionally pinned to the exact SDK version above; diagnostics carry
  // identifiers and counts only, never arguments or results.
  instrumentSdkEventDispatch(session, onDiagnostic);
  if (typeof onEvent === "function") {
    session.subscribe((event) => {
      if (isDiagnosticEvent(event)) {
        emitDiagnostic(onDiagnostic, diagnosticRecord(session, event, "session_subscriber"));
      }
      try {
        onEvent(event);
      } catch {
        // Listener errors never break the session.
      }
    });
  }
  return { session, modelRuntime, resourceLoader: loader, settingsManager, sessionManager };
}

function isDiagnosticEvent(event) {
  return event && (event.type === "tool_execution_start"
    || event.type === "tool_execution_end" || event.type === "agent_end");
}

function diagnosticRecord(session, event, stage, durationMs = undefined) {
  let pendingToolCount = null;
  try {
    const pending = session.agent?.state?.pendingToolCalls;
    if (pending && typeof pending.size === "number") pendingToolCount = pending.size;
  } catch { /* SDK state diagnostics must never affect tool execution */ }
  const record = {
    stage,
    sessionId: String(session.sessionId || "").slice(0, 200),
    eventType: String(event?.type || "").slice(0, 80),
    toolCallId: typeof event?.toolCallId === "string" ? event.toolCallId.slice(0, 200) : "",
    pendingToolCount,
  };
  if (Number.isFinite(durationMs)) record.durationMs = Math.max(0, Math.floor(durationMs));
  return record;
}

function emitDiagnostic(onDiagnostic, record) {
  if (typeof onDiagnostic !== "function") return;
  try { onDiagnostic(record); } catch { /* diagnostics never affect the session */ }
}

function instrumentSdkEventDispatch(session, onDiagnostic) {
  // `_emitExtensionEvent` is an AgentSession implementation detail. The
  // SDK is pinned, and this narrow wrapper is used only to expose whether
  // the awaited extension dispatch finishes before public subscribers run.
  const dispatch = session && session._emitExtensionEvent;
  if (typeof dispatch !== "function") return;
  session._emitExtensionEvent = async function (event) {
    if (!isDiagnosticEvent(event)) return dispatch.call(this, event);
    const startedAt = Date.now();
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      emitDiagnostic(onDiagnostic, diagnosticRecord(
        session, event, "extension_dispatch_stalled", Date.now() - startedAt));
    }, SDK_EVENT_STALL_WARN_MS);
    timer.unref?.();
    emitDiagnostic(onDiagnostic, diagnosticRecord(session, event, "extension_dispatch_start"));
    try {
      return await dispatch.call(this, event);
    } finally {
      settled = true;
      clearTimeout(timer);
      emitDiagnostic(onDiagnostic, diagnosticRecord(
        session, event, "extension_dispatch_end", Date.now() - startedAt));
    }
  };
}

// Built-in loadout implied by an excludeTools list: every SDK built-in
// except excluded and except powershell (never exposed on this adapter).
function defaultBuiltinsForExclude(excludeTools) {
  const excluded = new Set(Array.isArray(excludeTools) ? excludeTools : []);
  return [...SDK_BUILTIN_TOOLS]
    .filter((n) => n !== "powershell" && !excluded.has(n));
}

// Cached isolated-profile model runtimes, one per agentDir. Model/auth
// inventory is profile-global (not cwd-specific), matching the old
// short-lived discovery child. The snapshot read itself is synchronous;
// creation restores cached catalogs without network access unless the
// operator explicitly opts into a network refresh elsewhere.
const modelRuntimes = new Map();

export async function listSdkModels({ agentDir }) {
  const key = String(agentDir);
  let pending = modelRuntimes.get(key);
  if (!pending) {
    pending = ModelRuntime.create({
      authPath: path.join(key, "auth.json"),
      modelsPath: path.join(key, "models.json"),
    });
    modelRuntimes.set(key, pending);
    pending.catch(() => {
      if (modelRuntimes.get(key) === pending) modelRuntimes.delete(key);
    });
  }
  const runtime = await pending;
  return runtime.getAvailableSnapshot().map((m) => {
    const levelMap = m.thinkingLevelMap || {};
    const reasoningOptions = m.reasoning
      ? THINKING_LEVELS.filter((level) => levelMap[level] !== null
        && (level !== "xhigh" && level !== "max" || levelMap[level] !== undefined))
      : ["off"];
    return {
      provider: String(m.provider ?? ""),
      id: String(m.id ?? ""),
      name: String(m.name ?? m.id ?? ""),
      reasoningOptions,
    };
  });
}
