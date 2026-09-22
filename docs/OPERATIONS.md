# Operations

## State, secrets, backups

Default state: `~/.local/state/workspace-bridge`, mode 0700. Configuration, SQLite database and the admin-token file are private. Back up this state **and** project handoff folders together; the database holds mapping/authentication state, handoff metadata and publication hashes. Older installations can retain source snapshots and historical reviews; treat all backups as sensitive.

Stop the service before a plain filesystem copy; preserve any SQLite WAL/SHM files with the database. Alternatively use an explicitly managed SQLite online backup procedure. Do not copy only a live database file and assume it is consistent. No automatic backup or pruning is configured.

Keep the package and tunnel profiles outside mapped projects. State cannot overlap a project. `--state` is a global CLI option, before the subcommand. Configure approved parent roots at initialization; to change them later, stop the daemon, back up state, edit config.json locally while preserving its private permissions, then restart and run doctor. The first service open against v0.1 state (including doctor) triggers the fail-closed gateway migration; see MIGRATION_0.2.md. Removed parents cause old mappings to fail access checks.

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
preserved archive, or implement and review a deliberate state migration.

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
private persistent state, explicit project binds and a private client-only OpenCode
adapter with no published port. See DOCKER.md. Native listeners
remain loopback-only by default; only explicit container startup binds 0.0.0.0 inside the
container, with both Docker-published host ports restricted to 127.0.0.1.
Setting `WB_ADMIN_ALLOWED_HOSTS` widens only the admin listener to 0.0.0.0 with
those `Host` values allowed (MCP unaffected); invalid values fail closed.
The tunnel client stays on the host; no Docker socket is mounted into the bridge.

The optional `scripts/run_tunnel.py` helper only launches the official client when manually invoked. It is not imported or callable by the MCP server. OpenCode execution is available only through the bounded, opt-in agent tools and the separate host runtime; the server never exposes a shell or arbitrary command tool.

## OpenCode runs and restart recovery (v0.8)

Agent execution is enabled per workspace in the local manager (Agent execution,
default off, independent from write scope). The manager shows runtime health/version,
the Discord configured/not-configured state, the global model policy (enabled models
plus the mandatory default, saved atomically in a "Manage models" modal; MCP cannot change it),
linked runs
(status, handoff/job, bridge run id, OpenCode session id, exact model, timestamps,
notification status), a global Bridge-owned "OpenCode sessions" table across all
workspaces (newest first, bounded pagination, View/Stop on owned records only),
a bounded escaped session transcript, and any pending
permission/question requests with the exact OpenCode-proposed `always` scope. It can
stop an active session and answer `once`/`always`/`reject`. None of these admin routes
exist on the MCP listener.

On restart, the bridge reconciles: a persisted pending request stays `waiting` and is
answerable; a run whose session still exists is kept running; a session that is gone
becomes `orphaned`; and a final assistant message is only accepted as `completed` when
the runtime positively shows a completed, non-error response. An interrupted worker is
never assumed to have finished, and a transient adapter-unavailable result at startup
(e.g. Compose starts the bridge before the adapter) leaves the run active and retries
until the runtime is reachable rather than orphaning it. Pending requests are
re-verified against the recorded session so a positively missing session becomes an
explicit orphan instead of an indefinitely answerable wait.

The OpenCode SSE/event stream is treated as optional best-effort
acceleration: direct live validation in this environment (`curl -N` against
the native event endpoint) produced only `server.connected`/`server.heartbeat`
while a real session ran, so events are latency hints only and are never
required for correctness. A dedicated reconciliation loop (independent of
the 25s event long poll) owns authoritative state convergence while runs
are active, sharing one run enumeration per sweep across checks but never
merging their failure semantics:

- Permissions: exact-session pending snapshot (V2 session-scoped primary
  with V1 compatibility fallback) every ~4s for `starting`/`running` runs.
  A successful empty snapshot is not a failure; a persisted request is
  never resolved merely because it disappeared from a snapshot.
- Completion: durable bounded session messages after the run floor every
  ~7s (`starting`/`running` only, prompt acceptance proven, no pending
  request). The latest in-scope completed non-error assistant message is
  the only completion evidence; session status/idle never gate or prove it.
- Questions: V2 session-scoped snapshot primary
  (`GET /api/session/{sessionID}/question`), verified V1 global fallback
  (`GET /question`, strictly filtered by exact owning `sessionID`) only
  when V2 is explicitly unsupported/not-found (missing method, 404/405/501
  or not-supported), every ~4s with the same strict binding/dedupe/
  persistence semantics. A successful V2 response (even empty) never
  consults V1; generic failures fail closed without fallback. Post-restart
  live validation showed the deployed server not serving the V2 question
  route while completion/permission polling worked, so the fallback is the
  live-compatible path there. No TUI scraping, internal state reads,
  private endpoints, text inference, or ownership guessing exist.

Each sweep covers at most 50 distinct sessions, issues no work when no
relevant active run exists, and stops polling terminal runs immediately.
Expected worst-case detection latency is ~4s (poll tick) + 4s/7s cadence
plus one bounded adapter round trip. `read_agent_run` keeps an
immediate permission resync, question resync and completion self-heal
(`reason=read_reconcile`); sweeps complete with
`reason=background_reconcile`. Waiting permission/question runs and
pre-continuation history never complete a run, and completion notifies
exactly once under repeated reads, sweeps and duplicate events.

Event-stream health is functional, not just transport: the adapter counts
raw/control (`server.connected`/`server.heartbeat`)/functional frames
(`event_stream` in `/health`, counters and timestamps only, never
contents). While runs are active, subscribed transport with no functional
event for 45s reports `functional_status=degraded`
(`reason=no_functional_events`); a never-subscribed transport reports
`unknown` (`transport_not_subscribed`) instead of a false healthy. This
diagnostic never blocks runs because polling is authoritative; degraded /
recovered transitions log once at WARNING / INFO (DEBUG aggregate counts
only). If the adapter is still starting, background loops back off safely
(DEBUG inside the first 30s, throttled WARNINGs after) and recover
automatically.

The private adapter is **locked until `WB_RUNTIME_TOKEN` is set**: with an empty token
every operational endpoint returns 401 and the bridge fails closed. `/health` reports
`locked`/`token_configured` without revealing the token. Agent execution therefore
stays unavailable until both the per-workspace policy and the token are deliberately
configured. Discord notifications (if `WB_DISCORD_WEBHOOK_URL` is set) are sent on
waiting/completed/blocked/failed/cancelled with safe metadata only; delivery failures
are bounded and never change run state. The persisted `notification` record carries
only `status`/`attempts`/`code` plus an optional short non-secret `detail` parsed
from Discord JSON error bodies (HTML/proxy pages are never stored). Post-deploy live
smoke (user-triggered only, never in automated tests): with a webhook configured,
start a real run and let it reach a notified state (for example completion), then
read the run's `notification` field in the manager or via `read_agent_run`;
`sent` confirms delivery and `failed` with `http_403` points at webhook/egress
filtering, not the bridge payload. `workspace-bridge doctor` reports runtime
configuration and the model policy status without contacting the host server.

## Pi file permissions (3B1)

Pi runs natively with your macOS user authority: the permission layer is
pre-tool policy/approval, not a sandbox. By default Pi sessions are
read-only (`read,grep,find,ls`; no trusted extension loaded). The local
manager ("Manage permissions" on the Pi runtime card) can enable writable
tools (`edit`/`write`) with per-tool Allow/Ask/Deny, workspace-relative
protected glob patterns (hard deny, never approvable) with template
exceptions, and an "Always allow exact target" session toggle.

Safe defaults are read-only; restoring them is an explicit save, never a
hidden mutation. Every change is validated strictly (invalid input never
partially applies), logged as an activity event without policy contents,
and applies to NEW Pi sessions only: each session carries an immutable
policy snapshot plus revision, and continuation across a policy change is
refused fail-closed (start a fresh session). An `ask` suspends the exact
tool invocation and resumes it on `once`/`always`/`reject` through the
existing neutral permission flow; `always` is exact-resource and
session-local, never persisted. The adapter (`adapter_version >= 0.2.0`,
`capabilities` in `/health`) re-validates the snapshot, confines every
path canonically to the mapped workspace (symlink escapes denied), and
self-protects the permission implementation from edit/write. No bash, no
project/global extension discovery, no raw args/paths in pending records.
Do not restart live services from an implementation job; the project lead
deploys after audit.

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
See FILE_ACCESS.md for mode/ACL/ownership limits and MIGRATION_0.5.md for upgrade.
