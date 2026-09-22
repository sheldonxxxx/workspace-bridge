# Architecture

One `/mcp` endpoint behind one private tunnel and one shared bridge credential
serves all explicitly enabled mappings. Every project call carries its workspace
ID. The separate loopback manager cannot be reached on the MCP listener.

## Component map

- `api.py`: strict typed tool schemas, tools-only MCP adapter and loopback manager API.
- `service.py`: shared auth, mappings, safe source access, planning publication,
  handoff reads, per-workspace write and agent policy, and metadata. No source snapshot or diff engine.
- `orchestration.py`: the only long-running OpenCode lifecycle owner: handoff-bound
  session creation, run/request persistence, event handling, permission decisions,
  cancellation and restart reconciliation.
- `runtime.py`: narrow, bounded client boundary to the private SDK adapter; request
  and metadata sanitization; no arbitrary command surface.
- `notifications.py`: bounded Discord notifications from local runtime configuration,
  safe metadata only.
- `security.py`: pinned roots, descriptor-relative no-follow traversal, exclusions,
  bounded reads, fixed planning publication and hash-checked policy-scoped text writes.
- `media.py` / `image_worker.py`: typed native image results and fixed, timed
  decoding from authorized bytes; no caller-selected process, URL or file paths.
- `browse.py`: live directory trees, globs and bounded regex/literal search with
  signed continuation cursors and file/listing hashes.
- `embedded_skill.py` and `skills/project-lead/SKILL.md`: fixed package-owned
  project-lead guidance, retrieved on demand.
- `static/`: local manager with workspace controls, plan/context/acceptance viewing,
  run/session views, permission approvals, shared-token controls and copyable manual handoffs.
- `runtime/opencode-adapter/`: private Node client-only sidecar around
  `@opencode-ai/sdk`; it connects to the externally managed host OpenCode server and
  never creates one.

## Read and write boundaries

Authenticate shared credential → validate explicit enabled workspace → open pinned
root → apply bridge-owned policy → return bounded untrusted source plus pagination.
A serialized service lock protects internal operations, not external file writers.
This is live observation, not an atomic filesystem snapshot. Hashes detect stale
reads/listings; they do not recreate source history. Ignore files do not grant access.

`prepare_handoff` publishes three exclusively created planning
documents under a server-generated job folder. Optional context hashes read only
specifically named files before publication. No whole-tree capture occurs. Plans
and published hashes are retained; source text is not copied into job baselines.
`write_file` and `edit_file` use general paths with local policy: `none`,
`handoff` (default), or `workspace`. The current mapping policy is loaded under the
serialized operation lock, not taken from an MCP argument. `none` also denies
`prepare_handoff`. Only the separate manager API can set policy. Existing files need a matching SHA-256. SafeRoot stages complete bytes in a
private same-directory temporary file, rechecks the target/parent, then publishes
with no-clobber linking for creation or atomic replacement for an update. This is
not an OS-atomic compare-and-swap against arbitrary external writers. The service
lock serializes this process's tool calls, not other local programs. Notes are plain
files; no status engine or audit subsystem is introduced. One checked policy column
is added to workspaces; old mappings migrate to handoff-only.

Normal file tools allow explicit handoff reads and scans; default source-root
scans still exclude it. Published job hashes and metadata remain original and may
therefore differ from a deliberately edited document.

OpenCode remains an external actor owned by the host user. The manual path still
travels through the user. The optional automated path uses a private client-only SDK
adapter: a fresh start creates one native OpenCode session per prepared handoff
under the exact mapped workspace directory, submits a server-generated prompt,
and monitors runtime events. An explicit safe continuation instead creates a new
Bridge run and handoff iteration that reuses a completed run's OpenCode session
through promptAsync, keeping the same model. Only one active Bridge run owns a
session at a time, and continuation fails closed when binding, model, status or
scope validation fails.
The bridge never starts, supervises or packages an OpenCode server, and holds no
provider credentials. Runs have their own lifecycle (`starting`, `running`,
`waiting_permission`, `waiting_question`, `completed`, `blocked`, `failed`,
`cancelled`, `orphaned`) isolated from `jobs.state`. `waiting_permission` and
`waiting_question` are non-terminal and resumable; `always` approvals pass through
OpenCode's own proposed scope unchanged.

## Agent execution boundary

Handoffs remain the mandatory execution unit: `start_agent_run` requires a
prepared job in the same workspace and accepts no free-form prompt or path. Agent
execution is a per-workspace local-admin policy (`agent_enabled`, default FALSE,
independent from `write_scope`); MCP cannot change it. The runtime session directory
is routing context plus an explicit binding check, not a hard filesystem sandbox —
OpenCode permissions are not an OS sandbox and the native server has the host user's
authority. Pending requests are persisted with bounded, redacted review metadata and
the OpenCode-proposed pattern; the bridge never broadens it. On startup, interrupted
work is reconciled positively with the runtime or explicitly orphaned, and persisted
pending waits stay answerable.

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

Fresh databases contain mappings, gateway auth, jobs and content-free operation
events. Job publication uses `publishing`, `prepared` and `failed`; these states
say nothing about implementation completion. The historical `jobs.baseline` column
is retained only so existing v0.2 databases can be opened without rewriting them;
new rows put `{}` there. Normal queries do not select old baseline blobs.

Existing `reviews`/`audits` tables and old artifacts are preserved but never read,
written or exposed by a retired tool. They are not created in a fresh database.
No automatic cleanup or destructive migration runs. Existing mappings acquire a
`write_scope` field defaulting to `handoff`; subsequent explicit values persist. Existing v0.2 mappings,
tokens, endpoint and handoffs remain valid. The earlier fail-closed migration from
v0.1 is unchanged. See the migration guides.

## Intentional omissions

No general shell execution, Git operation, automatic source-write enablement, snapshot
audit, arbitrary binary reader, public OAuth, per-chat ACL, or background scheduling.
Agent execution exists only through the bounded, opt-in OpenCode tools described
above; there is no arbitrary command endpoint and no host-published adapter port.
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
the same absolute host path, preserving copyable OpenCode paths. State and the
parent must not overlap. Write scope remains none/handoff/workspace; read-only
rootfs does not make the writable project bind read-only. The host tunnel remains
separate, and the package-owned skill is version 1.6.1.

## OpenCode runtime — v0.8

The Compose stack adds a private `opencode-adapter` sidecar with no published port.
It imports `@opencode-ai/sdk`, builds `createOpencodeClient` against
`WB_OPENCODE_SERVER_URL`, and exposes only health, model list, session create/get,
prompt-async, bounded message read, permission reply, abort and an event long-poll.
It never calls `createOpencode()`/`createOpencodeServer()`. The bridge reaches it at
`http://opencode-adapter:8770` with a shared `WB_RUNTIME_TOKEN`; the host OpenCode
server stays outside Compose. Docker Desktop uses `host.docker.internal`; Linux
Engine needs an explicit reachable host URL. See [Docker](DOCKER.md).
