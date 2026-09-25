# Architecture

One `/mcp` endpoint behind one private tunnel and one shared bridge credential
serves all explicitly enabled mappings. Every project call carries its workspace
ID. The separate loopback manager cannot be reached on the MCP listener.

## Diagnostics and readiness

`workspace_bridge/diagnostics.py` is the sole server-side evaluator for the
canonical `DiagnosticReport`, `DiagnosticCheck`, and `RunnableRoute` schema.
Doctor and authenticated local-admin `GET /api/diagnostics` call the same
service method. A route is evaluated for one concrete workspace/adapter pair;
facts from another adapter instance cannot satisfy its prerequisites, including
when two adapters share the same runtime type.

Listener health, shared MCP gateway configuration/enabled state, Node health,
adapter-instance health, and an exact runnable route are separate observations.
Git Evidence is a read-only review capability and does not gate route readiness.
The status vocabulary is `pass`, `warning`, `action_required`, `failed`, and
`unknown`, ranked from lowest to highest severity as pass, warning, unknown,
action required, and failed. Unknown is never promoted to pass.

The authenticated Manager consumes `/api/diagnostics`; only
`runnable_routes[].ready` for the exact `(workspace_id, adapter_id)` pair
represents route readiness. `overall.status`, adapter health, Bridge gateway configuration and
enablement, and run interactions remain
separate concepts. A diagnostics fetch failure makes route readiness unavailable
in the UI and disables handoff starts without discarding other Manager data.

Offline diagnostics read local state only and mark adapter, profile, and model
freshness unknown. Doctor's service mode opens SQLite read-only, performs no
notification recovery, starts no workers, and does not contend for the serve
process lock. Runtime calls in live mode use bounded private Runtime Protocol
requests after copying relevant local policy out from under the database lock.
The Manager uses these authoritative route objects for overview, workspace
cards, and the Handoff route selector. Missing or truncated routes remain
unevaluated; the client does not reconstruct prerequisites.

## Component map

- `api.py`: strict typed tool schemas, tools-only MCP adapter and loopback manager API.
- `service.py`: shared auth, mappings, Node-routed workspace operations, planning
  publication, handoff reads, per-workspace write and agent policy, adapter
  references, and WorkspaceRoutes.
- `node_service.py` / `node_api.py`: private Node data-plane service/API for
  allowed roots, files, images, Git, handoffs, and Node-owned adapter secrets.
- `node_registry.py` / `node_client.py`: Bridge's authenticated Node inventory
  and bounded Node Protocol/runtime proxy.
- `adapter_registry.py`: sanitized Bridge cache of Node-owned AdapterInstances
  and on-demand Node runtime proxies.
- `diagnostics.py`: the canonical bounded diagnostics evaluator and exact
  workspace/adapter runnable-route model shared by Doctor and the admin API.
- `git_evidence.py`: fixed-function, bounded read-only Git status and diff
  collection. It keeps `.git` excluded from normal file access and stores no snapshots.
- `run_coordinator.py`: adapter-ID-keyed conversation, run, interaction, and
  activity persistence and reconciliation for Runtime Protocol v1 clients.
- `wbrp.py`: validated, bounded private HTTP client for every v1 adapter.
- `codex_host_adapter.py` / `codex_rpc.py`: dedicated Codex app-server v2 host
  adapter with native thread ownership and reviewed interactions.
- `runtime/pi-host-adapter/wbrp.mjs`: Pi's v1 facade over its isolated SDK
  session owner and trusted permission extension.
- `runtime.py`: shared runtime errors and identity validation.
- `notifications.py`: Bridge-owned semantic notification events, durable per-channel
  outbox state, and named channel adapters; Discord is configured from local
  environment and contains its own formatting, retries, and HTTP behavior.
- `security.py`: shared validation primitives used by the Node for descriptor-
  relative no-follow traversal, exclusions, bounded reads, fixed planning
  publication, and hash-checked policy-scoped text writes.
- `media.py` / `image_worker.py`: typed native image results and fixed, timed
  decoding from authorized bytes; no caller-selected process, URL or file paths.
- `browse.py`: live directory trees, globs and bounded regex/literal search with
  signed continuation cursors and file/listing hashes.
- `embedded_skill.py` and `skills/project-lead/SKILL.md`: fixed package-owned
  project-lead guidance, retrieved on demand.
- `web/` and `static/dist/`: local manager source and compiled assets, with
  adapter inventory, exact workspace routes, adapter-scoped models/profiles,
  Runtime Protocol run views, shared-token controls, and copyable handoffs.
- `runtime/pi-host-adapter/`: private Node Runtime Protocol v1 adapter around the
  natively hosted Pi agent; it never starts or packages Pi itself.

## Read and write boundaries

Authenticate the shared credential → resolve the explicit enabled workspace to
its authoritative Node → have the Node validate the node-local root and apply
its host `allowed_roots` ceiling → return bounded, untrusted source with
pagination. Bridge does not open the workspace root itself and never falls back
to a local checkout. A serialized service lock protects internal operations, not
external file writers. Hashes detect stale reads/listings; they do not recreate
source history. Ignore files do not grant access.

`prepare_handoff` publishes three exclusively created planning documents under a
server-generated job folder. Optional context hashes read only specifically named
files before publication. No whole-tree capture occurs. `write_file` and
`edit_file` use general paths with local policy: `none`, `handoff` (default), or
`workspace`. The current mapping policy is loaded under the serialized operation
lock, not taken from an MCP argument. `none` also denies `prepare_handoff`. Only
the separate manager API can set policy. Existing files need a matching SHA-256.
The Node-side SafeRoot stages complete bytes in a private same-directory
temporary file, rechecks the target/parent, then publishes with no-clobber
linking for creation or atomic replacement for an update. This is not an OS-atomic compare-and-swap
against arbitrary external writers. The service lock serializes this process's
tool calls, not other local programs. Notes are plain files; no source snapshots
or persisted audit subsystem is introduced. New mappings default to handoff-only
writes.

Normal file tools allow explicit handoff reads and scans; default source-root
scans still exclude it. Published job hashes and metadata remain original and may
therefore differ from a deliberately edited document.

## RuntimeType, AdapterInstance, and WorkspaceRoute

`RuntimeType` describes protocol behavior: currently `pi` or `codex`. It is not
an endpoint or destination. A Node owns each `AdapterInstance`, including its
opaque `adapter_id`, unique display name, immutable runtime type, private base URL
and token, enabled state, and connection revision. Multiple instances can use the
same runtime type. The Bridge cache contains only sanitized references; runtime
calls go through the owning Node.

`WorkspaceRoute` binds one workspace to one exact same-Node adapter ID and stores
the route's enabled/default state and security binding. Model policy and profile
discovery are scoped to an adapter instance. Diagnostics identify a route by
`(workspace_id, adapter_id)` and independently probe every adapter. The MCP
discovery tool returns sanitized adapter IDs and exact route availability; no
runtime-type fallback exists.

`RunCoordinator` validates handoffs, exact routes, security bindings and
adapter-scoped model policy before creating or reusing a conversation. It stores
Bridge-owned runs, conversations, live interactions, and bounded activity in
`agent_runs`, `agent_conversations`, `agent_interactions`, and `agent_activities`.
Runs and conversations persist both `adapter_id` and descriptive `runtime_type`.
Adapter-specific permission, approval, and filesystem controls remain inside
their native daemon and are exposed through reviewed interaction choices.

Security discovery may depend on an exact workspace context. The generic Bridge
stores either an opaque profile ID/revision or the explicit `runtime-config`
source; it does not interpret runtime-specific permission definitions. Codex
resolves native permission-profile IDs for the validated workspace directory
and includes effective security config and managed constraints in its opaque
revision fingerprint. Each runtime-config conversation persists the revision
and bounded summary actually applied to its native thread. Before the next turn,
Codex refreshes supported changes at an idle boundary and creates a fresh thread
when the native transition cannot be represented or confirmed.

The manual handoff path remains available independently of adapters. Agent
execution is a per-workspace local-admin policy, disabled by default and
independent from `write_scope`; MCP cannot change it. `start_agent_run` accepts a
prepared handoff and exact `adapter_id`, never a free-form prompt or path. A
continuation creates a new Bridge run inside the same conversation only after
adapter ID, connection revision, model, security source, handoff, and conversation
state checks pass. Endpoint/token changes invalidate explicit continuation with
`adapter_changed`; a rename leaves the connection revision unchanged. Codex
runtime-config drift is observed from the selected Codex adapter and refreshed
before the next turn.

The authenticated local Manager's `POST
/api/workspaces/{workspace}/jobs/{job_id}/runs` wrapper accepts only an explicit
adapter ID and idempotency request ID; the prepared job ID comes from the path. It
delegates directly to `service.start_agent_run`, preserving the existing
workspace route, handoff ownership, model policy, security binding, and adapter
checks. It accepts no arbitrary prompt or model override.

Runtime conversations are routing context and security bindings, not OS
filesystem sandboxes by themselves. Pi and Codex enforce different native
security mechanisms; review their security claims before enabling write access.
Codex either selects an explicit native permission profile or follows the
current config.toml state with Codex as authority for project/user/managed
layering and the filesystem/network rules.

## Notifications

Run state transitions and newly persisted attention interactions create canonical
notification events in the same SQLite transaction as their run/interaction snapshot.
Each configured channel gets its own durable delivery row. A persistent daemon
worker drains rows after startup and on wake signals; request, reconciliation, and
read paths only persist intent and signal it. Channel calls run outside the service
database lock. Rows for channels no longer configured are marked disabled so they
cannot block other channels. Adapter retries are bounded and terminal for each
delivery. A crash after remote acceptance but before the sent result is persisted can
cause one duplicate after restart, so delivery is at least once across that failure
window. Notification delivery is evidence about message delivery; it never decides
or changes run outcome.

Adapters receive only bounded Bridge metadata. Discord formatting, mentions policy,
webhook access, error diagnostics, and bounded network retries stay in the Discord
adapter. Adding a channel means implementing `NotificationChannel` and registering
its local configuration; the run coordinator uses canonical event types.
No webhook endpoint or token is stored in notification tables or returned by local
status/read APIs.

## Image read path

`SafeRoot.read` sniffs 32 bytes on the same authorized descriptor to select either
the unchanged text cap or the separate image-input cap. The source hash is checked
before decoding. The service calls a fixed package-owned Python worker with `-I -B`,
minimal environment, closed inherited descriptors and bytes on stdin. Pillow is
restricted to six raster codecs. The worker returns a clean, bounded raster preview;
`ImageReadResult` reaches the HTTP adapter without JSON-text wrapping the pixels.
Other tool results remain text-only. There is no URL fetch or general process tool.

Each decode has a 10-second parent wall timeout, 8 CPU seconds, no core dumps and
no regular-file writes through RLIMIT_FSIZE. Linux also applies a 1 GiB address-space
limit; macOS has no equivalent memory-limit claim. Bounds and process separation
are not an OS/network sandbox against a compromised native decoder. The service
remains serialized while decoding; a slow image can delay other tools until timeout.
The worker neither receives workspace paths nor produces persistent preview files.

## State and compatibility

Fresh v3 Bridge databases contain Nodes, workspace mappings, sanitized adapter
references, workspace routes/defaults, adapter-scoped model policies, jobs,
content-free operation events, and the `agent_*` Runtime Protocol execution
tables. Each Node has its own fresh private state. Existing non-v3 databases fail
with `state_schema_incompatible`; the development architecture cutover has no
migration or API compatibility layer. Use a fresh state path. Job
publication uses `publishing`, `prepared`, and `failed`; these states say nothing
about implementation completion. New
mappings have a `write_scope` of `handoff` and agent execution disabled until
configured locally.

## Intentional omissions

No general shell execution, Git mutation, automatic source-write enablement,
snapshot audit, arbitrary binary reader, public OAuth, per-chat ACL, or background scheduling.
Read-only Git evidence is a bounded live view and does not establish authorship.
Agent execution exists only through bounded, opt-in Runtime Protocol tools; there
is no arbitrary command endpoint and host adapters have no published container port.
Skill following and review quality are model behavior, not enforced guarantees. The
protocol adapter supports mixed text/image results; the tunnel profile is unchanged.
No external conformance certification is claimed.

## Docker transport wrapper — v0.7

A locally invoked package entrypoint bootstraps only fresh private Docker state,
then calls the same CLI/service with explicit container listener options. Internal
ports stay 8765/8766; exact loopback public ports are additional permitted
Host/Origin authorities. No wildcard, container DNS allowlist, proxy-header trust,
policy override, or MCP tool is added. The manager reports the published MCP port
for host tunnel profiles. Native Node startup defaults to 127.0.0.1 for host-only
use; Docker Desktop reachability requires an explicit non-loopback Node listen
choice and the `host.docker.internal:<node-port>` endpoint.

Compose carries Bridge control-plane state separately from the Node data plane.
Register a Node endpoint and configure its host `allowed_roots`; do not treat a
Bridge project bind or a local checkout as a fallback authority. Write scope
remains none/handoff/workspace, the host tunnel remains separate, and the
package-owned skill is version 2.6.0.

## Native adapter daemon boundary

The local Manager owns Bridge's Node connection inventory: Node endpoint and
token, enablement, and exact workspace routes live in private SQLite. Each Node
owns its adapter endpoint/token records in private Node SQLite. Tokens are
intentionally plaintext during this development phase; both databases are mode
`0600`, and API responses, diagnostics, logs, errors, and events never return
token values. Blank edit tokens preserve the saved value. Endpoint/token edits
take effect on the next request.

Native Pi/Codex daemon listen ports, tokens, state paths, and LaunchAgent/app-
server lifecycle remain configured on their hosts. The daemon's own bootstrap
credential (which may also be named `WB_RUNTIME_TOKEN`) is distinct from
the Node's saved per-adapter connection token. There is no Bridge-side
`WB_RUNTIME_ADAPTERS` registry or global Bridge token. See [Runtime Protocol](RUNTIME_PROTOCOL.md)
and [Docker](DOCKER.md).
