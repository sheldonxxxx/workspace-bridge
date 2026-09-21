// Private adapter HTTP surface, exported as a factory so token enforcement can be
// tested without binding a port. The process is a client only: it connects to an
// externally managed native OpenCode server and never starts one.
import http from "node:http";
import { timingSafeEqual } from "node:crypto";

import { SdkError } from "./sdk-runtime.mjs";

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
  if (error instanceof SdkError) {
    return send(res, error.status, { error: error.message, code: error.code });
  }
  return send(res, 502, { error: "OpenCode runtime is unavailable" });
}

function readBody(req, bodyLimit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > bodyLimit) {
        reject(new SdkError("Request body too large", 413, "too_large"));
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
        reject(new SdkError("Invalid JSON body", 400, "invalid_json"));
      }
    });
    req.on("error", () => reject(new SdkError("Request stream failed", 400, "invalid_body")));
  });
}

function timingSafeEqualStrings(expected, provided) {
  const a = Buffer.from(expected);
  const b = Buffer.from(provided);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export function createAdapterServer({ runtime, hub, token, serverConfigured, instance,
                                     adapterVersion, bodyLimit = DEFAULT_BODY_LIMIT, onLog = null }) {
  const locked = !token;
  const configuredToken = typeof token === "string" ? token : "";

  function oplog(level, event, fields) {
    try {
      if (typeof onLog === "function") onLog(level, "adapter", event, fields);
    } catch {
      // Logging must never break request handling.
    }
  }

  // Fail closed: a missing WB_RUNTIME_TOKEN never means "allow".
  function authorized(req) {
    if (locked) return false;
    const header = req.headers["x-runtime-token"];
    const provided = typeof header === "string" ? header : "";
    return timingSafeEqualStrings(configuredToken, provided);
  }

  function requireRuntime(res) {
    if (!runtime) {
      send(res, 503, { error: "OpenCode server URL is not configured" });
      return false;
    }
    return true;
  }

  function healthPayload() {
    if (locked) {
      return {
        ok: false, error: "locked", locked: true, token_configured: false,
        server_configured: serverConfigured, adapter_version: adapterVersion,
        instance, cursor: hub ? hub.cursor : 0,
      };
    }
    return null;
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://adapter.local");
    const segments = url.pathname.split("/").filter(Boolean);
    try {
      if (url.pathname === "/health" && req.method === "GET") {
        const lockedHealth = healthPayload();
        if (lockedHealth) return send(res, 200, lockedHealth);
        const health = runtime ? await runtime.health()
          : { ok: false, error: "not_configured", server_configured: false };
        // Sanitized event-stream health only: status/transitions/timing,
        // never event contents or secrets.
        let eventStream = null;
        try {
          eventStream = hub && typeof hub.health === "function" ? hub.health() : null;
        } catch {
          eventStream = null;
        }
        return send(res, 200, {
          ...health, locked: false, token_configured: true,
          server_configured: serverConfigured, adapter_version: adapterVersion,
          instance, cursor: hub ? hub.cursor : 0,
          event_stream: eventStream,
        });
      }
      if (!authorized(req)) {
        return send(res, 401, {
          error: locked ? "Runtime token is not configured; adapter is locked" : "Invalid runtime token",
          code: locked ? "locked" : "unauthorized", locked,
        });
      }
      if (!requireRuntime(res)) return;

      if (url.pathname === "/models" && req.method === "GET") {
        // Global discovery: no workspace directory is required or used.
        return send(res, 200, { models: await runtime.listModels(), scope: "global" });
      }

      if (url.pathname === "/events" && req.method === "GET") {
        const cursor = Number(url.searchParams.get("cursor") || "0");
        const timeout = Math.max(0, Math.min(Number(url.searchParams.get("timeout") || "25"), 30)) * 1000;
        const result = await hub.poll(cursor, timeout);
        return send(res, 200, result);
      }

      if (segments[0] === "sessions") {
        const sessionId = segments[1];
        if (req.method === "POST" && segments.length === 1) {
          const body = await readBody(req, bodyLimit);
          const directory = String(body.directory || "");
          if (!directory) throw new SdkError("directory is required", 400, "rejected");
          const session = await runtime.createSession(directory, String(body.title || "Workspace Bridge run"));
          return send(res, 200, { session });
        }
        if (!sessionId) throw new SdkError("Unknown route", 404, "not_found");
        if (req.method === "GET" && segments.length === 2) {
          const directory = url.searchParams.get("directory") || "";
          const session = await runtime.getSession(directory, sessionId);
          if (!session) return send(res, 404, { error: "Session not found", code: "not_found" });
          return send(res, 200, { session });
        }
        if (req.method === "GET" && segments[2] === "messages") {
          const directory = url.searchParams.get("directory") || "";
          const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || "40"), 100));
          return send(res, 200, { messages: await runtime.messages(directory, sessionId, limit) });
        }
        if (req.method === "POST" && segments[2] === "prompt-async") {
          const body = await readBody(req, bodyLimit);
          const directory = String(body.directory || "");
          await runtime.promptAsync(directory, sessionId, String(body.text || ""), body.model || null);
          return send(res, 200, { accepted: true });
        }
        if (req.method === "POST" && segments[2] === "abort") {
          const body = await readBody(req, bodyLimit);
          const ok = await runtime.abortSession(String(body.directory || ""), sessionId);
          return send(res, 200, { ok });
        }
        if (req.method === "GET" && segments[2] === "status") {
          const directory = url.searchParams.get("directory") || "";
          return send(res, 200, { status: await runtime.sessionStatus(directory, sessionId) });
        }
        if (req.method === "GET" && segments[2] === "permissions" && segments.length === 3) {
          // Narrow, session-scoped pending-permission read for missed-ask
          // recovery. The runtime tries the V2 session-scoped snapshot
          // first (GET /api/session/{sessionID}/permission) and falls back
          // to the V1 global listing (GET /permission) only when V2 is
          // unavailable/unsupported. Errors propagate (fail closed); they
          // are never converted to an empty list.
          const directory = url.searchParams.get("directory") || "";
          try {
            const permissions = await runtime.listPendingPermissions(directory, sessionId);
            // Success: generation/source + session_id + matched count only,
            // never permission objects/patterns/resources/metadata.
            const source = runtime && typeof runtime.lastPermissionSource === "string"
              ? runtime.lastPermissionSource : null;
            oplog("INFO", "permission_list", {
              generation: source || undefined, source: source || undefined,
              session_id: sessionId, status: "ok",
              matched: Array.isArray(permissions) ? permissions.length : 0,
            });
            return send(res, 200, { permissions });
          } catch (error) {
            // Failure: sanitized status/code only, never raw error/body.
            const code = error && typeof error.code === "string" && error.code
              ? String(error.code).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 80)
              : "permission_list_failed";
            oplog("WARNING", "permission_list", {
              session_id: sessionId, status: "degraded", code,
            });
            throw error;
          }
        }
        if (req.method === "GET" && segments[2] === "questions" && segments.length === 3) {
          // Narrow, session-scoped pending-question read for missed-ask
          // recovery. The runtime tries the V2 session-scoped snapshot
          // first (GET /api/session/{sessionID}/question) and falls back
          // to the verified V1 global listing (GET /question, strictly
          // filtered by exact sessionID) only when V2 is
          // unavailable/unsupported. Errors propagate (fail closed); they
          // are never converted to an empty list.
          const directory = url.searchParams.get("directory") || "";
          try {
            const questions = await runtime.listPendingQuestions(directory, sessionId);
            // Success: generation/source + session_id + matched count only,
            // never question bodies/options/answers.
            const source = runtime && typeof runtime.lastQuestionSource === "string"
              ? runtime.lastQuestionSource : null;
            oplog("INFO", "question_list", {
              generation: source || undefined, source: source || undefined,
              session_id: sessionId, status: "ok",
              matched: Array.isArray(questions) ? questions.length : 0,
            });
            return send(res, 200, { questions, source });
          } catch (error) {
            const code = error && typeof error.code === "string" && error.code
              ? String(error.code).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 80)
              : "question_list_failed";
            oplog("WARNING", "question_list", {
              session_id: sessionId, status: "degraded", code,
            });
            throw error;
          }
        }
        if (req.method === "POST" && segments[2] === "permissions" && segments[3]) {
          const body = await readBody(req, bodyLimit);
          const decision = String(body.response || "");
          // Wire endpoint selection is explicit: the Bridge persists the
          // permission generation with the request and sends it back here.
          // "v2" uses POST /api/session/{sessionID}/permission/{requestID}/reply;
          // absent keeps the V1 reply route for backward compatibility. An
          // explicitly unknown generation fails closed. The generation is
          // never inferred from request-id formatting.
          const rawGeneration = body.generation;
          if (rawGeneration !== undefined && rawGeneration !== "v1" && rawGeneration !== "v2") {
            throw new SdkError("Unknown permission generation for reply routing", 400, "rejected");
          }
          const generation = rawGeneration === "v2" ? "v2" : "v1";
          const ok = await runtime.respondPermission(String(body.directory || ""), sessionId, segments[3],
                                                     decision, generation);
          // Forwarded reply: generation + session_id/request_id/decision only.
          oplog("INFO", "permission_event", {
            generation, session_id: sessionId, request_id: segments[3], decision,
            source: "reply",
          });
          return send(res, 200, { ok });
        }
      }
      return send(res, 404, { error: "Unknown route", code: "not_found" });
    } catch (error) {
      return fail(res, error);
    }
  });

  return server;
}
