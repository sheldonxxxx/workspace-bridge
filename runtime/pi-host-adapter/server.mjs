// Narrow token-authenticated HTTP surface for the Pi host adapter (3C1).
//
// Endpoints:
//   GET  /health (readable without a token; booleans/version/status only)
//   GET  /models?directory=<workspace>
//   POST /sessions {directory, title}
//   GET  /sessions/:id?directory=...
//   GET  /sessions/:id/status?directory=...
//   POST /sessions/:id/prompt-async {directory, text, model?}
//   GET  /sessions/:id/messages?directory=...&limit=...
//   POST /sessions/:id/abort {directory}
//   GET  /sessions/:id/permissions?directory=...
//   POST /sessions/:id/permissions/:permissionId/respond {directory, response}
//   GET  /sessions/:id/executions?directory=...&after=<seq>&limit=<n>
//
// There is no arbitrary command, shell, spawn, or question endpoint;
// Bridge polls messages/status/permissions plus the execution journal.
// Execution evidence never lands in ops logs or general status.
import http from "node:http";
import { timingSafeEqual } from "node:crypto";

import { AdapterError } from "./adapter.mjs";
import { enforcementFingerprint } from "./fingerprint.mjs";
import { PathError } from "./paths.mjs";
import { RpcError } from "./rpc.mjs";

const DEFAULT_BODY_LIMIT = 256 * 1024;

function send(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function fail(res, error) {
  if (error instanceof AdapterError) {
    return send(res, error.status, { error: error.message, code: error.code });
  }
  if (error instanceof PathError) {
    return send(res, error.status, { error: error.message, code: error.code });
  }
  if (error instanceof RpcError) {
    return send(res, error.status, { error: "Pi runtime is unavailable", code: error.code });
  }
  return send(res, 502, { error: "Pi runtime is unavailable", code: "runtime_unavailable" });
}

function readBody(req, bodyLimit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > bodyLimit) {
        reject(new AdapterError("Request body too large", 413, "too_large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (!chunks.length) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        reject(new AdapterError("Invalid JSON body", 400, "invalid_json"));
      }
    });
    req.on("error", () => reject(new AdapterError("Request stream failed", 400, "invalid_body")));
  });
}

function timingSafeEqualStrings(expected, provided) {
  const a = Buffer.from(expected);
  const b = Buffer.from(provided);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export function createPiAdapterServer({ adapter, token, adapterVersion, instance,
                                        piUsable = true, piVersion = "",
                                        bodyLimit = DEFAULT_BODY_LIMIT, onLog = null }) {
  const locked = !token;
  const configuredToken = typeof token === "string" ? token : "";

  function log(level, event, fields) {
    try {
      if (typeof onLog === "function") onLog(level, "pi-adapter", event, fields);
    } catch {
      // Logging must never break request handling.
    }
  }

  function authorized(req) {
    if (locked) return false;
    const header = req.headers["x-runtime-token"];
    const provided = typeof header === "string" ? header : "";
    return timingSafeEqualStrings(configuredToken, provided);
  }

  function healthPayload() {
    // Bounded booleans/version/status only; never roots, full paths, or
    // raw policy. The capabilities block advertises the 3C1 permission +
    // execution-history surface so Bridge can refuse to treat old adapters
    // as managed. Enforcement fingerprint identifies the Pi enforcement
    // build without blocking normal project edits (no full paths).
    const ok = !locked && piUsable;
    let fingerprint = "";
    try {
      fingerprint = enforcementFingerprint().fingerprint || "";
    } catch {
      fingerprint = "";
    }
    return {
      ok,
      status: locked ? "locked" : (piUsable ? "ok" : "degraded"),
      locked,
      token_configured: !locked,
      pi_configured: true,
      pi_usable: piUsable,
      ...(piVersion ? { pi_version: piVersion } : {}),
      projects_configured: Boolean(adapter && adapter.projectsRoot),
      adapter_version: adapterVersion,
      instance,
      sessions: adapter ? adapter.sessionCount : 0,
      capabilities: {
        pending_snapshot: true, permission_response: true,
        execution_history: true,
      },
      ...(fingerprint ? { enforcement_fingerprint: fingerprint } : {}),
    };
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://pi-adapter.local");
    const segments = url.pathname.split("/").filter(Boolean);
    try {
      if (url.pathname === "/health" && req.method === "GET") {
        return send(res, 200, healthPayload());
      }
      if (!authorized(req)) {
        return send(res, 401, {
          error: locked ? "Runtime token is not configured; adapter is locked" : "Invalid runtime token",
          code: locked ? "locked" : "unauthorized", locked,
        });
      }
      if (!adapter) {
        return send(res, 503, { error: "Pi projects parent is not configured", code: "not_configured" });
      }

      if (url.pathname === "/models" && req.method === "GET") {
        const directory = url.searchParams.get("directory") || "";
        const models = await adapter.listModels(directory);
        return send(res, 200, { models, scope: "global" });
      }

      if (segments[0] === "sessions") {
        const sessionId = segments[1] ? decodeURIComponent(segments[1]) : "";
        if (req.method === "POST" && segments.length === 1) {
          const body = await readBody(req, bodyLimit);
          const directory = String(body.directory || "");
          // Bounded session options: exactly the Bridge-owned permission
          // policy snapshot fields cross this boundary; arbitrary body
          // fields are never forwarded.
          const options = {};
          if (body.permission_policy !== undefined) {
            options.permission_policy = body.permission_policy;
          }
          if (body.policy_revision !== undefined) {
            options.policy_revision = body.policy_revision;
          }
          const session = await adapter.createSession(
            directory, String(body.title || "Workspace Bridge run"), options);
          log("INFO", "session_create", { status: "ok" });
          return send(res, 200, { session });
        }
        if (!sessionId) return send(res, 404, { error: "Unknown route", code: "not_found" });
        if (req.method === "GET" && segments.length === 2) {
          const directory = url.searchParams.get("directory") || "";
          const result = await adapter.getSession(directory, sessionId);
          if (!result) return send(res, 404, { error: "Session not found", code: "not_found" });
          return send(res, 200, result);
        }
        if (req.method === "GET" && segments[2] === "status" && segments.length === 3) {
          const directory = url.searchParams.get("directory") || "";
          const status = await adapter.sessionStatus(directory, sessionId);
          return send(res, 200, { status });
        }
        if (req.method === "GET" && segments[2] === "messages" && segments.length === 3) {
          const directory = url.searchParams.get("directory") || "";
          const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || "40"), 100));
          const messages = await adapter.messages(directory, sessionId, limit);
          return send(res, 200, { messages });
        }
        if (req.method === "POST" && segments[2] === "prompt-async" && segments.length === 3) {
          const body = await readBody(req, bodyLimit);
          const directory = String(body.directory || "");
          const result = await adapter.promptAsync(
            directory, sessionId, String(body.text || ""),
            body.model === undefined ? null : body.model,
          );
          return send(res, 200, result);
        }
        if (req.method === "POST" && segments[2] === "abort" && segments.length === 3) {
          const body = await readBody(req, bodyLimit);
          const ok = await adapter.abortSession(String(body.directory || ""), sessionId);
          return send(res, 200, { ok });
        }
        // 3C1 exact-session permission surface (authenticated only).
        // Pending records carry bounded targets; bash carries the exact
        // bounded command + verified timeout detail.
        if (req.method === "GET" && segments[2] === "permissions" && segments.length === 3) {
          const directory = url.searchParams.get("directory") || "";
          const permissions = await adapter.listPermissions(directory, sessionId);
          return send(res, 200, { permissions, source: "v1" });
        }
        if (req.method === "POST" && segments[2] === "permissions"
            && segments[4] === "respond" && segments.length === 5) {
          const permissionId = decodeURIComponent(segments[3] || "");
          const body = await readBody(req, bodyLimit);
          const result = await adapter.respondPermission(
            String(body.directory || ""), sessionId, permissionId,
            String(body.response || ""));
          return send(res, 200, result);
        }
        // 3C1 exact-session execution journal (authenticated only).
        // Bounded normalized updates plus next/head/oldest evidence.
        // Evicted history reports audit_gap/cursor_too_old, never silent
        // completeness. Never lands in ops logs or general status.
        if (req.method === "GET" && segments[2] === "executions" && segments.length === 3) {
          const directory = url.searchParams.get("directory") || "";
          const after = Math.max(0, Number(url.searchParams.get("after") || "0") || 0);
          const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || "50") || 50, 100));
          const result = await adapter.readExecutions(directory, sessionId, { after, limit });
          return send(res, 200, result);
        }
      }
      return send(res, 404, { error: "Unknown route", code: "not_found" });
    } catch (error) {
      if ((error instanceof AdapterError || error instanceof PathError) && error.code !== "runtime_unavailable" && error.code !== "unavailable") {
        log("INFO", "request_rejected", { code: error.code });
      }
      return fail(res, error);
    }
  });

  return server;
}
