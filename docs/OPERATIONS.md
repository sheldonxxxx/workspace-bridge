# Operations

## State, secrets, backups

Default state: `~/.local/state/workspace-bridge`, mode 0700. Configuration,
SQLite database and the admin-token file are private; `bridge.sqlite3` is mode
0600. In this development phase, Node connection tokens are intentionally stored
as plaintext in Bridge SQLite and runtime adapter tokens in the private Node
SQLite. Normal Manager APIs return only `has_token`, and tokens are excluded
from diagnostics, logs, errors and events. Treat both state databases and project
handoff folders as sensitive; Bridge holds sanitized adapter references while the
Node holds adapter secrets and data-plane state.

Fresh state creates schema v4. This architecture cutover does not migrate older
databases or retain v1 API aliases. If Bridge reports `state_schema_incompatible`,
use a fresh state path; the existing database is left untouched.

Stop the service before a plain filesystem copy; preserve any SQLite WAL/SHM files with the database. Alternatively use an explicitly managed SQLite online backup procedure. Do not copy only a live database file and assume it is consistent. No automatic backup or pruning is configured.

Keep the package and tunnel profiles outside mapped projects. Bridge state cannot
overlap a project. `--state` is a global CLI option, before the subcommand.
Configure each Node's approved `allowed_roots` on that Node host; Bridge cannot
edit them. A workspace remains usable only through its selected Node and fails
closed when that Node is unavailable.

## Revocation

Disable a mapping to reject subsequent calls. Rotate the shared bridge token when it may have leaked; save the new secret in the local tunnel environment and restart tunnel-client. Old credentials no longer authorize new operations across any mapping. Pausing the shared bridge rejects all MCP requests without changing individual mapping states. Re-enabling the bridge restores access to those mappings. A current operation can finish before a serialized management change takes effect.

Shared-token rotation is available in the local manager while serving, or via `workspace-bridge rotate-bridge-token` while stopped. It generates a new token, enables the gateway and does not enable disabled mappings. Update the one tunnel environment and restart its process.

The bridge has no admin-token rotation button. For a compromised admin secret, stop the daemon and perform a reviewed local credential rotation or initialize fresh private state; do not continue exposing projects with a compromised local administrator account. Fresh state does not import old mappings or handoffs automatically.

## Root replacement / moving projects

Mappings authorize a canonical path. The same configured path stays usable
across reboot/remount even when device/inode identity changes; each request
re-validates the current root and still enforces containment, excludes and
scopes. A genuinely missing root fails as unavailable, and a symlink/file
replacement fails closed. Existing mappings, including disabled mappings,
cannot be overlapped. There is no destructive delete/remap operation and no
automatic fallback search for moved repositories; use fresh state with a
preserved archive when a mapping must be recreated.

## Limits and incomplete coverage

The initial implementation targets ordinary code workspaces after dependency/build/large-asset exclusions. A large RapidRAW checkout containing photo libraries, model weights or huge fixtures needs explicit exclusions. Directory-wide omissions are intentional scope, not reviewed content. A scan that exceeds its bounds reports the limit; do not claim complete coverage.

Unreadable, excluded, binary, redacted or oversized content limits manual code
review. No approval gate exists. Changing exclusions affects future browsing, not
a handoff's publication state. Source reads are live; pause external writers and
re-read changed files. No automatic retention cleanup is added: old snapshot blobs
may still consume space after upgrading, even though new handoffs create none.

## Readiness checks

`workspace-bridge doctor` prints concise Overall, Core, Workspaces, Adapters,
Runnable routes, and Git evidence sections. Use
`workspace-bridge doctor --json` for the canonical JSON report and
`workspace-bridge doctor --offline` to guarantee zero adapter/network calls.
The authenticated local-admin `GET /api/diagnostics` endpoint returns the same
schema; `GET /api/diagnostics?offline=1` selects offline mode. Both Doctor and
the API use one server-side evaluator.

Checks use `pass`, `warning`, `action_required`, `failed`, or `unknown`. Overall
severity is failed, action required, unknown, warning, then pass. Doctor exits
1 when overall is failed/action-required; it exits 0 for pass, warning, and
unknown-only reports. Unknown adapter/profile/model freshness is not success.
A route is ready only when the exact workspace/adapter mapping, accessible root,
handoff-capable write scope, agent switch, shared MCP gateway, WorkspaceRoute,
current adapter-scoped profile revision, and current default model/reasoning
setting all pass.
Git Evidence is review-only and never blocks a route.

Listener health, gateway enabled, runtime healthy, and runnable route are distinct.
The Manager reads this canonical report and treats only
`runnable_routes[].ready` for an exact `(workspace_id, adapter_id)` pair as route readiness;
overall health does not gate that pair. If diagnostics are unavailable, the
Manager disables starts and marks route readiness unavailable while retaining
other page data. The Handoff `Start run` action is an authenticated local-admin
wrapper for an existing prepared handoff and delegates to
`service.start_agent_run`; it accepts no free-form prompt or model override.
Offline Doctor opens SQLite read-only, creates no service workers, does not
recover notification `sending` rows, and runs while `serve` holds the process
lock.

Diagnostics do not prove account authorization, model tool behavior, test
execution, or ChatGPT's handling of a response. `scripts/smoke_mcp.py --url
http://127.0.0.1:8765/mcp` exercises read-only discovery, workspace discovery,
info and directory listing over real local HTTP. It prompts for the shared
bridge token or reads WORKSPACE_BRIDGE_TOKEN; the token is never a command-line
argument. Run `tunnel-client doctor` for the tunnel itself, then validate
discovery and source-write denial and allowed handoff-write behavior in a real
ChatGPT conversation with a nonsensitive sample project.

## Service persistence

On macOS, the native Node is persistent through the per-user LaunchAgent
`com.workspace-bridge.node`; it is not a Compose service and does not run as a
root LaunchDaemon. The managed plist is
`~/Library/LaunchAgents/com.workspace-bridge.node.plist`, Node state defaults to
`~/.local/state/workspace-bridge-node`, and stdout/stderr stay in its private
`logs/` directory. The Node and native Pi/Codex adapters therefore run as the
same user and see the same absolute host workspace paths.

Use the host-admin CLI; Manager does not control launchd:

```sh
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service start
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service stop
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service restart
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service uninstall
```

`install` requires initialized state with mode 0700 and private config/token
files, refuses an unmanaged or modified plist, and uses launchd's user-domain
`bootstrap`, `print`, and `kickstart`/`bootout` operations. `status` is read-only
and distinguishes the managed plist, loaded/running/failed state, configured
host/port, bounded authenticated Node health, and the availability of each
configured root by a bounded label/count summary. A reachable Node with an
unavailable root is transport-healthy but root-degraded; verify root availability
before testing a Manager or Bridge workspace.

On macOS, privacy controls can allow the LaunchAgent to start while still
blocking access to a workspace under locations such as `/Volumes/data2`.
Files & Folders, external/removable-storage, or other TCC prompts may apply to
the executable/interpreter and the selected workspace. Grant only the required
access to the actual executable/interpreter when prompted; Full Disk Access is
not mandatory when a narrower permission is sufficient. `status` root
availability followed by a Manager/Bridge workspace check is the verification
path; do not automate or assume the prompt.

`uninstall` removes only the exact managed plist and preserves Node state,
adapters, workspace bindings, allowed roots, tokens, and logs. Unsupported
operating systems fail explicitly; this milestone does not add systemd or a
Node container.

The safe default Node listen host remains `127.0.0.1` for host-only use. If the
Bridge is in Docker Desktop, initialize the Node with an explicit non-loopback
host such as `--host 0.0.0.0`, then register
`http://host.docker.internal:<node-port>` in Manager. A loopback-only Node is
not assumed reachable from the container. Non-loopback binding requires a host
firewall/private-network review, while Node token authentication remains
mandatory; the Node is never exposed through the MCP tunnel. v0.8 still uses
Docker Compose for Bridge + MCP tunnel only, with restart policy, health check,
non-root UID/GID, and private persistent Bridge state. See DOCKER.md.

Setting `WB_ADMIN_ALLOWED_HOSTS` widens only the admin listener to 0.0.0.0 with
those `Host` values allowed (MCP unaffected); invalid values fail closed.
The tunnel client stays on the host; no Docker socket is mounted into the bridge.

The optional `scripts/run_tunnel.py` helper only launches the official client when manually invoked. It is not imported or callable by the MCP server. Runtime execution is available only through bounded, opt-in Runtime Protocol tools and separate host adapters; the server never exposes a shell or arbitrary command tool.

## Runtime Protocol runs and restart recovery

Agent execution is disabled per workspace until a local administrator enables the
workspace, enables an exact same-Node WorkspaceRoute, assigns its adapter-specific
security binding, and saves that AdapterInstance's model policy. Run and
conversation views are local-admin-only; MCP cannot change routes, profiles, or
model policy. The Manager shows the owning Node and adapter name and ID, runtime
type, Bridge run and conversation IDs, handoff, model, immutable security-used
snapshot, state, timestamps, and notification delivery. Run details show current interactions and bounded activity
and execution records. Only live, adapter-provided choices can be submitted.

On startup and during active runs, Bridge reconciles its durable records with the
Runtime Protocol adapter snapshots. It rebinds only operations the adapter
positively identifies as owned. It never replays a prompt or approval. If the
adapter cannot confirm an operation, Bridge records an interrupted or orphaned
outcome and marks any pending interactions stale. Transient adapter failures are
reported as availability errors and do not cause a prompt retry.

Pi and Codex adapters are Node-owned private processes using the same Runtime
Protocol v1 contract. Native daemon listen ports, bootstrap tokens, state paths,
and LaunchAgent/app-server lifecycle remain configured on their hosts. Bridge
Node endpoints/tokens are managed in the local Manager and stored in Bridge
SQLite; Node adapter endpoints/tokens stay in Node SQLite. The Bridge does not
use `WB_RUNTIME_ADAPTERS` or a global `WB_RUNTIME_TOKEN`. Changes apply to the
next request without a Bridge restart. Native process logs are separate from
Bridge logs. Set `WB_LOG_LEVEL`
independently for Bridge and each adapter, and
`WB_TUNNEL_LOG_LEVEL` for the tunnel sidecar. Never enable raw HTTP tunnel logging
(`LOG_HTTP_RAW_UNSAFE`): it may expose sensitive headers or bodies.

The `/health` endpoint is a minimal lock and readiness check; the authenticated
`/v1/descriptor` reports protocol and adapter capabilities. Unsupported features
fail closed. Live `workspace-bridge doctor` checks configured AdapterInstances
with bounded Runtime Protocol requests; `doctor --offline` reads local adapter
configuration and model policy without contacting the host. Runtime Protocol run
notifications use Bridge-owned event and delivery tables; channel failures do not
change run state. The `read_agent_run` `notifications` object shows bounded event
summaries and per-channel delivery status.

## Pi runtime profile

Pi runs natively with the host user's authority, so its security profile is
pre-tool policy rather than an OS sandbox. Profiles control supported file tools,
external paths, protected paths, shell behavior, and session grants through the
Pi host adapter's trusted permission extension. A profile revision is immutable
for each conversation; changes take effect in new conversations. Review each
profile's scope in the local Manager before assigning it. Each adapter instance
has its own profile discovery and model policy even when two instances share the
Pi runtime type. Codex uses a separate
native permission profile with an approval policy and reviewer; the two runtimes'
profile claims are not equivalent. Codex profile discovery and binding are checked
with the exact workspace ID and validated directory. Bridge selects the native
profile ID; Codex owns the effective project/user/managed config layers and the
filesystem/network policy definitions. See
[Codex adapter security profiles](CODEX_ADAPTER.md).

Codex workspaces may instead follow the effective native `config.toml` security
settings. Bridge observes a security-only revision and bounded summary, then
attempts to update an idle conversation before its next turn. A running turn keeps
its captured settings. When Codex cannot represent or confirm an update, Bridge
starts a fresh conversation that resolves the current native configuration. This
mode is distinct from assigning a Bridge profile and is unsupported for Pi.

## Writable handoff notes

Use the general write/edit tools with the default handoff-only policy for small UTF-8 documents. Current hashes
are required for replacements/edits; no forced overwrite, delete or rename is
available. Read back after connection loss or ambiguous disk errors. Writes may
create parent directories, and partial failures can leave empty directories or an
internal staging file after process termination; internal staging files are hidden
from tools. Do not remove such files while a write is active. No automated cleanup
or total handoff-storage quota is provided.

For least privilege, precreate each project's `.workspace-handoff/` with ownership
and write permission for the dedicated service identity; give that identity only
read access to the rest of the project. This release does not configure those OS
permissions for you. Stop the local coding agent before revising its active plan.

## Per-workspace write permissions (v0.5)

The general tools are read_file, write_file and edit_file. Write scope is set only
in the local manager: Exclusions & policy → Write permission → Save and confirm.
The default handoff mode allows notes but not source. none denies every file
mutation including prepare_handoff; workspace allows bounded permitted source
text. Both old and new mappings default to handoff. Scope changes take effect on
subsequent calls with no tunnel/schema change or restart. OS permissions remain
an additional requirement; enabling scope does not grant filesystem privileges.
See FILE_ACCESS.md for mode/ACL/ownership limits.
