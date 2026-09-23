# Pi host adapter (native macOS only)

A native macOS host adapter that owns in-process Pi AgentSession instances
(Pi 0.87.0 SDK) for Workspace Bridge. It is intentionally **not** a Compose
service and never runs in Docker.

## Topology

```
Docker Workspace Bridge (HttpPiRuntime)
  -> http://host.docker.internal:8780 (X-Runtime-Token)
    -> native pi-host-adapter (this component, normal macOS user)
      -> in-process AgentSession (@earendil-works/pi-coding-agent 0.87.0)
        -> macOS Xcode/CoreML/MLX/Metal toolchain
```

The Bridge runtime selection is Pi-only: Pi is the configured backend.
This adapter serves the Mac execution plane.

There is no subprocess transport: managed sessions never spawn
`pi --mode rpc`, there is no JSONL framing, and no frame-size ceiling can
kill a session. Oversized tool payloads are summarized to bounded audit
evidence while the session stays usable.

## Read-only boundary loadout

Every managed session is created with an explicit SDK loadout:

- `tools: ["read", "grep", "find", "ls"]` (plus `edit`, `write` when the
  session is writable; plus `bash` unless shell mode is `deny`) is Pi's
  strict allowlist across built-in, extension, and custom tools.
- Project trust is disabled (`projectTrusted: false`, the SDK equivalent
  of `--no-approve`), so trusted project-local resources cannot widen
  what the session may do.
- Extension/skills/prompt/theme/context-file discovery is disabled (the
  SDK equivalent of `--no-extensions` plus skill/prompt/context
  suppression). Extensions otherwise execute arbitrary TypeScript with
  the macOS user's full permissions, so they stay off for this
  read-only posture even though the tool allowlist alone would constrain
  tool names.

## Managed extension set (3C2, adapter 0.4.0)

The isolated Bridge profile (`PI_CODING_AGENT_DIR`) may hold user-scope npm
packages installed with the Pi CLI:

```sh
PI_CODING_AGENT_DIR="$HOME/.pi/workspace-bridge" pi install npm:pi-web-access
```

The web manager lists installed packages that provide extension resources
and lets the local admin enable/disable each package for NEW Bridge
sessions (default: none enabled; newly installed code is never
auto-enabled). Token-authenticated `GET /extensions` exposes the bounded
native inventory (no host paths); `GET /health` advertises the
`extension_inventory` capability.

Managed sessions keep auto-discovery off and load the package-owned
trusted permission extension as an inline factory (with the session's
immutable policy snapshot closed over), plus each enabled package root
via explicit `additionalExtensionPaths`. A manifest entry that resolves
to a directory follows Pi's verified explicit-loader rule (empirically
confirmed with marker fixtures, no provider): the directory loads
`<dir>/index.ts`, else `<dir>/index.js`, else `<dir>/package.json`
`main` limited to same-directory files; subpath mains, `index.mjs`, and
main-less/index-less directories load nothing. This is why e.g.
`pi.extensions: ["./dist"]` with `dist/index.js`
(a real installed package shape) is accepted and inventoried as
`dist/index.js`. Because the SDK `tools` allowlist covers extension
tools too, sessions with enabled packages drop the allowlist and hide
built-ins with `excludeTools` instead (edit/write unless writable; bash
when shell is denied; powershell defensively), then activate the
intended built-ins explicitly alongside registered extension tools, so
extension tools stay available. Session creation fails clearly when an
enabled package disappeared or its manifest is invalid; enabled packages
are never silently skipped.

Trust semantics: enabled extension packages execute native code with the
macOS user's authority; Bridge file/shell policy is NOT a sandbox for
extension internals or extension tools. Extension-tool executions are
audited with bounded generic evidence (args hash/size/keys + redacted
safe selectors; ~8 KiB redacted result preview) and never ask for Bridge
permission. Redaction is conservative best-effort, not perfect secret
detection; detail views stay labeled potentially sensitive.

## Tool availability notes (Pi 0.87.0 SDK, verified locally)

The SDK enumerates registered tools directly (`getAllTools()`), so a
provider-free check proves a specific extension tool is registered: the
adapter test suite creates a real AgentSession with a fixture package
root under the managed boundary (auto-discovery off, explicit root,
denylist exposure) and asserts the fixture tool is registered. No LLM
prompt is sent.

Post-deploy smoke requirement (after restart, before trusting the
extension set): exercise one REAL tool from each enabled extension
package in a scratch session and confirm its execution appears in the
Bridge execution audit with the expected extension snapshot.

Provider-free real-profile check (read-only; modifies nothing
installed): `WB_REAL_PROFILE_CHECK=1 npm test` additionally validates
the real isolated-profile inventory (e.g. installed `pi-web-access`
resolves supported with contained relative markers) and creates a real
AgentSession against the real package root with a throwaway agentDir,
asserting the session starts with no extension load error.

## Session protocol notes (Pi 0.87.0 SDK, verified locally)

- `promptAsync` returns `{accepted: true}` as soon as the prompt is
  **accepted** (via the SDK preflight hook); the agent keeps working
  asynchronously (poll `GET /sessions/:id` / `status`). The adapter never
  waits for run completion before responding.
- `GET /sessions/:id` returns authoritative `sessionId`, `sessionFile`,
  `isStreaming`, `messageCount`, and `pendingMessageCount` read
  in-process from the owned session.
- `GET /models` returns `{models: [...]}` with provider/id/name style
  entries from the isolated profile inventory (empty when no provider is
  authenticated).
- Prompt-async `model` selection and `setModel` take an exact
  `provider/id` selector and fail closed for unknown or ambiguous models.
- `abort` succeeds even when idle, resolves owned suspended permission
  selects as rejected first, and never disposes the session.
- Sessions are persistent (SessionManager files under the isolated
  profile session area) and dispose cleanly on shutdown; a disposed
  session fails closed instead of being silently replaced.

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

`GET /health` is readable without a token and exposes booleans/version/status
plus the `agentsession-sdk` transport marker. Every other endpoint requires
`X-Runtime-Token: <WB_RUNTIME_TOKEN>`; an empty token locks the adapter
fail-closed.

## Smoke

```sh
curl -s http://127.0.0.1:8780/health
curl -s -H "X-Runtime-Token: $WB_RUNTIME_TOKEN" \
  "http://127.0.0.1:8780/models?directory=$HOME/Projects/my-app"
curl -s -H "X-Runtime-Token: $WB_RUNTIME_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"directory\":\"$HOME/Projects/my-app\",\"title\":\"review\"}" \
  http://127.0.0.1:8780/sessions
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
temporary profile (verifying session persistence, model discovery, the
0.87.0 dependency pin, and suppressed auto-discovery) without sending
an LLM prompt. An optional real-profile check
(`WB_REAL_PROFILE_CHECK=1 npm test`) additionally validates the real
isolated-profile inventory and loads the real package root in-process.
