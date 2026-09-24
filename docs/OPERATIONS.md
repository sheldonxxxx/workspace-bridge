# Operations

## State, secrets, backups

Default state: `~/.local/state/workspace-bridge`, mode 0700. Configuration, SQLite database and the admin-token file are private. Back up this state **and** project handoff folders together; the database holds mapping/authentication state, handoff metadata and publication hashes. Treat all backups as sensitive.

Stop the service before a plain filesystem copy; preserve any SQLite WAL/SHM files with the database. Alternatively use an explicitly managed SQLite online backup procedure. Do not copy only a live database file and assume it is consistent. No automatic backup or pruning is configured.

Keep the package and tunnel profiles outside mapped projects. State cannot overlap a project. `--state` is a global CLI option, before the subcommand. Configure approved parent roots at initialization; to change them later, stop the daemon, back up state, edit config.json locally while preserving its private permissions, then restart and run doctor. Removed parents cause old mappings to fail access checks.

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

`workspace-bridge doctor` checks local configuration and mapped roots. It does not prove tunnel connectivity, account authorization, model tool behavior or test execution. `scripts/smoke_mcp.py --url http://127.0.0.1:8765/mcp` exercises read-only discovery, workspace discovery, info and directory listing over real local HTTP. It prompts for the shared bridge token or reads WORKSPACE_BRIDGE_TOKEN; the token is never a command-line argument. Run `tunnel-client doctor` for the tunnel itself, then validate discovery and source-write denial and allowed handoff-write behavior in a real ChatGPT conversation with a nonsensitive sample project.

## Service persistence

Native startup remains foreground-only; no launchd/systemd or reverse proxy is installed.
v0.8 adds Dockerfile/Compose with restart policy, health check, non-root UID/GID,
private persistent state, explicit project binds, and private host runtime adapters
(no Compose service, no published port). See DOCKER.md. Native listeners
remain loopback-only by default; only explicit container startup binds 0.0.0.0 inside the
container, with both Docker-published host ports restricted to 127.0.0.1.
Setting `WB_ADMIN_ALLOWED_HOSTS` widens only the admin listener to 0.0.0.0 with
those `Host` values allowed (MCP unaffected); invalid values fail closed.
The tunnel client stays on the host; no Docker socket is mounted into the bridge.

The optional `scripts/run_tunnel.py` helper only launches the official client when manually invoked. It is not imported or callable by the MCP server. Runtime execution is available only through bounded, opt-in Runtime Protocol tools and separate host adapters; the server never exposes a shell or arbitrary command tool.

## Runtime Protocol runs and restart recovery

Agent execution is disabled per workspace until a local administrator enables the
workspace, grants a configured runtime, assigns a security profile, and saves that
runtime's model policy. Run and conversation views are local-admin-only; MCP
cannot change grants, profiles, or model policy. The manager shows the owning
runtime, Bridge run and conversation IDs, handoff, model, state, timestamps, and
notification delivery. Run details show current interactions and bounded activity
and execution records. Only live, adapter-provided choices can be submitted.

On startup and during active runs, Bridge reconciles its durable records with the
Runtime Protocol adapter snapshots. It rebinds only operations the adapter
positively identifies as owned. It never replays a prompt or approval. If the
adapter cannot confirm an operation, Bridge records an interrupted or orphaned
outcome and marks any pending interactions stale. Transient adapter failures are
reported as availability errors and do not cause a prompt retry.

Pi and Codex adapters are private host processes using the same Runtime Protocol
v1 contract. They bind to loopback, require `WB_RUNTIME_TOKEN`, and are configured
in Bridge with `WB_RUNTIME_ADAPTERS`. Their native process logs are separate from
Bridge logs. Set `WB_LOG_LEVEL` independently for Bridge and each adapter, and
`WB_TUNNEL_LOG_LEVEL` for the tunnel sidecar. Never enable raw HTTP tunnel logging
(`LOG_HTTP_RAW_UNSAFE`): it may expose sensitive headers or bodies.

The `/health` endpoint is a minimal lock and readiness check; the authenticated
`/v1/descriptor` reports protocol and adapter capabilities. Unsupported features
fail closed. `workspace-bridge doctor` reports local adapter configuration and
model policy without contacting the host. Runtime Protocol run notifications use
Bridge-owned event and delivery tables; channel failures do not change run state.
The `read_agent_run` `notifications` object shows bounded event summaries and
per-channel delivery status.

## Pi runtime profile

Pi runs natively with the host user's authority, so its security profile is
pre-tool policy rather than an OS sandbox. Profiles control supported file tools,
external paths, protected paths, shell behavior, and session grants through the
Pi host adapter's trusted permission extension. A profile revision is immutable
for each conversation; changes take effect in new conversations. Review each
profile's scope in the local manager before assigning it. Codex uses a separate
native sandbox and approval policy; the two runtimes' profile claims are not
equivalent.

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
