# Runtimes

Consolidated Pi, Codex, Claude Code, and OpenCode runtime notes for Workspace Bridge `1.0.0`. For
the wire contract see [Runtime Protocol](RUNTIME_PROTOCOL.md); for service
commands see [Setup](SETUP.md) and [Operations](OPERATIONS.md).

## Concepts

A `RuntimeType` (`pi`, `codex`, `claude`, or `opencode`) names the protocol family. An
`AdapterInstance` is one Node-owned destination: a plugin the Node runs, with
its own name, projects root, settings, enabled state, and configuration
revision. A `WorkspaceRoute` binds one
workspace to one exact same-Node adapter ID with a security binding. Two
instances may share a runtime type (for example two Pi plugins with
different agent directories). MCP callers
discover instances with `list_targets` and select by `adapter_id`;
the runtime type never selects a destination and there is no fallback.

## Plugin adapters

Each runtime also ships as an agent plugin that the owning Node starts and
supervises over stdio (Agent Protocol v2, see
[Runtime Protocol](RUNTIME_PROTOCOL.md#agent-protocol-v2-stdio-plugins)).
A plugin adapter has no endpoint, token or service; add one with
`workspace-bridge node adapter add <plugin> --name <name>` (or
`workspace-bridge local-node adapter add` on the Bridge machine) as shown in
[Setup](SETUP.md#plugin-adapters-no-separate-service).

| Plugin | Command the Node runs | Settings |
|---|---|---|
| `claude` | `python -m workspace_bridge.plugin_host claude` (needs the `claude` extra) | `binary`, `setting_sources`, `permission_mode` |
| `codex` | `python -m workspace_bridge.plugin_host codex` | none |
| `opencode` | `python -m workspace_bridge.plugin_host opencode` | `binary` |
| `pi` | `workspace-bridge-pi-adapter --stdio` (npm package) | `pi_binary`, `agent_dir` |

Every plugin also accepts `log_level`. Changing a plugin adapter's projects root
or settings rotates its revision, so active runs bound to the old
configuration are marked interrupted.

## Supervision

The Node starts a plugin on first use and keeps it for later calls. If it
exits, pending calls fail as unavailable and the next call restarts it with
exponential backoff (from one second up to a minute). Stopping, disabling
or removing an adapter, or changing its settings, stops its process group.
Plugin state stays in `plugins/<adapter-id>/state` under the Node state
folder and survives restarts and removal. There is no auto-updater: after a
package update, restart the Node.

The Codex adapter and its owned native app-server form one supervised
failure domain: unexpected native loss terminates the plugin so the Node
restarts the whole unit instead of leaving it alive but degraded. Active runs are marked interrupted on
restart and are never replayed; idle persisted conversations remain
resumable.

The Claude Code adapter drives Claude Code through the Claude Agent SDK
(`claude-agent-sdk`, installed by the `claude` extra), which bundles its own
Claude Code CLI. Set `--set binary=<absolute path or name>` on `adapter add`
to use a different Claude Code executable instead. The adapter uses the Node
user's existing Claude Code login; no provider credential is stored by the
Bridge or the Node. Run `claude` once on that host and sign in before adding
the adapter. `--set setting_sources=...` picks the settings layers to load:
`user`, `project`, `local` (default, all three), or `none`.
`--set permission_mode=auto` (or `default`, `acceptEdits`, `plan`, `dontAsk`)
sets the Claude Code permission mode for routes under native security; unset,
the loaded settings' `defaultMode` applies, except `auto`, which Claude Code
honours only when passed this way. If the SDK is missing, the
descriptor fails closed with an install hint instead of starting runs.
Each run starts one Claude Code process for that prompt and resumes the
adapter-created session for continuations, so idle conversations survive an
adapter restart; runs that were active are marked interrupted and never
replayed.

The Pi adapter persists bounded tool audit records in its isolated agent
directory before allowing each tool invocation and independently of Bridge
polling. Records are bound to the exact native session, canonical workspace
directory, and permission and extension revisions. Restart recovery imports
committed completions and marks unfinished tool calls interrupted without
executing them. Persisted approval decisions are evidence only: pending
interactions and session grants are never restored. Storage failures block
new tool execution and approvals; retained-history gaps remain
explicit in the run activity. This does not change run admission or the
Bridge's no-replay rule.

## OpenCode v2

The Python package includes the OpenCode plugin; no separate package or SDK
extra is needed. Use an existing OpenCode **v2** installation and the Node
user's existing provider login and configuration:

```sh
opencode2 --version
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter add opencode --name OpenCode --set binary=opencode2
```

The adapter owns a private authenticated loopback `serve --stdio` process and
uses the **same native OpenCode database** and configuration as the terminal.
It does not copy credentials or create a private native database. A process
configuration overlay adds the packaged permission plugin and disables native
updates, sharing, and Git snapshots. Only the owned server is stopped.
An unexpected owned-server exit terminates the plugin so the Node restarts the
unit and it reconciles journalled state without replay.

The trusted v2 plugin gates every tool and child-session call against the run's
[security profile](SECURITY.md#security-profiles). Edits never reach
`.opencode`, `.agents`, or `opencode.json`/`opencode.jsonc`. An
unknown tool fails closed. Approved shell commands and existing native plugins
run with the host user's authority; this is not an OS sandbox. A missing or
failed guard blocks admission. Native saved permissions cannot widen the guard.
Content grep requires an explicit allowed file because native recursive grep
cannot exclude protected environment files. Code Mode `execute` follows the
`web` control because it includes HTTP fetch; nested tools retain their own gates.

Runs journal native session/message IDs before submission. An ambiguous write
is reconciled from native messages and durable logs; the Bridge never resends
it. Native recovery in the shared database is observed only for owned sessions;
unexpected input relinquishes ownership. Unconfirmed execution becomes
interrupted or orphaned, with stale interactions. Steering, queueing, tools,
forms, usage, and run events use the v2 protocol.

Opening the shared database from another OpenCode process does not grant
that process a Bridge route or make its activity an owned Bridge run, and
unexpected native input relinquishes Bridge ownership. Handing an owned
session to the terminal and reclaiming it are no longer offered: continue
work through ChatGPT. After a crash, an unfinished transcript can prevent
idle proof even after the owned server restarts; start a fresh run instead.

## Troubleshooting

- An adapter is unavailable: run `workspace-bridge node adapter test
  <adapter-id>` on the Node host. It starts the plugin, prints the reason it
  could not answer, and names its `plugin.log`.
- Model save rejected: list the live catalog for the exact workspace first;
  stale selectors fail at save time.
- Route not ready: check the exact `(workspace_id, adapter_id)` pair in
  Doctor or the Manager, not overall health. Gateway, Node, adapter,
  profile, and model freshness are separate observations.
- Missing or busy native conversation on continuation: create a fresh run
  instead; do not retry the same continuation blindly.
- Slow or stuck runs: check pending interactions and bounded activity before
  cancelling. Cancellation requests the bound conversation only.
