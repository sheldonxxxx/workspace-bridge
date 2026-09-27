# Runtimes

Consolidated Pi and Codex runtime notes for Workspace Bridge `0.1.1`. For
the wire contract see [Runtime Protocol](RUNTIME_PROTOCOL.md); for service
commands see [Setup](SETUP.md) and [Operations](OPERATIONS.md).

## Concepts

A `RuntimeType` (`pi` or `codex`) names the protocol family. An
`AdapterInstance` is one Node-owned destination with its own name, endpoint,
token, enabled state, and connection revision. A `WorkspaceRoute` binds one
workspace to one exact same-Node adapter ID with a security binding. Two
instances may share a runtime type (for example two Pi daemons). MCP callers
discover instances with `list_agent_adapters` and select by `adapter_id`;
the runtime type never selects a destination and there is no fallback.

## Installation surface

| Item | Pi | Codex |
|---|---|---|
| Package | `workspace-bridge-pi-host-adapter` on npm, executable `workspace-bridge-pi-adapter` | Python product (`uv tool install workspace-bridge`), executable `workspace-bridge-codex-adapter` |
| Instance init | `workspace-bridge adapter --state <dir> init --runtime pi --projects-root <parent> --port 8780` | `workspace-bridge adapter --state <dir> init --runtime codex --projects-root <parent> --port 8772` |
| Token | Printed once by `init`; reprint locally with `show-token`; register on the owning Node | Same lifecycle; stored in private Node SQLite |
| Health | `GET /health` unauthenticated; every `/v1/*` needs the runtime token header | Same |

Each adapter state owns exactly one instance. Service artifacts contain only
the stable `workspace-bridge adapter --state <state> serve` launcher, never
the token or projects root. The Bridge container never starts or publishes
adapters; they stay native on their hosts.

## Service lifecycle

Both runtimes use the same verbs on their own state directory:

```sh
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service status
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service restart
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service uninstall
```

macOS installs a per-user LaunchAgent (starts after login); Linux installs a
system unit (starts at boot, runs as the state owner). `service status`
combines OS state with a bounded authenticated descriptor check. After every
manual package update, explicitly restart the affected service; there is no
auto-updater. `service uninstall` preserves config, token, runtime state,
and logs.

The Codex adapter and its owned native app-server form one supervised
failure domain: unexpected native loss terminates the host adapter so the
supervisor restarts the whole unit. Without that, the HTTP surface would
stay alive but permanently degraded. Active runs are marked interrupted on
restart and are never replayed; idle persisted conversations remain
resumable.

## Security-profile differences

Pi and Codex share the Runtime Protocol but enforce different native
mechanisms. Never describe a profile as an OS sandbox unless that runtime
provides that guarantee.

- **Pi (Bridge-managed profile):** the adapter owns in-process Pi sessions
  and enforces a trusted permission extension with file-tool, external-path,
  protected-path, shell, and session-grant controls. The built-in read-only
  profile allows read tools only; broader profiles ask before shell commands
  or enable edits per their definition. Pi runs with the host user's
  authority. Custom profiles are managed through the Manager and apply to
  new conversations; an existing conversation keeps its immutable revision.
  Native retry uses a bounded budget with exponential backoff; quota and
  billing errors fail promptly without consuming it.
- **Codex (native runtime config and profiles):** the adapter owns native
  threads through its app-server and follows either a Bridge profile wrapper
  (native permission-profile ID plus approval policy and reviewer) or the
  effective native configuration for the exact workspace. Codex owns
  permission definitions, filesystem and network rules, inheritance, trust,
  and config layering; the Bridge shows only a bounded summary and never an
  editable mirror of native rules. Profile-bound starts send the permissions
  selector; config-following starts omit security overrides so Codex
  resolves its own layers. Before a later turn on an existing thread, the
  adapter verifies idle state, re-reads effective security, and applies
  supported changes or starts a fresh thread with a bounded reason when the
  change cannot be represented.

## Continuation and rebind (conceptual)

A new run may continue a terminal run's conversation for a new prepared
handoff in the same workspace. Ownership is proven live against the stored
native conversation on the current same-Node adapter: it must exist, belong
to the workspace, and be idle. History, model changes, and stored revision
drift alone do not block a proven continuation; missing, foreign, or busy
conversations fail closed before any prompt. After proof, only current
Node/adapter metadata is refreshed; historical run evidence stays immutable.

A named-profile change may rebind the same conversation at an idle boundary
when the adapter advertises the rebind capability; the next prompt is sent
only after the target binding is proven, and the transition is recorded as
bounded audit activity. Adapters without that capability fail the transition
before any prompt. A binding-source change requires a fresh conversation;
an ordinary fresh run under the new binding remains allowed. Native
authority stays with the runtime in every case.

## Native authority and model policy

The Bridge owns authorization, handoffs, model policy, run records, and
audit. The Node owns files, Git, handoffs, and adapter secrets. Each adapter
owns its native process or SDK, session files, model syntax, and enforcement.

Model policy is per adapter instance: an enabled allowlist plus a default.
Every selector must currently exist at save time. With no policy the adapter
is unrestricted by Bridge governance: omitting `model` sends no selector so
the runtime uses its native default, and an explicit model only needs to be
in the live catalog. MCP cannot change policy. An optional per-model
reasoning default may be saved by the local administrator; runs use it only
when configured for the selected model.

## Troubleshooting

- `service status` degraded but OS unit running: query the bounded
  descriptor health, then check native logs (macOS state `logs/`, Linux
  journal via manual `journalctl`). Routine status does not read journal
  text; only the sanitized support bundle collector does, through a fixed
  bounded invocation.
- Authenticated endpoints return unauthorized without the token: expected
  fail-closed behavior. Confirm the registered token matches the instance
  that printed it; blank edit fields preserve the stored value.
- Model save rejected: list the live catalog for the exact workspace first;
  stale selectors fail at save time.
- Route not ready: check the exact `(workspace_id, adapter_id)` pair in
  Doctor or the Manager, not overall health. Gateway, Node, adapter,
  profile, and model freshness are separate observations.
- Missing or busy native conversation on continuation: create a fresh run
  instead; do not retry the same continuation blindly.
- Slow or stuck runs: check pending interactions and bounded activity before
  cancelling. Cancellation requests the bound conversation only.
