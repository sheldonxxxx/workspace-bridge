# Architecture

One `/mcp` endpoint behind one private tunnel and one shared bridge credential
serves all explicitly enabled mappings. Every project call carries its workspace
ID. The separate loopback manager cannot be reached on the MCP listener.

## Component map

- `api.py`: strict typed tool schemas, tools-only MCP adapter and loopback manager API.
- `service.py`: shared auth, mappings, safe source access, planning publication,
  handoff reads, per-workspace write and agent policy, and metadata.
- `git_evidence.py`: fixed-function, bounded read-only Git status and diff
  collection. It keeps `.git` excluded from normal file access and stores no snapshots.
- `run_coordinator.py`: runtime-neutral conversation, run, interaction, and
  activity persistence and reconciliation for Runtime Protocol v1 adapters.
- `wbrp.py`: validated, bounded private HTTP client for every v1 adapter.
- `codex_host_adapter.py` / `codex_rpc.py`: dedicated Codex app-server v2 host
  adapter with native thread ownership and reviewed interactions.
- `runtime/pi-host-adapter/wbrp.mjs`: Pi's v1 facade over its isolated SDK
  session owner and trusted permission extension.
- `runtime.py`: shared runtime errors and identity validation.
- `notifications.py`: Bridge-owned semantic notification events, durable per-channel
  outbox state, and named channel adapters; Discord is configured from local
  environment and contains its own formatting, retries, and HTTP behavior.
- `security.py`: pinned roots, descriptor-relative no-follow traversal, exclusions,
  bounded reads, fixed planning publication and hash-checked policy-scoped text writes.
- `media.py` / `image_worker.py`: typed native image results and fixed, timed
  decoding from authorized bytes; no caller-selected process, URL or file paths.
- `browse.py`: live directory trees, globs and bounded regex/literal search with
  signed continuation cursors and file/listing hashes.
- `embedded_skill.py` and `skills/project-lead/SKILL.md`: fixed package-owned
  project-lead guidance, retrieved on demand.
- `web/` and `static/dist/`: local manager source and compiled assets, with
  workspace controls, Runtime Protocol run views, profiles, shared-token controls,
  and copyable manual handoffs.
- `runtime/pi-host-adapter/`: private Node Runtime Protocol v1 adapter around the
  natively hosted Pi agent; it never starts or packages Pi itself.

## Read and write boundaries

Authenticate the shared credential → validate the explicit enabled workspace →
open the pinned root → apply Bridge-owned policy → return bounded, untrusted
source with pagination. A serialized service lock protects internal operations,
not external file writers. Hashes detect stale reads/listings; they do not recreate
source history. Ignore files do not grant access.

`prepare_handoff` publishes three exclusively created planning documents under a
server-generated job folder. Optional context hashes read only specifically named
files before publication. No whole-tree capture occurs. `write_file` and
`edit_file` use general paths with local policy: `none`, `handoff` (default), or
`workspace`. The current mapping policy is loaded under the serialized operation
lock, not taken from an MCP argument. `none` also denies `prepare_handoff`. Only
the separate manager API can set policy. Existing files need a matching SHA-256.
SafeRoot stages complete bytes in a private same-directory temporary file,
rechecks the target/parent, then publishes with no-clobber linking for creation or
atomic replacement for an update. This is not an OS-atomic compare-and-swap
against arbitrary external writers. The service lock serializes this process's
tool calls, not other local programs. Notes are plain files; no source snapshots
or persisted audit subsystem is introduced. New mappings default to handoff-only
writes.

Normal file tools allow explicit handoff reads and scans; default source-root
scans still exclude it. Published job hashes and metadata remain original and may
therefore differ from a deliberately edited document.

## Runtime Protocol v1

`RunCoordinator` is the only Bridge run path. It validates handoffs, workspace
grants, profile revisions and model policy before creating a runtime conversation.
It persists Bridge-owned runs, live interactions, and bounded activity snapshots
in `runtime_runs`, `runtime_conversations`, `runtime_interactions`, and
`runtime_activities`. Pi and Codex adapters implement the same private `/v1/*`
contract. Adapter-specific permission, approval, and filesystem controls remain
inside each native runtime and are exposed through reviewed interaction choices.
All configured adapters share these run tables and APIs; the Bridge has no direct
Pi session endpoint.

The manual handoff path remains available independently of runtime adapters.
Agent execution is a per-workspace local-admin policy, disabled by default and
independent from `write_scope`; MCP cannot change it. `start_agent_run` accepts a
prepared handoff only, never a free-form prompt or path. A continuation creates a
new Bridge run inside the same conversation only after runtime, model, profile
revision, handoff, and conversation state checks pass. Otherwise it fails closed.

Runtime conversations are routing context and profile bindings, not OS
filesystem sandboxes by themselves. Pi and Codex enforce different native
security mechanisms; review their profile claims before enabling write access.

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

Fresh databases contain mappings, gateway auth, jobs, content-free operation
events, and Runtime Protocol conversation/run/interaction/activity tables. Job
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
for host tunnel profiles. Native startup still binds 127.0.0.1.

Compose uses a separate private state bind and a dedicated project-parent bind at
the same absolute host path, preserving consistent workspace paths for host
adapters. State and the
parent must not overlap. Write scope remains none/handoff/workspace; read-only
rootfs does not make the writable project bind read-only. The host tunnel remains
separate, and the package-owned skill is version 2.5.0.

## Runtime adapters

`WB_RUNTIME_ADAPTERS` maps runtime IDs to private host adapter URLs, and
`WB_RUNTIME_TOKEN` authenticates every adapter request. The Pi adapter implements
Runtime Protocol v1 over its isolated native SDK owner. The Codex adapter owns a
dedicated app-server process and native thread state. Both bind to host loopback
and have no Compose service or published port. See
[Runtime Protocol](RUNTIME_PROTOCOL.md) and [Docker](DOCKER.md).
