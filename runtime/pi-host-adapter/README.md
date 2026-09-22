# Pi host adapter (native macOS only)

A native macOS host adapter that owns read-only `pi --mode rpc` subprocesses
for Workspace Bridge. It is intentionally **not** a Compose service and never
runs in Docker.

## Topology

```
Docker Workspace Bridge (future HttpPiRuntime)
  -> http://host.docker.internal:8780 (X-Runtime-Token)
    -> native pi-host-adapter (this component, normal macOS user)
      -> stdio JSONL
        -> pi --mode rpc --tools read,grep,find,ls --no-approve --no-extensions
          -> macOS Xcode/CoreML/MLX/Metal toolchain
```

The Bridge runtime selection is not wired yet: OpenCode remains the sole wired
backend. This adapter validates the Mac execution plane independently first.

## Read-only boundary flags

Every Pi child (long-lived sessions and short-lived model-discovery children)
is spawned as:

```
pi --mode rpc --tools read,grep,find,ls --no-approve --no-extensions
```

- `--tools read,grep,find,ls` is Pi's strict allowlist across built-in,
  extension, and custom tools.
- `--no-approve` overrides project trust for the run, so trusted
  project-local resources cannot widen what the child may do.
- `--no-extensions` disables extension discovery in the isolated agent dir.
  Extensions otherwise execute arbitrary TypeScript with the macOS user's full
  permissions, so they stay off for this read-only spike even though the tool
  allowlist alone would constrain tool names.

## Managed extension set (3C2, adapter 0.3.0)

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

Managed sessions keep `--no-extensions` (auto-discovery stays off) and
load the package-owned trusted permission extension first, then each
enabled package root via repeated explicit `-e`. A manifest entry that
resolves to a directory follows Pi 0.86.1's verified explicit-loader
rule (empirically confirmed with marker fixtures, no provider): the
directory loads `<dir>/index.ts`, else `<dir>/index.js`, else
`<dir>/package.json` `main` limited to same-directory files; subpath
mains, `index.mjs`, and main-less/index-less directories load nothing.
This is why e.g. `pi.extensions: ["./dist"]` with `dist/index.js`
(a real installed package shape) is accepted and inventoried as
`dist/index.js`. Because official Pi
`--tools` allowlists extension tools too, sessions with enabled packages
drop `--tools` and hide built-ins with `--exclude-tools` instead
(edit/write unless writable; bash when shell is denied; powershell
defensively), so extension tools stay available. Session creation fails
clearly when an enabled package disappeared or its manifest is invalid;
enabled packages are never silently skipped.

Trust semantics: enabled extension packages execute native code with the
macOS user's authority; Bridge file/shell policy is NOT a sandbox for
extension internals or extension tools. Extension-tool executions are
audited with bounded generic evidence (args hash/size/keys + redacted
safe selectors; ~8 KiB redacted result preview) and never ask for Bridge
permission. Redaction is conservative best-effort, not perfect secret
detection; detail views stay labeled potentially sensitive.

## Tool availability notes (Pi 0.86.1, verified locally)

Pi's RPC surface enumerates commands, skills, and prompts
(`get_commands`) but exposes NO route to enumerate registered tools, so
a provider-free check cannot prove a specific extension tool is callable.
The adapter test suite proves instead that a fixture package root loads
through the managed flags (`--no-extensions -e <root> --exclude-tools …`)
and that its command registration is visible via `get_commands`.

Post-deploy smoke requirement (after restart, before trusting the
extension set): exercise one REAL tool from each enabled extension
package in a scratch session and confirm its execution appears in the
Bridge execution audit with the expected extension snapshot.

Provider-free real-profile check (read-only; modifies nothing
installed): `WB_REAL_PROFILE_CHECK=1 npm test` additionally validates
the real isolated-profile inventory (e.g. installed `pi-web-access`
resolves supported with contained relative markers) and launches Pi
with the managed flags against the real package root, asserting the
session starts with no extension load error.

## Protocol notes (pi 0.86.1, verified locally)

- Commands are sent as single LF-delimited JSON lines shaped
  `{type, id, ...params}` where `type` is the command name (`get_state`,
  `prompt`, `get_messages`, `get_available_models`, `set_model`, `abort`, …).
- Responses correlate by `id`: `{type:"response", id, command, success,
  data|error}`. Command order on the wire is not response order.
- `prompt` responds with success as soon as the prompt is **accepted**; the
  agent keeps working asynchronously (poll `GET /sessions/:id` / `status`).
- `get_state` returns authoritative `sessionId`, `sessionFile`, `isStreaming`
  (plus model/thinking/steering metadata the adapter does not expose).
- `get_available_models` returns `{models: [...]}` with provider/id/name
  style entries (empty when no provider is authenticated).
- `set_model` takes `{provider, modelId}` and fails closed (`Model not
  found`) for unknown models.
- `abort` succeeds even when idle and never kills the child process.
- The Pi RPC protocol is version-sensitive: the adapter fails closed on
  incompatible shapes (malformed lines, uncorrelated responses, missing
  `sessionId`) by marking that session dead instead of guessing.

## Run

```sh
cd runtime/pi-host-adapter
WB_RUNTIME_TOKEN="shared-secret" \
WB_PI_PROJECTS_DIR="$HOME/Projects" \
node main.mjs
```

Optional environment:

| Variable | Default | Notes |
|---|---|---|
| `WB_PI_ADAPTER_HOST` | `127.0.0.1` | Keep loopback; broad binds are not the default. |
| `WB_PI_ADAPTER_PORT` | `8780` | |
| `WB_PI_BINARY` | `pi` | Never installed or upgraded by the adapter. |
| `PI_CODING_AGENT_DIR` | `$HOME/.pi/workspace-bridge` | Isolated agent dir; `~`, `$HOME`, `${HOME}` prefixes expanded. Never the normal `~/.pi/agent` tree. |
| `WB_PI_PROJECTS_DIR` (`WB_PROJECTS_DIR` fallback) | required | Canonicalized; must exist. Every session dir must realpath beneath it. |

`GET /health` is readable without a token and exposes booleans/version/status
only. Every other endpoint requires `X-Runtime-Token: <WB_RUNTIME_TOKEN>`;
an empty token locks the adapter fail-closed.

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

Docker-to-host connectivity (from inside the future Bridge container; Bridge
wiring is not implemented yet):

```sh
curl -s http://host.docker.internal:8780/health
```

`compose.yaml` is deliberately untouched: this adapter must never become a
Compose service because it needs the native macOS toolchain.

## launchd (optional, manual)

The adapter never touches launchd itself. To run it as your user agent:

1. Copy `launchd/com.workspace-bridge.pi-host-adapter.plist` to
   `~/Library/LaunchAgents/`.
2. Replace **every** `__HOME__` with your absolute home path
   (launchd performs no shell expansion), set `WB_RUNTIME_TOKEN` and
   `WB_PI_PROJECTS_DIR`, and fix the `node` path (`which node`).
3. `launchctl load ~/Library/LaunchAgents/com.workspace-bridge.pi-host-adapter.plist`

## Tests

```sh
npm test
```

Node test runner with a fake Pi child process; no network, provider, or model
calls. An optional live smoke (`LIVE_PI_SMOKE=1 npm test`) checks
`pi --version` and a `get_state`/`abort` round-trip inside a temporary
workspace with a temporary `PI_CODING_AGENT_DIR`, without sending an LLM
prompt. It is skipped when `pi` is not installed.
