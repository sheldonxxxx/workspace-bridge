# v0.8.4 — safe OpenCode session continuation for follow-up runs

- `start_opencode_run` accepts an optional `continue_from_run_id`: a new
  Bridge run for a new prepared handoff that reuses a completed run's exact
  OpenCode session via `promptAsync` (no `session.create`; implies
  `parent_run_id`; exposes `session_reused` / `continue_from_run_id`).
  Continuation requires the same workspace, a completed source with no pending
  requests, a revalidated session binding, native status idle (never busy or
  retry, which fail closed as `session_busy` because an open v1.18.x issue can
  persist a prompt sent to a busy session without scheduling it), and the
  source run's exact still-enabled/available model
  (`continuation_model_mismatch` on any change). Omitted `model` inherits the
  source model even if the global default changed. Failures return
  `continuation_unavailable` / `session_busy` / `session_mismatch` and never
  silently start a fresh session. Adapter 0.1.4 adds the narrow session-status
  read (`GET /sessions/:id/status` over the official `session.status` map; a
  missing entry is idle, malformed entries fail validation).
- One active Bridge run per OpenCode session is now a durable SQLite partial
  unique index, and session events route only to the sole active owner
  (ambiguity mutates nothing). Every continuation records a durable
  pre-prompt message boundary; completion, `message_count` and transcripts are
  scoped to that iteration, completed runs persist their own transcript
  snapshot, and legacy rows fail safe when their session was reused later.
  Project-lead skill 1.6.1 prefers continuation for small same-task/model/scope
  corrections and requires fresh sessions otherwise; reuse inherits
  session-scoped approvals, so the permission scope must be unchanged.

# v0.8.3 — permission ask capture, Discord diagnostics, model allowlist, model modal

- The private SDK adapter now accepts OpenCode v1.18.31's real `permission.asked`
  approval-prompt event (previously only `permission.updated`, which dropped the
  real ask before persistence/Discord). Both normalize to one canonical internal
  `permission.asked` representation end-to-end (adapter, transport, orchestrator);
  `permission.updated` remains a compatibility alias at the boundary. A following
  `permission.replied` from a manual approval in an attached OpenCode client still
  resolves the persisted pending request and resumes the run. The ask maps the
  real V1 `PermissionV1.Request` fields (`id`, `permission`, `patterns` as the
  requested target, `always` as the exact proposed always scope surfaced in
  `pattern`, plus bounded `metadata`/`tool`); the reply maps V1 `requestID` to
  the pending OpenCode id and `reply` to the decision. Adapter 0.1.3.
- Discord webhook delivery now sends an explicit stable `User-Agent`
  (Workspace-Bridge, no secrets) and `Accept: application/json`. HTTP errors
  capture a bounded, redacted diagnostic from Discord JSON error bodies only;
  HTML/non-JSON bodies persist just the HTTP code. Failures never change run
  state; 429 retry and permanent 4xx no-retry behavior are unchanged.
- Global model policy is now an enabled allowlist plus a default, not a
  default-only lock: omitting `model` uses the default; an explicit model is
  allowed only when its exact selector is admin-enabled and currently available
  (`model_not_enabled` / `model_unavailable` otherwise; `model_override_forbidden`
  is removed). Whether ChatGPT should pick a non-default enabled model is a
  project-lead skill rule (user request or category → may use; self-initiated
  change → ask first; never silently switch). MCP still cannot change the policy.
- The manager model panel is now a compact summary plus a "Manage models" modal
  (`<dialog>`): searchable enable-checkbox list, a single default `<select>`
  populated from enabled selections, atomic save, cancel discards drafts. Skill
  1.6.0 with the clarified model-choice and capability-aware continuation rules.

# v0.8.2 — global model guardrails and manager UX

- OpenCode model discovery is now GLOBAL: the adapter `/models` endpoint, the
  Python runtime, the orchestrator and the manager `/api/opencode/models` no
  longer take or use a workspace directory (the installed SDK's
  `directory` query is optional). Two workspaces always see the same model set.
  `list_opencode_models` keeps its required `workspace_id` for schema
  compatibility but returns `scope="global"` with per-model policy flags.
- Added a server-enforced global model policy (settings-table `model_policy`):
  an exact-selector enabled set plus one mandatory default. New runs are
  fail-closed (`model_policy_unconfigured`) until the local administrator saves
  a policy; every selector must currently exist globally, the default must be
  enabled, and any non-default `model` is rejected with
  `model_override_forbidden` even when enabled. Omitting `model` uses the
  default; passing the exact default is tolerated. The legacy `default_model`
  setting alone never configures the new policy. MCP has no policy mutation or
  bypass. Adapter 0.1.2.
- The manager model panel is global (no workspace picker): enable/disable
  checkboxes, a default radio among enabled models, atomic save with a
  mandatory-default confirmation, and a status line showing the enforced default
  and enabled count. Added a global Bridge-owned "OpenCode sessions" table
  (`/api/opencode/sessions`, newest first, bounded pagination) with View
  details/session and Stop for active owned sessions.
- Refresh bootstrap no longer flashes the login form: initial HTML hides both
  login and dashboard behind a neutral "Checking local session…" state that
  resolves directly to the dashboard on a valid HttpOnly session and to login
  otherwise. The agent toggle now sits immediately beside workspace access in
  each action row.
- Docker builds are incremental: third-party Python wheels build in a
  dependency-only layer (stdlib `tomllib` extraction) before any Workspace
  Bridge source is copied, the builder uses a BuildKit pip cache mount instead
  of `PIP_NO_CACHE_DIR`, and the adapter uses a BuildKit npm cache mount. The
  runtime image stays offline (`--no-index`), non-root, read-only and
  health-checked with no source-tree COPY-all.

# v0.8.1 — orchestration race, lock and recovery hardening

- Added a local-admin browser manager with cookie-scoped sessions (Path=/api,
  HttpOnly, SameSite=Strict), Bearer fallback, bounded session count, and an
  8-hour TTL with correct absolute-expiry cleanup. Login exchanges the admin
  Bearer token for an opaque session cookie; refresh survives page reload.
- Replaced the old agent-checkbox + "Save agent policy" pair with a direct
  Enable agent / Disable agent toggle per workspace.
- Fixed prompt-submission versus event-pump races: `_submit` records prompt
  acceptance and only advances `starting -> running` conditionally, so a concurrent
  `permission.updated`/`session.error`/cancel is preserved. Idle completion now
  requires an accepted/started run and a completed, non-error assistant response;
  an empty or pre-prompt idle no longer completes a run.
- The private SDK adapter now fails closed when `WB_RUNTIME_TOKEN` is unset: every
  operational endpoint returns 401 and `/health` reports `locked`/`token_configured`
  booleans without revealing the token. Compose/docs explain the locked-until-set
  behavior. The bridge still runs without OpenCode; agent operations stay unavailable.
- Restart reconciliation is conservative: a transient adapter-unavailable result at
  startup (Compose bridge-before-adapter ordering) no longer orphans active runs; it
  retries until the runtime is reachable. Only a positive missing-session result (or
  an observed directory mismatch) orphans a run, including pending-permission runs.
- `permission.replied` persists its response decision and reconciles a synchronous
  race with the manager/MCP respond path (one resolved request, correct decision).
- Sensitive follow-up operations (permission reply, cancel, reconciliation, final
  transcript reads) revalidate that the recorded session still resolves to the mapped
  directory and fail closed on an absent/mismatched directory. The JS SDK adapter now
  preserves an absent upstream session directory as empty instead of substituting the
  caller-requested query directory, so this check cannot be masked at the adapter layer.
- The local manager can clear the bridge default model; subsequent runs use
  OpenCode's own default policy. Version/metadata bumped to 0.8.1 (adapter 0.1.1).



- Added an opt-in, workspace-scoped OpenCode execution path while preserving the
  manual handoff/copy-prompt fallback. ChatGPT can publish a handoff, discover an
  exact model, start one bounded run, read the final result, and dispatch corrective
  handoffs. `jobs.state` remains publication-only; runs live in isolated tables.
- Added a private, client-only `@opencode-ai/sdk` (1.18.31) adapter sidecar. The
  OpenCode server stays native on the host; Workspace Bridge never starts one. The
  adapter has no published port, and the bridge never sees provider credentials or
  the server URL.
- Agent execution is a per-workspace local-admin policy, default FALSE and
  independent from `write_scope`; MCP cannot enable it. `start_opencode_run` is
  handoff-bound, rejects arbitrary prompts/paths, validates the exact model against
  current workspace availability, and is idempotent per `request_id`.
- Preserved OpenCode's normal permissions. `waiting_permission`/`waiting_question`
  are non-terminal, resumable states. Pending requests persist with the exact
  OpenCode-proposed `always` scope, which is passed through unchanged and fails
  closed when unavailable. `once`/`always`/`reject` resume the same session.
- Added bounded Discord notifications for waiting/completion/blocked/failed/cancelled
  from `WB_DISCORD_WEBHOOK_URL`, with safe metadata only and no state change on
  failure. Added restart reconciliation that keeps pending waits answerable and never
  infers completion from a missing worker.
- Added seven MCP tools (19 total), local-manager runtime health, run/session views,
  agent toggle, default model and stop/approve controls. Skill 1.5.0, model discovery,
  migrations (agent_enabled default 0), and documentation updates.



- Added a multi-stage Python image, loopback-published Compose service, private
  persistent state, host UID/GID setup, health checks and deployment documentation.
- Same absolute host/container project paths preserve manual OpenCode handoff paths.
- Fresh-only state bootstrap; existing credentials, mappings and write scopes are
  preserved on Compose restarts/upgrades. Native state is not auto-imported.
- Explicit container bind/public-port settings retain strict Host/Origin checks;
  native CLI remains loopback-only. Manager profiles use the published MCP port.
- Added Docker app-contract tests and a real-container smoke/CI script. Docker engine
  execution was unavailable here; no image build/run or live tunnel success claimed.
- No MCP tool/schema/skill/write-policy changes. Twelve tools; skill remains 1.4.0.

# Changes in v0.6.0 — image reading

- General `read_file` auto-detects PNG/JPEG/WebP/GIF/BMP/TIFF and returns native
  MCP image blocks plus source/preview metadata. Text output remains compatible.
- Optional `representation` and `max_image_dimension`; no added tool names.
- Source/path protection and write scopes unchanged; all writes remain text-only.
- Fixed, timed/resource-bounded Pillow worker; EXIF orientation, metadata stripping,
  alpha preservation and explicit first-frame/downscaling limitations.
- Skill 1.4.0, image documentation, local transport probe and visual-marker helper.
- No new state migration, PDF/Office reader, agent integration or snapshot review.

# Changelog

## 0.5.0 — 2026-09-18

- Rename handoff write/edit tools to general `write_file` and `edit_file`; old names
  are rejected. `read_file` is already general-purpose and keeps its schema.
- Separate tool interface from per-workspace write scope: none, handoff (default),
  workspace. Local-only manager controls permission; no MCP override is possible.
- Migrate old mappings to handoff-only without altering enabled state, credentials
  or jobs. Recheck current policy at call execution. Read-only scope also blocks
  prepare_handoff. Keep 12 tools and the same single tunnel/credential.
- Preserve ordinary source-file permission bits during replacement; keep newly
  created files/handoff notes private. Protect staging names across all paths.
- Update skill 1.3.0, metadata, manager controls, migration and file-access guides.
  Manual OpenCode returns/general-tool review remain the default workflow.


## 0.4.0 — 2026-09-18

- Add `write_handoff_file` (create/full replacement) and `edit_handoff_file`
  (one exact unique replacement), restricted to `.workspace-handoff/`.
- Require the current hash for updates; reject stale changes, unsafe links,
  excluded paths, binary/control/secret-like content and files above 256 KiB.
  Stage complete bytes before publication. No source writes, delete or execution.
- Permit explicit handoff reads/listing/glob/search with existing tools; keep
  default source-root scans separate. Raise only the MCP request cap to 1 MiB.
- Apply handoff exclusions/content checks before publishing standard plans too.
- Update project-lead skill to 1.2.0, tool descriptions, manager labels and guides.
  Preserve manual OpenCode returns and general-source audits; no review engine.
- Retain original job publication hashes/metadata after deliberate document edits.
  No schema migration or new connection is required; discovery now has 12 tools.


## 0.3.0 — 2026-09-18

- Removed snapshot capture, before/after diff storage, review IDs, audit verdicts and three dedicated review tools. Ten tools remain.
- Handoffs now publish three planning documents only; OpenCode returns a normal conversation reply, which the user pastes into ChatGPT. No RESULT.md or TESTS.json is required.
- ChatGPT audits current code through general browsing; updated the project-lead skill to 1.1.0, server instructions, manager and docs.
- Retained optional named-file context checks, source-read protection, one tunnel, workspace IDs, shared authentication and manual dispatch.
- Preserved existing state and historical evidence without loading it into new workflow responses; no automatic deletion or reclamation. Old review tools and report-document selectors are rejected.
- Replaced retired workflow tests and added explicit no-snapshot, current-read, API rejection, UI and non-destructive compatibility coverage.

## 0.2.1 — 2026-09-18

- Added a compact packaged project-lead skill: ChatGPT owns decisions, scope, decomposition and audit; the local model follows explicit milestones.
- Added authenticated read-only `read_project_lead_skill()` (no arguments or workspace access), server instruction hints, and small discovery pointers. Thirteen tools; all existing project tools stay scoped.
- Manual dispatch prompts now specify stop conditions and prohibit weakening tests or inventing results. No agent integration, shell, source-write, permissions or database changes.
- Added skill retrieval/authentication/isolation/content and packaging checks. See `VALIDATION.md` for actual current evidence and live-client limitations.
- Compatible update from v0.2.0: reinstall/restart using existing state and refresh client tool discovery; no new tunnel or token required.

## 0.2.0 — 2026-09-18

Confirmed scope change: **one tunnel/app connection for all explicitly enabled mappings**, superseding v0.1's per-project transport/credential design.

- Single `/mcp` endpoint with `X-Bridge-Token`; explicit required `workspace_id` on every project operation and no mutable current-workspace state.
- Added `list_workspaces`, `list_dir`, `glob`, `grep_files`; changed source `read_file` to 1-based offset/limit and retained stale-content hashes. Removed remote `list_files` and `search_files` aliases.
- Bounded tree listing, deterministic/hash-checked pages, filename patterns, timed regex/literal search, context and signed query/project/inventory-bound cursors. Pagination continues inside files and across no-match scan pages.
- Global manager controls for shared token creation/rotation, pause/resume and one tunnel profile. Per-mapping enable/disable/exclusions and handoff management remain.
- Fail-closed migration disables existing mappings once, preserves history and invalidates old per-project URLs/credentials. Shared access is explicitly not per-chat authorization.
- Preserved baseline/audit ownership and drift checks, no shell/source writes/agent execution, and corrected line-number preservation during multiline redaction.
- Added multi-workspace, cursor, regex-budget, browsing and migration tests; evidence and remaining live-tunnel limitations are in `VALIDATION.md`.

## 0.1.0 — 2026-09-18 (superseded transport design)

Initial protected workspace MCP, loopback manager, immutable manual handoffs and baseline-based static audits. One endpoint/token/tunnel per workspace and ten tools. This mode is not retained as a parallel v0.2 route.
