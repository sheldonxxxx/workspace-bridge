// Narrow token-authenticated Runtime Protocol v1 surface for the Pi host.
// Health is a bounded unauthenticated diagnostic; all /v1 routes require the
// shared runtime token. No arbitrary command, shell, spawn, or question route.
import http from "node:http";
import { timingSafeEqual } from "node:crypto";

import { AdapterError } from "./adapter.mjs";
import { enforcementFingerprint } from "./fingerprint.mjs";
import { sanitizedErrorCode } from "./logging.mjs";
import { PathError } from "./paths.mjs";
import { PiRuntimeProtocol } from "./wbrp.mjs";

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
  let protocol = null;

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
    // Bounded readiness/version only; never roots, full paths, or raw policy.
    // Runtime Protocol capabilities are reported by /v1/descriptor.
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
      pi_usable: piUsable,
      ...(piVersion ? { pi_version: piVersion } : {}),
      projects_configured: Boolean(adapter && adapter.projectsRoot),
      adapter_version: adapterVersion,
      instance,
      runtime: "pi",
      protocol: 1,
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

      if (segments[0] === "v1") {
        if (!protocol) protocol = new PiRuntimeProtocol(adapter);
        let result;
        const key = (index) => decodeURIComponent(segments[index] || "");
        if (url.pathname === "/v1/descriptor" && req.method === "GET") {
          result = protocol.descriptor();
        } else if (url.pathname === "/v1/models" && req.method === "GET") {
          result = await protocol.models();
        } else if (url.pathname === "/v1/profiles" && req.method === "GET") {
          result = protocol.profileList();
        } else if (url.pathname === "/v1/profiles" && req.method === "POST") {
          result = protocol.saveProfile(await readBody(req, bodyLimit));
        } else if (segments.length === 3 && segments[1] === "profiles"
            && req.method === "DELETE") {
          result = protocol.deleteProfile(key(2));
        } else if (url.pathname === "/v1/conversations" && req.method === "POST") {
          result = await protocol.createConversation(await readBody(req, bodyLimit));
        } else if (segments.length === 3 && segments[1] === "conversations"
            && req.method === "GET") {
          result = await protocol.conversation(key(2));
        } else if (segments.length === 4 && segments[1] === "conversations"
            && segments[3] === "security" && req.method === "POST") {
          const body = await readBody(req, bodyLimit);
          if (!body || typeof body.securityBinding !== "object") {
            throw new AdapterError("Invalid security rebind binding", 400, "invalid_arguments");
          }
          result = await protocol.rebindConversation(key(2), body.securityBinding);
        } else if (segments.length === 4 && segments[1] === "conversations"
            && segments[3] === "runs" && req.method === "POST") {
          result = await protocol.startRun(key(2), await readBody(req, bodyLimit));
        } else if (segments.length === 5 && segments[1] === "conversations"
            && segments[3] === "runs" && req.method === "GET") {
          result = await protocol.findRun(key(2), key(4));
        } else if (segments.length === 3 && segments[1] === "runs"
            && req.method === "GET") {
          result = await protocol.run(key(2));
        } else if (segments.length === 4 && segments[1] === "runs"
            && segments[3] === "cancel" && req.method === "POST") {
          result = await protocol.cancel(key(2));
        } else if (segments.length === 4 && segments[1] === "runs"
            && segments[3] === "interactions" && req.method === "GET") {
          result = await protocol.interactions(key(2));
        } else if (segments.length === 4 && segments[1] === "runs"
            && segments[3] === "activities" && req.method === "GET") {
          result = await protocol.activityList(key(2));
        } else if (segments.length === 4 && segments[1] === "interactions"
            && segments[3] === "resolve" && req.method === "POST") {
          result = await protocol.resolve(key(2), await readBody(req, bodyLimit));
        } else if (segments.length === 3 && segments[1] === "activities"
            && req.method === "GET") {
          result = protocol.activity(key(2));
        } else {
          return send(res, 404, { error: "Unknown route", code: "not_found" });
        }
        return send(res, 200, result);
      }

      return send(res, 404, { error: "Unknown route", code: "not_found" });
    } catch (error) {
      if (error instanceof AdapterError || error instanceof PathError) {
        if (error.code !== "runtime_unavailable" && error.code !== "unavailable") {
          // Bounded adapter/path/policy rejections worth operator attention.
          // Never log directory/title/model prompt/tool args or token values.
          log("WARNING", "request_rejected", { code: String(error.code || "rejected").slice(0, 80) });
        }
        // Known recoverable unavailability stays unlogged here to avoid
        // duplicate poll noise handled/throttled by the Bridge.
      } else {
        // Unexpected exception: one sanitized ERROR record with a
        // type-derived code only. Never error.message, URL/path,
        // directory, model, body, token, tool args/results, or stacks.
        log("ERROR", "request_error", { code: sanitizedErrorCode(error) });
      }
      return fail(res, error);
    }
  });

  return server;
}
