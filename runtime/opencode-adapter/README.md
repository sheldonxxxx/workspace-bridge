# OpenCode SDK adapter (Workspace Bridge)

This is a **private, client-only sidecar** for Workspace Bridge. It imports
`@opencode-ai/sdk` (and its public `v2` namespace for the pending-permission
listing) and connects to an OpenCode server that the user runs and
manages **natively on the host**. It:

- never calls `createOpencode()`/`createOpencodeServer()`,
- never publishes a host port,
- exposes only a narrow, token-authenticated protocol on the private Compose
  network: health, model list, session create/get, prompt-async, bounded message
  read, pending-permission/question reads, permission reply and abort.

Provider credentials and the OpenCode server URL live only in this process's
local runtime environment. They are never returned to Workspace Bridge, MCP,
the manager, events or logs.

## Protocol (all JSON)

| Method | Path | Body / query | Result |
| --- | --- | --- | --- |
| GET | `/health` | – | `{ok, version, adapter_version, server_configured, locked, token_configured, instance, cursor, event_stream:{status,transitions,lastTransition,consecutiveFailures,rawEventCount,controlEventCount,functionalEventCount,lastRawEventAt,lastFunctionalEventAt}}` (sanitized subscription health plus bounded raw/control/functional frame counters; no event contents or secrets) |
| GET | `/models` | `directory` | `{models:[{provider,model,selector,name,default}]}` |
| POST | `/sessions` | `{directory,title}` | `{session:{id,directory,title}}` |
| GET | `/sessions/:id` | `directory` | `{session}` / 404 |
| POST | `/sessions/:id/prompt-async` | `{directory,text,model?}` | `{accepted:true}` |
| GET | `/sessions/:id/messages` | `directory,limit` | `{messages:[...]}` |
| GET | `/sessions/:id/permissions` | `directory` | `{permissions:[...],source:"v1"\|"v2"}` (exactly this session; V2 session-scoped snapshot `GET /api/session/{sessionID}/permission` is primary, legacy `GET /permission` is fallback only when V2 is unavailable/unsupported; a successful empty snapshot never falls back; errors never become `[]`) |
| POST | `/sessions/:id/permissions/:permissionID` | `{directory,response,generation?}` | `{ok}` (`generation:"v2"` replies via `POST /api/session/{sessionID}/permission/{requestID}/reply`; anything else keeps the V1 reply route; never inferred from request-id formatting) |
| GET | `/sessions/:id/questions` | `directory` | `{questions:[...],source:"v2"\|"v1"}` (exactly this session; V2 session-scoped snapshot `GET /api/session/{sessionID}/question` is primary, verified V1 global listing `GET /question` is fallback only when V2 is unavailable/unsupported and is strictly filtered by exact `sessionID`; a successful empty V2 snapshot never falls back; errors never become `[]`; question bodies/options/answers never leave the adapter) |
| POST | `/sessions/:id/abort` | `{directory}` | `{ok}` |
| GET | `/events` | `cursor,timeout` | `{events:[...],cursor}` |

`response` is one of `once`, `always`, `reject` and is forwarded unchanged.
`always` approves OpenCode's own proposed scope (V1 `always`, V2 `save`,
surfaced as `pattern`); this adapter never rewrites, broadens or synthesizes
that scope. The requested target (V1 `patterns`, V2 `resources`) is kept
separately reviewable as `requested_patterns`.

Permission generations (verified against installed `@opencode-ai/sdk`
1.18.31): V1 is the legacy `permission.asked`/`permission.replied` event pair
with the global `GET /permission` listing and the
`POST /session/{id}/permissions/{permissionID}` reply route. V2 is the
current `permission.v2.asked`/`permission.v2.replied` event pair
(`PermissionV2Request`: `{id, sessionID, action, resources, save?, metadata?,
source?}`) with the session-scoped `GET /api/session/{sessionID}/permission`
snapshot (`client.v2.session.permission.list`) and the session-scoped
`POST /api/session/{sessionID}/permission/{requestID}/reply` reply route
(`client.v2.session.permission.reply`, body `{reply}`). Every normalized
permission carries `generation: "v1"|"v2"`; the Bridge persists it and the
reply path routes on it verbatim. V2 `save` absent means `pattern: []`, so
`always` fails closed. Operational logs carry generation/source plus
session/request ids and matched counts only — never paths, scopes, resources
or metadata.

The 0.1.5/0.1.6 V1 listing was healthy but was the wrong generation for
current TUI requests: live logs showed repeated successful `matched=0`
resyncs while a real V2 `external_directory` ask was pending, because the
Bridge listened, listed and replied only on V1 surfaces. V2 is now primary;
V1 remains strictly as compatibility fallback/event alias. A V2 snapshot that
succeeds (even empty) never consults V1; V1 is used only when V2 is
unavailable/unsupported (missing method, 404/501, or the client's explicit
"not supported by this version"). Listing/binding failures propagate and the
Bridge surfaces them as non-terminal degraded diagnostics; the run stays
active with `pending_request_count=0`, and absence from a successful listing
never resolves a persisted wait. Child/subagent permission asks carry the
child session ID and are never attached to a root run without a verified
public parent relationship. This adapter never scrapes TUI output, reads
internal state files, calls private endpoints, or infers scopes from shell
text. The event stream exposes only sanitized subscription health; reconnect
never advances past buffered events, and gaps across a disconnected upstream
stream cannot be replayed (the installed SDK subscribe takes no
cursor/last-event-id).

## Environment

- `WB_OPENCODE_SERVER_URL` (required) — e.g. `http://host.docker.internal:4096`.
- `WB_OPENCODE_SERVER_USERNAME` / `WB_OPENCODE_SERVER_PASSWORD` — Basic Auth for
  the host server (set `OPENCODE_SERVER_PASSWORD` when starting OpenCode).
- `WB_RUNTIME_TOKEN` — shared private token required by every operational endpoint.
  **When unset the adapter is locked**: every endpoint except `/health` returns 401,
  which is the fail-closed default. `/health` reports `locked`/`token_configured`
  booleans and never the token.
- `WB_ADAPTER_PORT` (default `8770`), `WB_ADAPTER_HOST` (default `0.0.0.0`).

## Tests

```
npm ci
npm test
```

Tests inject a fake SDK client and make no network calls.
