# Architecture — v0.6

One `/mcp` endpoint behind one private tunnel and one shared bridge credential
serves all explicitly enabled mappings. Every project call carries its workspace
ID. The separate loopback manager cannot be reached on the MCP listener.

## Components

- `api.py`: strict typed tool schemas, tools-only MCP adapter and local manager API.
- `service.py`: shared auth, mappings, safe source access, planning publication,
  handoff reads, per-workspace write policy and metadata. No source snapshot, diff or review engine.
- `security.py`: pinned roots, descriptor-relative no-follow traversal, exclusions,
  bounded reads, fixed planning publication and hash-checked policy-scoped text writes.
- `media.py` / `image_worker.py`: typed native image results and fixed, timed
  decoding from authorized bytes; no caller-selected process, URL or file paths.
- `browse.py`: live directory trees, globs and bounded regex/literal search with
  signed continuation cursors and file/listing hashes.
- `embedded_skill.py` and `skills/project-lead/SKILL.md`: fixed package-owned
  project-lead guidance, retrieved on demand.
- `static/`: local manager with workspace controls, plan/context/acceptance viewing,
  shared-token controls and copyable manual handoffs.

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

OpenCode remains an external manually invoked actor. Its response travels through
the user, not a callback, report parser, polling loop or agent integration.

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

No agent API, shell execution, Git operation, automatic source-write enablement, snapshot
audit, arbitrary binary reader, public OAuth, per-chat ACL, automatic completion tracking,
or background scheduling. Skill following and review quality are model behavior,
not enforced guarantees. The protocol adapter now supports mixed text/image results; the tunnel profile is unchanged.
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
separate, and the package-owned skill stays version 1.4.0.
