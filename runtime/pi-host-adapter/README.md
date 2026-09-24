# Pi Runtime Protocol adapter (native macOS)

A private macOS host adapter that owns Pi AgentSession instances for Workspace
Bridge. It is not a Compose service and never runs in Docker. The Bridge and Pi
host communicate only through Runtime Protocol v1; the old direct session,
permission, execution, model, and extension HTTP API has been removed.

## Topology

```text
Docker Workspace Bridge
  -> http://host.docker.internal:8780 (X-Runtime-Token, Runtime Protocol v1)
    -> native Pi host adapter (this component, normal macOS user)
      -> in-process AgentSession (@earendil-works/pi-coding-agent 0.87.0)
        -> macOS Xcode/CoreML/MLX/Metal toolchain
```

The adapter owns its Pi conversations, runs, profiles, interaction requests,
and activity snapshots. It never adopts a Pi TUI session. It does not run a
`pi --mode rpc` subprocess. Workspace Bridge owns run authorization and durable
cross-runtime records; Pi owns native session files, model syntax, and enforcement.

## Profiles and host authority

The built-in `read-only` profile allows read tools only. The
`workspace-write-reviewed` profile enables edit/write tools and asks before shell
commands. Custom Pi profiles are managed through the local Bridge manager and
apply to new conversations; an existing conversation keeps its immutable profile
revision. The trusted permission extension validates each supported file and shell
action against that profile.

Pi runs with the macOS user's authority. These controls are pre-tool policy, not an
OS sandbox. Review external paths, protected paths, writable tools, and shell mode
before enabling a profile. Third-party extensions with host authority are not
exposed for management by the Bridge adapter.

## Runtime Protocol surface

The token-authenticated HTTP API is limited to `/v1/*` resources: descriptor,
models, profiles, conversations, runs, interactions, and activities. `/health`
remains a bounded unauthenticated readiness check. The adapter advertises the
protocol and implemented features through `GET /v1/descriptor`. Every other
operational route requires `X-Runtime-Token: <WB_RUNTIME_TOKEN>`; an empty token
locks the adapter fail-closed.

A conversation is bound to a canonical project directory, model, and security
profile revision. A run starts only in an idle owned conversation. Run snapshots
report active state, terminal outcome, current interactions, and bounded activity
records. On adapter restart, an in-progress run is marked `interrupted`; Bridge
never replays the prompt or adopts an unrelated Pi session.

## Temporary SDK event diagnostics

Remove this instrumentation and section after the missing tool-completion
issue is identified and fixed.

The adapter emits structured `sdk_tool_event_trace` records for tool
start/end delivery and terminal agent events. Records contain only the
session ID, tool-call ID, event type, stage, pending-tool count, journal
state/cursor, and dispatch duration; tool arguments and results are never
logged. Routine stages (`extension_dispatch_start/end`, `session_subscriber`,
`adapter_received`, `journal_started/completed`) are DEBUG, so default INFO
launchd logs stay quiet; `extension_dispatch_stalled`, `adapter_event_error`,
`adapter_event_unusable`, and `journal_missing_start` are WARNING. Use
`WB_LOG_LEVEL=DEBUG` temporarily to trace a stalled tool call.
`extension_dispatch_stalled` is a warning emitted after 10 seconds
when Pi has not finished awaiting extension handlers for that event.

For a stalled tool call, compare the stages for its call ID:

- A start with a positive `pending_tool_count` and no end dispatch means
  Pi has not finished the tool or its pre/post-tool hook.
- An end dispatch with `pending_tool_count: 0` but no dispatch end or
  `session_subscriber` points to an awaited extension event handler.
- `adapter_received` followed by `journal_missing_start` means the adapter
  received an end without a journal start for that call ID.
- `journal_completed` includes the resulting journal state and update
  cursor, which can be compared with Bridge execution-audit persistence.

## Run

```sh
cd runtime/pi-host-adapter
npm install
WB_RUNTIME_TOKEN="shared-secret" \
WB_PI_PROJECTS_DIR="$HOME/Projects" \
node main.mjs
```

Optional environment:

| Variable | Default | Notes |
|---|---|---|
| `WB_PI_ADAPTER_HOST` | `127.0.0.1` | Keep loopback; broad binds are not the default. |
| `WB_PI_ADAPTER_PORT` | `8780` | |
| `WB_PI_BINARY` | `pi` | Deployment signal only (checked for health); sessions run in-process and never spawn it. Never installed or upgraded by the adapter. |
| `PI_CODING_AGENT_DIR` | `$HOME/.pi/workspace-bridge` | Isolated agent dir; `~`, `$HOME`, `${HOME}` prefixes expanded. Never the normal `~/.pi/agent` tree. |
| `WB_PI_PROJECTS_DIR` (`WB_PROJECTS_DIR` fallback) | required | Canonicalized; must exist. Every session dir must realpath beneath it. |
| `WB_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`; same semantics as the Docker Bridge. Invalid nonblank value fails adapter startup safely. Set independently from the Bridge (Compose does not configure the LaunchAgent). |

`GET /health` is readable without a token and exposes bounded readiness,
protocol, and version fields. Every other endpoint requires
`X-Runtime-Token: <WB_RUNTIME_TOKEN>`; an empty token locks the adapter
fail-closed.

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

Docker-to-host connectivity (from inside the Bridge container):

```sh
curl -s http://host.docker.internal:8780/health
```

`compose.yaml` is deliberately untouched: this adapter must never become a
Compose service because it needs the native macOS toolchain.

## Start after login with launchd

Install a copy of this adapter under
`~/Library/Application Support/workspace-bridge/pi-host-adapter` before loading
the LaunchAgent. A LaunchAgent starts when this macOS user logs in after a
reboot and restarts the process if it exits. It does not run before login.
Keeping the Node entrypoint under the user's home directory avoids startup
access problems when the source checkout is on an external volume.

```sh
mkdir -p "$HOME/Library/Application Support/workspace-bridge/pi-host-adapter"
rsync -a --exclude '/test/' --exclude '/launchd/' --exclude '/README.md' \
  runtime/pi-host-adapter/ \
  "$HOME/Library/Application Support/workspace-bridge/pi-host-adapter/"
```

Copy `launchd/com.workspace-bridge.pi-host-adapter.plist` to
`~/Library/LaunchAgents/`. Replace every placeholder with the absolute home,
project parent, Node and Pi paths, and the shared `WB_RUNTIME_TOKEN` value.
Keep the plist private (`chmod 600`) because it contains the token. Then load
and inspect the service:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.workspace-bridge.pi-host-adapter.plist"
curl -s http://127.0.0.1:8780/health
```

After updating adapter source, stop the LaunchAgent, sync the installed copy,
and load it again. Check that no managed sessions are active before restarting:

```sh
launchctl bootout "gui/$(id -u)/com.workspace-bridge.pi-host-adapter"
# Run the rsync command above.
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.workspace-bridge.pi-host-adapter.plist"
```

## Tests

```sh
npm test
```

Node test runner with an in-process fake SDK transport; no network,
provider, or model calls. A provider-free real-SDK smoke runs in the
normal suite: it creates/disposes a real AgentSession in an isolated
temporary profile (verifying conversation persistence, model discovery,
the 0.87.0 dependency pin, and suppressed project auto-discovery) without sending
an LLM prompt. An optional real-profile check
(`WB_REAL_PROFILE_CHECK=1 npm test`) additionally validates the real
isolated-profile package path and loads its package root in-process.
