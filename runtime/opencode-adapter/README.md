# OpenCode SDK adapter (Workspace Bridge)

This is a **private, client-only sidecar** for Workspace Bridge. It imports
`@opencode-ai/sdk` and connects to an OpenCode server that the user runs and
manages **natively on the host**. It:

- never calls `createOpencode()`/`createOpencodeServer()`,
- never publishes a host port,
- exposes only a narrow, token-authenticated protocol on the private Compose
  network: health, model list, session create/get, prompt-async, bounded message
  read, permission reply and abort.

Provider credentials and the OpenCode server URL live only in this process's
local runtime environment. They are never returned to Workspace Bridge, MCP,
the manager, events or logs.

## Protocol (all JSON)

| Method | Path | Body / query | Result |
| --- | --- | --- | --- |
| GET | `/health` | – | `{ok, version, adapter_version, server_configured, locked, token_configured, instance, cursor}` |
| GET | `/models` | `directory` | `{models:[{provider,model,selector,name,default}]}` |
| POST | `/sessions` | `{directory,title}` | `{session:{id,directory,title}}` |
| GET | `/sessions/:id` | `directory` | `{session}` / 404 |
| POST | `/sessions/:id/prompt-async` | `{directory,text,model?}` | `{accepted:true}` |
| GET | `/sessions/:id/messages` | `directory,limit` | `{messages:[...]}` |
| POST | `/sessions/:id/permissions/:permissionID` | `{directory,response}` | `{ok}` |
| POST | `/sessions/:id/abort` | `{directory}` | `{ok}` |
| GET | `/events` | `cursor,timeout` | `{events:[...],cursor}` |

`response` is one of `once`, `always`, `reject` and is forwarded unchanged.
`always` approves OpenCode's own proposed `always` scope (surfaced as `pattern`);
this adapter never rewrites, broadens or synthesizes that scope. The requested
target (`patterns`) is kept separately reviewable.

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
