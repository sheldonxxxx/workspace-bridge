# Operations

Public operator guide for Workspace Bridge `0.3.0`: state, services, manual
upgrades, release identity, logging, notifications,
recovery, and troubleshooting. For installation see [Setup](SETUP.md); for
architecture see [Architecture](ARCHITECTURE.md).

## State, secrets, backups

Default Bridge state is `$HOME/.local/state/workspace-bridge` (mode `0700`).
Configuration, SQLite database, and the salted admin password file (`admin-account.json`) are private;
`bridge.sqlite3` is mode `0600`. HTTP Node connection tokens are stored in
private Bridge SQLite; agent plugins have no tokens.
Normal Manager APIs return only whether a token exists, and tokens are
excluded from diagnostics, logs, errors, and events. Treat both state
databases and project handoff folders as sensitive: the Bridge holds
sanitized adapter references while the Node holds adapter secrets and
data-plane state.

Fresh state creates the current schema (v4) directly. Existing v4 databases
are upgraded in place when a release needs it, in one transaction that keeps
rows and indexes; `0.2.0` widens the runtime-type constraint to accept Claude
Code. Pre-v4 databases report `state_schema_incompatible` and are left
untouched; use a fresh state path for them. Back up state before upgrading.

Stop the service before a plain filesystem copy; preserve SQLite WAL/SHM
files alongside the database. Alternatively use an explicitly managed SQLite
online backup procedure. Do not copy only a live database file and assume it
is consistent. No automatic backup or pruning is configured, except that run
checkpoints in the Node's `checkpoints/` directory are pruned after 14 days or
when a workspace's store exceeds 1 GiB. That directory holds copies of
workspace source; backups may exclude it, which only loses old run diffs.
Workspace settings in the Manager show checkpoint storage and can clear it.

Keep package installs and tunnel profiles outside mapped projects. Bridge
state cannot overlap a project. `--state` is a global CLI option placed
before the subcommand. Each Node's approved `allowed_roots` is configured
on that Node host; the Bridge cannot edit it. A workspace remains usable
only through its selected Node and fails closed when that Node is
unavailable.

## Revocation

Disable a mapping to reject subsequent calls. Rotate the shared gateway
credential when it may have leaked; save the new secret in the local tunnel
environment and restart the tunnel process. Old credentials stop authorizing
new operations across every mapping. Pausing the shared gateway rejects all
MCP requests without changing individual mappings; re-enabling restores
access to them. An operation already in progress can finish after a
management change; writes and run mutations still queued authenticate and
reload policy when their turn comes, so they see the change.

Shared-token rotation is available in the local Manager while serving, or
via `workspace-bridge rotate-bridge-token` while stopped. It generates a new
token, enables the gateway, and does not enable disabled mappings. Update
the one tunnel environment and restart its process.

Use **Change password** in the Manager to update the admin account. Enter the
current password and a different new password of 8 to 256 characters. The
current browser receives a new session; every other session is invalidated.
Browser sessions expire after eight hours and do not survive service restart.

If the password is lost, run this on the Bridge host with the service user's
private state directory (the Manager may remain running):

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" reset-admin-password
```

For Docker, use `docker compose exec bridge workspace-bridge --state /state
reset-admin-password`. Recovery resets the account to username `admin` and
temporary password `admin`, revokes existing sessions, and requires another
password change on first login. It preserves mappings, run history, and MCP
and Node tokens.

Fresh state and upgraded token-login state bootstrap this same temporary
account. Upgrade creates `admin-account.json` on first Manager startup; old
`admin-token` files and configuration hashes are preserved,
but no longer authenticate the Manager. Complete the first-login password
change promptly. Passwords are stored only as salted scrypt hashes; normal
APIs never return them. Failed sign-ins are limited to five per minute.

Paired Nodes have no token. Disable one in the Manager to refuse its
connection, or re-pair it with `workspace-bridge pairing create --name <name>
--replace` to rotate its key; the old key is refused immediately and runs
started under it become interrupted. The gateway certificate and key live in
the Bridge state folder (`gateway-cert.pem`, `gateway-key.pem`); back them up
with that folder, because a new certificate means pairing every Node again.

## Root replacement and moving projects

Mappings authorize a canonical path. The same configured path stays usable
across reboot or remount even when device identity changes; each request
re-validates the current root and still enforces containment, exclusions,
and scopes. A genuinely missing root fails as unavailable, and a
symlink or file replacement fails closed. Existing mappings, including
disabled ones, cannot be overlapped. There is no destructive delete or
remap operation and no automatic search for moved repositories; use fresh
state with a preserved archive when a mapping must be recreated.

## Limits and incomplete coverage

The implementation targets ordinary code workspaces after
dependency, build, and large-asset exclusions. Checkouts containing photo
libraries, model weights, or huge fixtures need explicit exclusions.
Directory-wide omissions are intentional scope, not reviewed content. A scan
that exceeds its bounds reports the limit; do not claim complete coverage.

Unreadable, excluded, binary, redacted, or oversized content limits manual
code review. No approval gate exists. Changing exclusions affects future
browsing, not a handoff's publication state. Source reads are live; pause
external writers and re-read changed files.

## Readiness checks

`workspace-bridge doctor` prints Overall, Core, Workspaces, Adapters,
Runnable routes, and Git evidence sections. Use `workspace-bridge doctor
--json` for the canonical JSON report and `workspace-bridge doctor
--offline` to guarantee zero adapter or network calls. The authenticated
local-admin `GET /api/diagnostics` endpoint returns the same schema;
`GET /api/diagnostics?offline=1` selects offline mode. Doctor and the API
use one server-side evaluator.

Doctor inspects only the selected state. For a native deployment, use
`workspace-bridge --state /path/to/bridge-state doctor`; for Docker, use
`docker exec workspace-bridge workspace-bridge --state /state doctor`.
An unconfigured default host state does not describe a separate running
Docker Bridge. Add `--offline` to either command to skip network probes.

Statuses are `pass`, `warning`, `action_required`, `failed`, and `unknown`.
Overall severity ranks failed, action required, unknown, warning, then pass.
Doctor exits nonzero for failed or action-required reports and zero for
pass, warning, or unknown-only reports. Unknown adapter, profile, or model
freshness is not success. A route is ready only when the exact
workspace/adapter mapping, accessible root, shared MCP gateway, enabled
workspace route and adapter, and the adapter's current security profile all
pass. A configured model policy is validated; an unconfigured policy is
optional governance and the runtime chooses its own default. Write scope is
not a run prerequisite — it governs only new handoff publication and is
reported as its own diagnostic — so a read-only workspace may still run an
existing prepared handoff. Git evidence is review-only and never blocks a
route.

Listener health, gateway state, runtime health, and runnable routes are
distinct observations. The Manager treats only `runnable_routes[].ready`
for the exact `(workspace_id, adapter_id)` pair as route readiness. If
diagnostics are unavailable, the Manager disables starts and marks readiness
unavailable while retaining other page data. The Handoff start action is an
authenticated local-admin wrapper for an existing prepared handoff that
delegates to the run policy path; it accepts no free-form prompt or model
override. Offline Doctor opens SQLite read-only, starts no workers, recovers
no notification deliveries, and does not contend for the serve process lock.

Diagnostics do not prove account authorization, model behavior, test
execution, or a client's handling of a response. `scripts/smoke_mcp.py
--url http://127.0.0.1:8765/mcp` exercises read-only discovery over real
local HTTP. It prompts for the shared gateway token or reads
`WORKSPACE_BRIDGE_TOKEN`; the token is never a command-line argument.

## Release identity and compatibility

Every deployed component exposes a bounded content-addressed identity:
product `workspace-bridge`, product version, component name and version,
and a deterministic build ID over production inputs only. The build ID never
uses live Git state, paths, timestamps, hostnames, tokens, mutable instance
IDs, or environment-only values, and no paths or file inventories are
exposed.

Product and component versions stay distinct: the product version is the
Workspace Bridge release (`0.3.0`); the component version is that
component's own version. Adapter and native semantic versions, protocol and
feature versions, and Node/adapter revisions remain separate fields.

The Bridge status endpoint returns both the Bridge core identity and the
served Manager identity (or null when missing or invalid, never a
fabricated match). Node and adapter descriptors may carry an additive
optional release object; descriptors without it stay protocol-usable, and a
present malformed or unsupported release object is observed as degraded
update metadata while normal operations continue. Protocol, feature, and
runtime-identity validation stay strict.

The Manager compares its compile-time identity with the served Manager
identity: any difference shows a non-destructive build-mismatch warning
(stale cached bundle against a newer Bridge); absent or invalid metadata on
either side shows identity-unavailable, never a false mismatch.

Canonical diagnostics include a release section. Live Node checks cover
identity present and valid, exact product-version match, and exact core
build-ID match. Live adapter checks cover identity present and valid plus
product-version match (core build-ID match additionally for the Python-based
adapter; the Pi adapter uses an independent artifact build ID). Missing,
invalid, or unsupported first-party identity from an otherwise
protocol-compatible peer is a warning; product or build skew is a warning;
offline or unobserved identity is unknown. Release warnings never enter
runnable-route blockers; protocol compatibility, security, model, and
workspace readiness remain the execution authority.

This is source and package identity, not a container image digest or
code-signing provenance.

## Release target manifest and bundle

A deterministic release target manifest pins exact release identities for
the deployed components with a content-addressed manifest ID over canonical
content excluding the ID itself. Generate it with:

```sh
uv run python scripts/build_release_manifest.py [--output manifest.json]
```

`workspace-bridge release build --output <new-dir> [--json]` builds a
content-addressed bundle from the current checkout, proves every artifact's
embedded release identity matches a freshly generated target manifest, and
never installs, restarts, deploys, contacts live Nodes or adapters, or
mutates service state. `workspace-bridge release validate --bundle <dir>
[--json]` re-runs the same pure validation. The bundle pins exact
deployable bytes to exact release identities with content-addressed bundle
and receipt IDs. Corrupt or tampered bundles fail before any use.

Routine component updates do not transfer bundles: host owners install the
published versions locally — Node, Codex and Claude Code from the Python package via
`uv`, Pi from the npm package via `npm`. Bundles remain release
verification and audit material, not update transport.

## Version compatibility (read-only)

Bridge-first upgrades are supported: the Bridge and Manager may update
first while compatible Nodes and adapters lag until the operator chooses a
maintenance window. Release skew is informational update state, not an
execution gate.

`GET /api/system/versions` is read-only and performs bounded live
observations only. It never refreshes catalogs, mutates state, creates
backups, downloads artifacts, or touches locks. Per-component states are
`current`, `update_available`, `unsupported_build`, `target_mismatch`,
`incompatible`, or `unavailable`, each with execution compatibility and
bounded reason codes. The Manager System / Versions view shows the target
Bridge version and per-component state as routine rollout information with
manual-update guidance only; it performs no installs. Only affected routes
or features are described as impacted for incompatible or unavailable
components.

## Service persistence

### The Bridge

The Bridge installs as a persistent service with the same hardened
lifecycle as the Node: the per-user LaunchAgent `com.workspace-bridge.bridge`
on macOS, or the system unit `workspace-bridge.service` on Linux, running
`workspace-bridge --state <state> serve` as the state owner.

```sh
workspace-bridge service install
workspace-bridge service status
workspace-bridge service restart
workspace-bridge service uninstall
```

A service has no shell environment. Put settings in `bridge.env` in the
Bridge state folder (mode `0600`), one `KEY=value` per line. Only
`WB_NODE_GATEWAY_HOST`, `WB_NODE_GATEWAY_URL`, `WB_DISCORD_WEBHOOK_URL`,
`WB_ADMIN_ALLOWED_HOSTS` and `WB_LOG_LEVEL` are accepted; any other line is an
error, and a variable set in the real environment wins. Restart the service
after editing it. `status` reports whether both listeners answer on
loopback.

### Paired Node connection state

The Bridge records when each paired Node connected, was last seen (every
30 seconds) and disconnected. `doctor` reports it as connected, disconnected
since a time, never connected, or unknown when the running Bridge has not
reported for 90 seconds (for example because it is stopped). Workspace roots
on paired Nodes show as unknown in `doctor`; the Manager checks them live. On
the Node, `node service status` reads the dial-out loop's private
`link-status.json`: `connected` with the time, `disconnected` with the last
error code, `stopped`, or `unknown` when the status is stale.

### Nodes

On macOS the native Node persists through the per-user LaunchAgent
`com.workspace-bridge.node`; on Linux through the system unit
`workspace-bridge-node.service`. Neither is a Compose service. The managed
macOS plist is `~/Library/LaunchAgents/com.workspace-bridge.node.plist`;
Node state defaults to `$HOME/.local/state/workspace-bridge-node`; macOS
output stays in its private `logs/` directory. The managed Linux unit is
`/etc/systemd/system/workspace-bridge-node.service` with output in the
system journal; `User=`/`Group=` keep the Node process non-root as the
installing user. Node and native adapters therefore run as the same user
and see the same absolute host workspace paths. The system unit starts at
boot and survives logout with no extra step.

Use the host-admin CLI without leading `sudo`; the Manager does not control
launchd or systemd:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service start
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service stop
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service uninstall
```

`install` requires initialized state with mode `0700` and private
config/token files, refuses an unmanaged or modified unit, and is idempotent
for exact managed content. On macOS it uses user-domain launchd operations;
on Linux the plain command runs unprivileged and the backend uses only fixed
`sudo` operations for privileged mutation with no shell and no arbitrary
`sudo` arguments. Run the command as the Node owner and authorize the narrow
`sudo` prompts. `status` is read-only and distinguishes the managed unit,
enabled/running/failed state, configured host and port, bounded
authenticated Node health, and per-root availability. A reachable Node with
an unavailable root is transport-healthy but root-degraded; verify root
availability before testing a workspace. Linux `status` never exposes
tokens, executable paths, `sudo` commands, home paths, raw environment,
journal text, or raw service output. Inspect the journal manually with
`journalctl -u workspace-bridge-node.service` when needed. Without
systemd, systemctl, or `sudo` privilege, service management fails clearly
while foreground `serve` remains available.

Advanced root mode (intentional only): running the Node as root is supported
when the operator deliberately chooses it. Initialize and manage a separate
root-owned Node state as root; the unit then carries `User=root` and
`Group=root` and the backend executes the same fixed helper commands
directly without `sudo`. The executable (including any shim and its target)
plus both parent chains must be root-owned and non-writable, otherwise
install fails closed. Root mode grants the Node, and every agent plugin it
starts, full root filesystem authority. Ownership is strictly isolated with no takeover:
a root invocation against an existing user-owned state is rejected rather
than converted, a non-root invocation against root-owned state is rejected,
and switching an installed service between root and non-root requires
`service uninstall` first (or a fresh state).

On macOS, privacy controls can allow the LaunchAgent to start while still
blocking access to a workspace under an external volume. Files & Folders,
external-storage, or other privacy prompts may apply to the
executable/interpreter and the selected workspace. Grant only the required
access to the actual executable or interpreter when prompted; Full Disk
Access is not mandatory when a narrower permission suffices. Root
availability in `status` followed by a Manager workspace check is the
verification path; do not automate the prompt.

`uninstall` removes only the exact managed unit plus its state-local
manifest and preserves Node state, adapters, bindings, allowed roots,
tokens, and logs. Unsupported operating systems fail explicitly; a Node
container is not part of this deployment.

### Agent plugins

Agents are not separate services. Each adapter is a plugin the Node runs as
a child process, as the Node's OS user, under the Node's own service; see
[Runtimes](RUNTIMES.md#supervision). A root Node runs its plugins as root.
Add, test and remove them with `workspace-bridge node adapter ...` on the
Node host. Each plugin's stderr goes to `plugins/<adapter-id>/plugin.log`
in the Node state folder, capped at 5 MiB with one previous file kept.

The safe default Node listen host remains `127.0.0.1` for host-only use. If
the Bridge runs in Docker Desktop, initialize the Node with an explicit
non-loopback host such as `--host 0.0.0.0`, then register the host-alias
Node URL in the Manager. A loopback-only Node is not assumed reachable from
the container. Non-loopback binding requires a host firewall or private
network review, while Node token authentication remains mandatory; the Node
is never exposed through the MCP tunnel. Compose carries Bridge plus the MCP
tunnel only, with restart policy, health check, non-root identity, and
private persistent Bridge state. See [Docker](DOCKER.md).

Setting `WB_ADMIN_ALLOWED_HOSTS` widens only the admin listener to
`0.0.0.0` with those `Host` values allowed (MCP unaffected); invalid values
fail closed. The tunnel client stays on the host; no Docker socket is
mounted into the Bridge.

The optional `scripts/run_tunnel.py` helper only launches the official
client when manually invoked. It is not imported or callable by the MCP
server. Runtime execution is available only through bounded, opt-in Runtime
Protocol tools and Node-supervised agent plugins; the server never exposes a shell
or arbitrary command tool.

## Native logging policy

Docker Bridge and tunnel logs use Docker `json-file` rotation
(`max-size: 10m`, `max-file: 3`).

macOS managed Bridge and Node services write private `<state>/logs/`
`stdout.log` and `stderr.log` (`0700` directory, `0600` files). Each stream
is bounded to a 10 MiB active file plus two 10 MiB archives under the same
private state. A package-owned guard performs bounded copy-truncate rotation
for managed services only; foreground `serve` never spawns it. The guard
owns only those log files, emits nothing to the service logs, exits when its
parent service is gone, and never supervises or restarts the service.

Linux Bridge and Node units declare journal output explicitly. Retention
and rotation remain the host journald and administrator policy; Workspace
Bridge does not install or edit journald, logrotate, timer, cron, or daemon
configuration.

## Runtime Protocol runs and restart recovery

Agent execution requires a local administrator to enable the workspace,
enable an exact same-Node workspace route, and assign its adapter-specific
security binding; a model policy is optional governance per adapter. Run and
conversation views are local-admin-only; MCP cannot change routes, profiles,
or model policy. The Manager shows the owning Node and adapter name and ID,
runtime type, Bridge run and conversation IDs, handoff, model, immutable
security-used snapshot, state, timestamps, and notification delivery. Run
details show current interactions plus bounded activity and execution
records. Only live, adapter-provided choices can be submitted.

On startup and during active runs, the Bridge reconciles durable records
with adapter snapshots. It rebinds only operations the adapter positively
identifies as owned. It never replays a prompt or approval. If the adapter
cannot confirm an operation, the Bridge records an interrupted or orphaned
outcome and marks pending interactions stale. Transient adapter failures are
reported as availability errors and do not cause a prompt retry.

Changing a Node's or adapter's URL or token gives it a new connection
revision. The Bridge never sends control traffic for an existing run to the
replacement endpoint, so active runs started under the old revision are
marked `interrupted` with error `runtime_authority_changed` and their pending
interactions become stale. The native agent may still be running at the old
endpoint; stop it on its host if needed. Disabling a Node or adapter keeps its
revision, so its runs resume reconciliation when it is enabled again.

For a run the Bridge can no longer reach for any other reason, the Manager
API offers `POST /api/runs/<run_id>/abandon`. It marks the run `orphaned`
with error `abandoned_by_admin` and stale interactions without contacting the
runtime. It is local-admin only and never an MCP tool.

Pi, Codex, and Claude Code adapters are Node-owned private processes using the same
Runtime Protocol v1 contract. Native listen ports, bootstrap tokens, state
paths, and service lifecycle remain configured on their hosts. Bridge Node
endpoints and tokens are managed in the local Manager and stored in Bridge
SQLite; Node adapter endpoints and tokens stay in Node SQLite. Changes apply
to the next request without a Bridge restart. Native process logs are
separate from Bridge logs. Set `WB_LOG_LEVEL` independently for the Bridge
and each adapter, and `WB_TUNNEL_LOG_LEVEL` for the tunnel sidecar. Never
enable raw HTTP tunnel logging: it may expose sensitive headers or bodies.

The adapter `/health` endpoint is a minimal lock and readiness check; the
authenticated descriptor reports protocol and adapter capabilities.
Unsupported features fail closed. Live Doctor checks configured instances
with bounded Runtime Protocol requests; offline Doctor reads local
configuration and model policy without contacting the host. Run
notifications use Bridge-owned event and delivery tables; channel failures
do not change run state. The run `notifications` object shows bounded event
summaries and per-channel delivery status.

[Run events](EVENTS.md) use MCP 2.0 discovery and verified, signed HTTPS webhooks
to ChatGPT's subscription callbacks. They reuse the durable notification journal
even without Discord, and expose only identifiers, state and timestamps.
After deploying server changes, rescan the existing ChatGPT MCP connection and
test in a Work chat with Cloud selected. The Bridge needs outbound public HTTPS;
the Manager remains loopback-only. Aggregate event delivery states are available
under `mcp_events` in authenticated `/api/status`. Never enable raw HTTP/tunnel
logging or inspect subscription signing secrets through a public API.

## Security profiles

Security profiles live in the Bridge and are edited by a local admin in the
Manager (`/api/profiles`); no MCP tool reads or changes them. Each route names
one profile, and every run carries that profile's current rules to the plugin,
which enforces them as pre-tool policy rather than an OS sandbox. A run keeps
the rules it started with; an edit reaches the next run, including a
continuation. Before saving an edit that loosens a profile, or that a routed
plugin cannot enforce, the Manager lists the affected rules and routes and
asks for confirmation. A profile in use cannot be deleted. A route is refused
when its plugin cannot enforce the profile's restrictive rules; Codex, for
example, accepts `trusted` but not `read-only` or `reviewed`. See
[Security profiles](SECURITY.md#security-profiles) and
[Security on every run](RUNTIME_PROTOCOL.md#security-on-every-run).

Approved shell commands run with the host user's authority. Claude Code loads
the host's Claude settings, hooks, skills, and MCP servers unless the plugin's
`setting_sources` setting narrows them. See
[Runtimes](RUNTIMES.md).

Codex and Claude Code routes may instead follow the agent's native security:
Codex runs under its effective configuration, and Claude Code under its loaded
settings in the plugin's `permission_mode` (for example `auto`) or the
settings' `defaultMode`. Each native permission prompt becomes a Bridge
interaction. Switching a route between a profile and native security needs a
fresh conversation. Native security is unsupported for Pi and OpenCode.

## Writable handoff notes

Use the general write and edit tools with the default handoff-only policy
for small UTF-8 documents. Current hashes are required for replacements and
edits; no forced overwrite, delete, or rename is available. Read back after
connection loss or ambiguous disk errors. Writes may create parent
directories, and partial failures can leave empty directories or an internal
staging file after process termination; internal staging files are hidden
from tools. Do not remove such files while a write is active. No automated
cleanup or total handoff-storage quota is provided.

For least privilege, precreate each project's `.workspace-handoff/` with
ownership and write permission for the dedicated service identity; give that
identity only read access to the rest of the project. This release does not
configure those OS permissions. Stop the local coding agent before revising
its active plan.

## Per-workspace write permissions

The general tools are `read_file`, `write_file`, and `edit_file`. Write
scope is set only in the local Manager: Exclusions & policy, Write
permission, Save and confirm. The default handoff mode allows notes but not
source. `none` denies every file mutation including `prepare_handoff`;
`workspace` allows bounded permitted source text. New mappings default to
handoff. Scope changes take effect on subsequent calls with no tunnel,
schema change, or restart. OS permissions remain an additional requirement;
enabling scope does not grant filesystem privileges. See
[File access](FILE_ACCESS.md) for mode and ownership limits.

## Manual upgrades

There is no automatic or remote updater. Update each component locally on its
own host, then explicitly restart its persistent service. A package upgrade
never restarts anything by itself, and compatible version differences do not
block routes, so components can be updated one at a time. The Manager
System / Versions view shows component versions and compatibility as
information only; it performs no installs or restarts.

**Bridge and Manager.** With Docker, rebuild and recreate the container from
the checkout; the image builds the Manager, so it ships with the Bridge:

```sh
git pull
docker compose up -d --build
```

Without Docker, upgrade the Python package, then restart the `serve` process.

**Node and its agent plugins.** The Node and the Claude Code, Codex and
OpenCode plugins share the Python package; the Pi plugin is a separate npm
package. Upgrade what you use, then restart the Node, which restarts its
plugins:

```sh
uv tool upgrade workspace-bridge
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge --version
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
```

An install with the Claude extra (`uv tool install 'workspace-bridge[claude]'`)
keeps it on upgrade. A host that runs a Node from a source checkout (an
editable install) needs only the restart after `git pull`.

These steps do not update the native Codex, Claude Code, or Pi CLIs, which you
update with their own installers. A restart interrupts any run in flight on that
Node; Bridge never replays prompts or approvals, so check for active runs
first.

## Known limitations / validation status

- Native Windows is unsupported; use WSL2.
- Image previews are bounded first-frame previews. Whether a given tunnel
  client passes pixels to the model must be verified per connection with a
  fresh visual marker; local tool success alone is not that proof.
- No automatic package updater and no automatic support upload exist.
- Deployment combinations that have not run as a live Bridge plus Node plus
  tunnel smoke on a Docker-equipped host are unvalidated for that checkout;
  validate them together before relying on the deployment. Older container
  fixture scripts that still encode the pre-Node local-bind flow are not
  evidence for the current authority boundary.
- Root service mode is advanced only. A root-owned Node state managed as root
  runs the Node and its agent plugins as root with full filesystem authority.
