# Pi host adapter

Native host package that owns Pi agent sessions for Workspace Bridge. It is
not a Compose service and never runs in Docker. The npm package
`workspace-bridge-pi-host-adapter` ships the stable
`workspace-bridge-pi-adapter` executable (`pi-adapter.mjs` via the `bin`
entry); `workspace-bridge adapter` owns the service lifecycle and there is
no npm-managed service installer. The Bridge and this adapter communicate
only through Runtime Protocol v1.

For runtime behavior, profiles, and troubleshooting see
[Runtimes](../../docs/RUNTIMES.md). For repo checks see
[Contributing](../../CONTRIBUTING.md).

## Topology

```text
Bridge control plane
  -> host adapter on this host (X-Runtime-Token, Runtime Protocol v1)
    -> in-process Pi session (isolated agent dir, pinned Pi dependency)
```

The adapter owns its Pi conversations, runs, profiles, interaction requests,
and activity snapshots. It never adopts an unrelated Pi session. Workspace
Bridge owns run authorization and durable cross-runtime records; Pi owns
native session files, model syntax, and enforcement.

## Profiles and host authority

The built-in read-only profile allows read tools only. Broader profiles
enable edit and write tools and ask before shell commands. Custom Pi
profiles are managed through the local Bridge manager and apply to new
conversations; an existing conversation keeps its immutable profile
revision. The trusted permission extension validates each supported file
and shell action against that profile.

Pi runs with the host user's authority. These controls are pre-tool policy,
not an OS sandbox. Review external paths, protected paths, writable tools,
and shell mode before enabling a profile.

## Runtime Protocol surface

The token-authenticated HTTP API is limited to `/v1/*` resources:
descriptor, models, profiles, conversations, runs, interactions, and
activities. `/health` is a bounded unauthenticated readiness check. Every
other operational route requires the runtime token header; an empty token
locks the adapter fail-closed.

A conversation is bound to a canonical project directory, model, and
security profile revision. A run starts only in an idle owned conversation.
On adapter restart, an in-progress run is marked interrupted; the Bridge
never replays the prompt or adopts an unrelated session.

Native session retry uses a bounded budget with exponential backoff;
quota and billing errors fail promptly without consuming it. Retry state is
observable through sanitized records containing only safe scalar fields.

## Run locally

```sh
cd runtime/pi-host-adapter
npm install
WB_RUNTIME_TOKEN="shared-secret" \
WB_PI_PROJECTS_DIR="$HOME/Projects" \
node main.mjs
```

| Variable | Default | Notes |
|---|---|---|
| `WB_PI_ADAPTER_HOST` | `127.0.0.1` | Keep loopback. |
| `WB_PI_ADAPTER_PORT` | `8780` | |
| `WB_PI_BINARY` | `pi` | Deployment signal only; sessions run in-process. |
| `PI_CODING_AGENT_DIR` | `$HOME/.pi/workspace-bridge` | Isolated agent dir; never the normal agent tree. |
| `WB_PI_PROJECTS_DIR` (`WB_PROJECTS_DIR` fallback) | required | Canonicalized; must exist. Every session dir must resolve beneath it. |
| `WB_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. Set independently from the Bridge. |

## Smoke

```sh
curl -s http://127.0.0.1:8780/health
curl -s -H "X-Runtime-Token: $WB_RUNTIME_TOKEN" \
  http://127.0.0.1:8780/v1/descriptor
curl -s -H "X-Runtime-Token: $WB_RUNTIME_TOKEN" \
  http://127.0.0.1:8780/v1/models
curl -s -H "X-Runtime-Token: $WB_RUNTIME_TOKEN" \
  http://127.0.0.1:8780/v1/profiles
```

## Supported lifecycle

Install the package, then let `workspace-bridge adapter` own the service:

```sh
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" init \
  --runtime pi --projects-root "$HOME/Projects" --port 8780
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service status
```

`init` prints the one-time runtime token: register it as an adapter
instance on the owning Node. Service artifacts contain only the stable
launcher, never the token or projects root. After every manual package
update, explicitly restart the service; there is no auto-updater.

## Tests

```sh
npm test
```

Node test runner with an in-process fake transport; no network, provider,
or model calls.
