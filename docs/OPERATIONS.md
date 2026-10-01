# Operations

Public operator guide for Workspace Bridge `0.1.1`: state, services, manual
upgrades, release identity, logging, support bundles, notifications,
recovery, and troubleshooting. For installation see [Setup](SETUP.md); for
architecture see [Architecture](ARCHITECTURE.md).

## State, secrets, backups

Default Bridge state is `$HOME/.local/state/workspace-bridge` (mode `0700`).
Configuration, SQLite database, and the salted admin password file (`admin-account.json`) are private;
`bridge.sqlite3` is mode `0600`. Node connection tokens are stored in
private Bridge SQLite and runtime adapter tokens in private Node SQLite.
Normal Manager APIs return only whether a token exists, and tokens are
excluded from diagnostics, logs, errors, and events. Treat both state
databases and project handoff folders as sensitive: the Bridge holds
sanitized adapter references while the Node holds adapter secrets and
data-plane state.

Fresh state creates the current schema directly. Older databases report
`state_schema_incompatible` and are left untouched; use a fresh state path.

Stop the service before a plain filesystem copy; preserve SQLite WAL/SHM
files alongside the database. Alternatively use an explicitly managed SQLite
online backup procedure. Do not copy only a live database file and assume it
is consistent. No automatic backup or pruning is configured.

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
access to them. A current operation can finish before a serialized
management change takes effect.

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
password change on first login. It preserves mappings, run history, and MCP,
Node, and adapter tokens.

Fresh state and upgraded token-login state bootstrap this same temporary
account. Upgrade creates `admin-account.json` on first Manager startup; old
`admin-token` files and configuration hashes are preserved,
but no longer authenticate the Manager. Complete the first-login password
change promptly. Passwords are stored only as salted scrypt hashes; normal
APIs never return them. Failed sign-ins are limited to five per minute.

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
Workspace Bridge release (`0.1.1`); the component version is that
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
published versions locally — Node and Codex from the Python package via
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
install fails closed. Root mode grants the Node full root filesystem
authority; it does not by itself make separately non-root adapter services
root (a root-owned adapter state managed as root is what runs that adapter
and its native agent as root). Ownership is strictly isolated with no takeover:
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

### Adapter instances (Pi/Codex via `workspace-bridge adapter`)

Each adapter state owns exactly one Pi or Codex AdapterInstance. The
supported path is package installation (`uv tool install workspace-bridge`
for the `workspace-bridge-codex-adapter` executable, `npm install -g
workspace-bridge-pi-host-adapter` for `workspace-bridge-pi-adapter`)
followed by `workspace-bridge adapter --state <path> init` (token printed
once) plus `service install`; hand-written plists are not supported. The
per-instance macOS label is
`com.workspace-bridge.adapter.<runtime>.<opaque-id>` under
`~/Library/LaunchAgents/`; the Linux unit is
`workspace-bridge-adapter-<runtime>-<id>.service` under
`/etc/systemd/system/`. Both artifacts run only the stable lexical
`workspace-bridge adapter --state <state> serve` launcher and never contain
the runtime token or projects root. `service status` combines OS service
state with an authenticated descriptor health check and exposes only bounded
readiness, version, and service fields. macOS starts after user login;
Linux starts at boot. Root mode (root-owned state, root-controlled launcher
plus runtime executable) gives the agent root filesystem authority and is
intentional only. After every manual package update, explicitly run
`service restart`; there is no auto-updater. `service uninstall` preserves
config, token, runtime state, and logs.

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
Protocol tools and separate host adapters; the server never exposes a shell
or arbitrary command tool.

## Native logging policy

Docker Bridge and tunnel logs use Docker `json-file` rotation
(`max-size: 10m`, `max-file: 3`).

macOS managed Node and adapter services write private `<state>/logs/`
`stdout.log` and `stderr.log` (`0700` directory, `0600` files). Each stream
is bounded to a 10 MiB active file plus two 10 MiB archives under the same
private state. A package-owned guard performs bounded copy-truncate rotation
for managed services only; foreground `serve` never spawns it. The guard
owns only those log files, emits nothing to the service logs, exits when its
parent service is gone, and never supervises or restarts the service.

Linux Node and adapter units declare journal output explicitly. Retention
and rotation remain the host journald and administrator policy; Workspace
Bridge does not install or edit journald, logrotate, timer, cron, or daemon
configuration. Support exports bound what is read (newest lines via fixed
`journalctl` only).

## Support bundle

`workspace-bridge support bundle --output <new-file.zip> [--offline]
[--node-state <path>] [--adapter-state <path> ...]` creates a new private
(`0600`) ZIP of at most 5 MiB for troubleshooting. It is a local-admin
read and export operation only: no service or package mutation, no upload,
and no network beyond the same bounded `doctor` probes unless `--offline`
is set. Node and adapter states are explicit local-admin paths; the default
Node state is included only when it exists and validates, adapter states
only when explicitly supplied (deterministic order, at most 16). When Bridge
state is unavailable, the bundle still contains a sanitized initialization
diagnostic plus any readable local service evidence.

The ZIP uses fixed generic names and contains only projected diagnostics and
release identities, safe local service status, and bounded sanitized log
excerpts (newest lines per service). It excludes tokens, credentials,
prompts, results, tool arguments, provider payloads, absolute paths,
environment dumps, webhook URLs, raw bodies and headers, private keys, and
SQLite, config, and token files, and it never reads project files. The
bundle is designed to be shareable but must still be reviewed before any
external upload; no upload occurs automatically.

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

Pi and Codex adapters are Node-owned private processes using the same
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

## Runtime profiles

Pi runs natively with the host user's authority, so its security profile is
pre-tool policy rather than an OS sandbox. Profiles control supported file
tools, external paths, protected paths, shell behavior, and session grants
through the Pi host adapter's trusted permission extension. A profile
revision is immutable for each conversation; changes take effect in new
conversations. Review each profile's scope in the local Manager before
assigning it. Each adapter instance has its own profile discovery and model
policy even when two instances share one runtime type. Codex uses a separate
native permission profile with an approval policy and reviewer; the two
runtimes' profile claims are not equivalent. See [Runtimes](RUNTIMES.md).

Codex workspaces may instead follow the effective native configuration
security settings. The Bridge observes a security-only revision and bounded
summary, then attempts to update an idle conversation before its next turn.
A running turn keeps its captured settings. When Codex cannot represent or
confirm an update, the Bridge starts a fresh conversation that resolves the
current native configuration. This mode is distinct from assigning a Bridge
profile and is unsupported for Pi.

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

There is no automatic or remote updater. Update the installed tool locally
on each host, then explicitly restart the affected persistent services:

```sh
uv tool upgrade workspace-bridge
workspace-bridge --version
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
```

A package upgrade never restarts anything by itself. The Pi adapter is a
separate local npm-managed component updated with npm on its host. The
Manager System / Versions view shows component versions and compatibility
as information only.

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
  runs the Node as root with full Node filesystem authority; a root-owned
  adapter state managed as root runs that adapter and its native agent as
  root. Running the Node as root does not by itself make separately non-root
  adapter services root.
