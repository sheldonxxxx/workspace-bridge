# Unreleased — canonical runtime-id grammar for registry and cursor keys

- Package-owned canonical runtime identity
  (`^[a-z0-9][a-z0-9_-]{0,31}$`, shared from workspace_bridge.runtime):
  RuntimeRegistry.register() rejects noncanonical ids with
  invalid_arguments even when the explicit key matches, and the MCP
  RuntimeID schema uses the same constant so the two can never drift.
- Cursor/instance setting keys are lossless suffixes of the exact
  configured id (OpenCode keeps legacy keys); invalid ids outside a
  registered path fail closed instead of normalizing to a colliding key.
- Historical persisted rows are unaffected (persisted ids are never
  re-validated). No adapter, policy, routing, UI, or skill changes.

# Unreleased — 3A3 audit corrections (runtime-correct neutral workflow)

- Native Pi adapter maps AssistantMessage stopReason to completion evidence:
  stop/length with a numeric timestamp produce completed; toolUse/pending/
  deferred/error/aborted/unknown/missing stay incomplete. Fail-closed for
  unknown future reasons; reasoning, tool args, and stopReason stay hidden.
  Fake-child fixture uses a realistic stopReason:"stop" millisecond
  timestamp and the lifecycle test asserts the completed timestamp.
- Completion for runtimes without event polling additionally requires a
  provably idle session_status; busy/unknown/unavailable leaves the run
  active/retryable. Applied in _probe_completion (sweep + read) and restart
  reconciliation. OpenCode eventful behavior unchanged.
- Event-stream cursor/instance settings are per-runtime (OpenCode keeps
  legacy runtime_instance/runtime_cursor; others use runtime-scoped keys);
  event_polling=false starts no event pump and performs no event poll.
- Generic admin /api/runs/* routes and the workspace runs listing are now
  runtime-neutral (persisted-runtime routing / cross-runtime view);
  /api/opencode/sessions stays OpenCode-only.
- Historical rows from a temporarily unconfigured runtime stay readable
  from persisted state (run/request views, terminal transcript snapshot);
  cancel/respond/start/discovery still fail closed with no reroute.
- Tests: adapter stopReason matrix, Python idle/cursor/admin/history
  regression tests (tests/test_3a3_corrections.py), full suite green.

# Unreleased — runtime-neutral agent tools and Pi model policy (3A3)

- New runtime-neutral MCP tools: `list_agent_models`, `start_agent_run`,
  `list_agent_runs`, `read_agent_run`, `read_agent_request`,
  `respond_agent_permission`, `cancel_agent_run`. List/start select a runtime
  explicitly; per-run tools route solely by the persisted run runtime.
- Legacy `list_opencode_*`/`start_opencode_run`/`read_opencode_*`/
  `respond_opencode_permission`/`cancel_opencode_run` stay as OpenCode-only
  compatibility aliases: lists filter to `runtime="opencode"` rows and
  per-run paths fail closed (`runtime_mismatch`) on other runtimes.
- Run `request_id` idempotency is runtime-safe: the same workspace
  `request_id` never replays a run owned by another runtime, for fresh
  starts and continuations alike.
- Model policy stays global per runtime (`model_policy` for OpenCode,
  `model_policy:pi` for Pi). Saving a Pi policy requires an explicit enabled
  workspace as discovery context; every Pi run revalidates against its own
  workspace. `list_agent_models` reports runtime, discovery scope,
  `runtime_global` policy scope, policy, and workspace id.
- Local admin adds `GET /api/runtimes/{runtime}/models`,
  `GET|POST /api/runtimes/{runtime}/model-policy`, and neutral
  `GET /api/sessions`; `/api/settings`, `/api/opencode/models`, and
  `/api/opencode/sessions` stay compatible. `/api/status` adds
  `runtime_policies` without changing existing fields.
- Manager shows a Pi runtime/policy card and manages Pi models through an
  explicit discovery workspace; run/session rows are runtime-aware and the
  sessions table uses the neutral endpoint. Project-lead skill 1.7.0 prefers
  the neutral workflow with OpenCode as the silent/default runtime.
- Pi runs complete through status/message polling with `event_polling=false`
  and no events; permission/question resync stays capability-gated.
- Network docs: OrbStack 29.4.0 verified container
  `host.docker.internal` → native `127.0.0.1`-bound adapter; Docker Desktop
  must be smoke-tested; never broaden to `0.0.0.0`/LAN.
- Tests: new `tests/test_3a3.py` (18 tests); full suite green.

# Unreleased — live-compatible question polling fallback (adapter 0.1.10)

- Post-restart live finding: completion polling recovered a smoke run
  (`reason=background_reconcile`) and permission polling reported ok, but
  question polling stayed `degraded`/`question_list_failed`/
  `runtime_unavailable` — the deployed native server does not serve the V2
  session-scoped question snapshot the 0.1.9 adapter used exclusively.
- Verified against installed `@opencode-ai/sdk` 1.18.31 (no guessing):
  the legacy `Question.list({directory?, workspace?})` →
  `GET /question` (200 `Array<QuestionRequest>`) where
  `QuestionRequest = {id, sessionID, questions, tool?: {messageID,
  callID}}`. Every item carries its exact owning `sessionID`, so strict
  exact-session filtering is explicit and reliable — the safety condition
  for using the global list is met.
- Adapter 0.1.10: `listPendingQuestions` keeps V2 primary (a successful V2
  response, even empty, never consults V1) and falls back to the verified
  V1 surface only on explicit unsupported/not-found signals (missing
  method, 404/405/501, "not supported by this version"; 405 newly
  recognized alongside the permission path). Generic transport/5xx failures
  still fail closed with no fallback. V1 rows normalize through a
  dedicated `normalizeQuestionV1Request` (ids/counts/call refs only) with
  the same strict session binding; `lastQuestionSource` (v2|v1) is exposed
  in the `question_list` log and the `GET /sessions/:id/questions`
  response. Health/wire output changed, so the adapter version bumps
  0.1.9 → 0.1.10.
- Bridge: `question_sync` diagnostics now expose `source` (v1/v2) wherever
  reported; question recovery semantics are otherwise unchanged.
- Tests: adapter fallback matrix (V2-empty/V2-request never touch V1;
  404/405/501/not-supported/missing-method fall back; multi-session and
  malformed V1 rows filtered; generic V2 and malformed V1 failures stay
  closed; source propagation incl. HTTP route) plus Python source
  reporting/transport tests, fallback-path discovery tests, and an opt-in
  live fresh-session question diagnostic
  (`tests/test_opencode_live.py::test_live_question_snapshot_reports_compatible_source`).

# Unreleased — polling-authoritative reconciliation over broken SSE (adapter 0.1.9)

- Root cause, demonstrated live: `curl -N` against the native OpenCode
  event endpoint receives `server.connected`/`server.heartbeat` but no
  lifecycle/message/permission/session events while a real session runs —
  ruling out the bridge, TUI ownership and handlers. Permission recovery
  polled every 60s and completion every 20s, but both sweeps ran only after
  the 25s event long poll returned inside `_pump_loop`, coupling
  reconciliation latency to an event stream that never delivers.
- The bridge now treats OpenCode SSE as optional best-effort acceleration.
  A dedicated `opencode-reconcile-poll` loop (lifecycle-owned alongside the
  event pump, neither able to terminate the other) converges permission
  (~4s), question (~4s) and completion (~7s) state from authoritative
  pollable/durable surfaces while runs are active, sharing one
  starting/running enumeration per sweep (max 50 distinct sessions) without
  merging failure semantics. No active runs means no polling; terminal runs
  leave sweeps immediately. `read_opencode_run` keeps its immediate
  permission/question resync plus completion self-heal.
- Questions use the verified official V2 session-scoped surface only
  (installed `@opencode-ai/sdk` 1.18.31:
  `client.v2.session.question.list` →
  `GET /api/session/{sessionID}/question`, 200 `{data:
  Array<QuestionV2Request>}`), with strict exact-session binding, dedupe by
  request id and `waiting_question` persistence mirroring permissions. Only
  request ids, counts and call references cross the boundary — never
  question bodies/options/answers. The unverified V1 global question
  listing is deliberately not consulted.
- Adapter 0.1.9: EventHub classifies raw frames before normalization and
  exposes bounded `rawEventCount`/`controlEventCount`/`functionalEventCount`
  plus `lastRawEventAt`/`lastFunctionalEventAt` in hub health (counters and
  timestamps only, never contents; per-heartbeat logging stays off, one
  DEBUG aggregate per 100 raw frames). New `GET
  /sessions/:id/questions` route with `question_list` operational logs.
  Health/wire output changed, so the adapter version bumps 0.1.8 → 0.1.9.
- Bridge-level `event_stream.functional_status`: `degraded`
  (`no_functional_events`) while runs are active on subscribed transport
  with no functional event for 45s; `unknown` with no active runs or
  unsubscribed transport (never mislabeled healthy). Diagnostic only —
  polling correctness never blocks — with one WARNING on degrade and one
  INFO on recovery. Startup adapter unavailability backs off safely (DEBUG
  in the first 30s, throttled WARNINGs after) with no false terminal
  states.
- Operational log allowlists add `completion_probe`,
  `completion_reconcile`, `question_resync`, `event_stream_health`
  (bridge) and `question_list`, `eventhub_counts` plus
  `raw/control/functional_event_count` (adapter).
- Tests: `tests/test_polling_authoritative.py` (event-dead permission,
  completion and question convergence via the sweep; 25s-blocked pump
  independence; cadence/cap contract; idle-quietness incl. terminal
  disappearance; unavailable retry/recovery; heartbeat-only degraded health
  without blocking; recovery and once-only transitions; loop lifecycle and
  cadence independence; question transport shape/fail-closure) and
  `runtime/opencode-adapter/test/polling-authoritative.test.mjs`
  (V2 question snapshot semantics, counter classification, DEBUG
  aggregate hygiene, questions HTTP route). `docs/OPERATIONS.md`
  documents cadences, caps, evidence rules and worst-case latency.

# Unreleased — live completion recovery without restart

- Root cause, demonstrated live: an OpenCode job finished in the attached
  UI while Bridge kept showing `running`; only a bridge restart (via
  `restart_reconcile`) marked it completed. The orchestrator completed a
  run only from the transitional `session.idle` event, ignored
  `session.status` entirely, never probed completion on `read_opencode_run`,
  and ran no normal-operation completion sweep — so a missed idle left the
  run stale until restart.
- Verified against installed `@opencode-ai/sdk` 1.18.31: the event union
  exposes `session.status` (`{sessionID, status: idle|busy|retry}`),
  `session.idle` (`{sessionID}`) and `session.error`; there are no
  `session.execution.succeeded/failed/interrupted` (or equivalent newer
  execution-completion) events in this build, so nothing new had to be
  normalized. Session status is in-memory/transitional (an absent map entry
  reads as idle; `GET /session/status` can lag on busy after messages
  already hold a completed assistant response), so status alone never
  proves completion.
- Bridge now treats live events as latency hints and durable completed
  assistant messages as completion evidence: one canonical bounded probe
  (`starting`/`running` only, prompt acceptance proven, no pending
  requests, binding revalidated, messages scoped to the run floor) completes
  the run with `reason=read_reconcile` / `background_reconcile` /
  `status_idle` / `idle`. `session.status` idle triggers the same probe;
  busy/retry change nothing, and busy lag cannot block durable evidence
  (the probe never consults status). Completion evidence means the latest
  in-scope assistant message is completed/non-error, so an earlier
  completed turn cannot finish a still-active later turn; continuation
  floor isolation is unchanged. Read/background probes never orphan (reads
  keep their fail-closed degraded semantics); event-driven finalization
  keeps the existing verified missing/mismatch orphan behavior.
- A bounded background sweep (every 20s, max 50 sessions/sweep, one probe
  per distinct session) recovers missed completions with no read and no
  restart; restarted in-flight runs stay eligible for it after startup
  reconciliation stops retrying. Operational logs add `completion_probe`
  (DEBUG for ordinary no-final probes, INFO on recovery) and
  `completion_reconcile` with run/session ids, reason and message count
  only — never response text. A restart is no longer required to recover a
  missed completion event.
- Tests: `tests/test_completion_reconcile.py` covers read and background
  self-heal with no idle delivered, status-idle hint semantics, busy-lag
  completion, waiting/floor isolation, latest-turn selection, exactly-once
  notification, runtime-unavailable retryability, restart-then-sweep
  eligibility, and log hygiene. Permission/cancel/pre-start tests in
  `tests/test_opencode_runs.py` now script transcripts without durable
  completion evidence (new `hold_open` helper) so they isolate the behavior
  they assert; no assertion was relaxed.

# Unreleased — V2 permission generation alignment (adapter 0.1.8)

- Root cause, demonstrated by live logs: the 0.1.5/0.1.6 V1 listing was
  healthy but was the wrong permission generation for current TUI requests.
  A deployed run stopped on a real OpenCode permission prompt while Bridge
  resyncs repeatedly reported successful `matched=0`: the Bridge listened
  (`permission.asked`/`permission.updated`), listed (`GET /permission`) and
  replied (`POST /session/:id/permissions/:permissionID`) only on V1
  surfaces while the live request lived on the V2 surface.
- Verified against installed `@opencode-ai/sdk` 1.18.31 (no guessing, no
  private endpoints): V2 ask/reply events `permission.v2.asked`
  (`{id, sessionID, action, resources, save?, metadata?, source?}`) /
  `permission.v2.replied` (`{sessionID, requestID, reply}`); V2
  session-scoped snapshot `client.v2.session.permission.list` →
  `GET /api/session/{sessionID}/permission` (200 `{data: [...]}`); V2
  session-scoped reply `client.v2.session.permission.reply` →
  `POST /api/session/{sessionID}/permission/{requestID}/reply` (body
  `{reply: once|always|reject}`, 204). V1 (`permission.asked`,
  `GET /permission`, deprecated session permissions reply) is retained as
  compatibility fallback/alias.
- Adapter 0.1.8: dedicated V2 normalizer (`resources` → requested targets,
  `save` → exact always scope, never synthesized; `source.callID`
  preserved); `permission.v2.asked`/`permission.v2.replied` normalize to the
  existing internal ask/reply flow with `generation: "v2"`; the V2 snapshot
  is the primary recovery source (a successful empty V2 snapshot never
  consults V1; V1 is used only on missing method, 404/501, or the client's
  explicit "not supported by this version"); replies route explicitly on
  the persisted generation (`generation: "v2"` in the reply body selects
  the V2 endpoint, anything else keeps V1). Operational logs add
  `generation`/`source` (allowlisted) with session/request ids and matched
  counts only — never paths, scopes, resources or metadata.
- Bridge: `agent_requests.generation` (`'v1'|'v2'`, default `'v1'`,
  backward-compatible migration) is persisted on every ask and threaded
  through resync into reply routing — never inferred from request-id
  formatting. Legacy rows keep working as V1; unknown generations fail
  closed. `once`/`always`/`reject` semantics, exact always scope, strict
  session scoping (child-session asks never attach to a root run), dedupe
  by OpenCode request id, and same-session resume are unchanged.
- Tests: V2 pending-recovery while the legacy surface is empty, V2 live
  event without resync, V2 once reply on the V2 endpoint only, V2 empty
  snapshot inventing nothing, V1 fallback only on unsupported, wrong-session
  discard, event+snapshot dedupe, and log hygiene (Python + adapter). An
  opt-in live diagnostic (`tests/test_opencode_live.py`,
  `WB_LIVE_OPENCODE=1`) observes the V2 snapshot/event path read-only: it
  never changes permission policy and never replies.
- Live revalidation is still required after rebuilding and restarting
  (see below). Manual steps: 1) rebuild/restart bridge + adapter
  (`adapter_version` 0.1.8 in `/health`); 2) start a run whose handoff
  needs a permission-gated tool; 3) when the TUI shows the prompt,
  `read_opencode_run` must report `waiting_permission` with
  `pending_request_count=1` and `permission_sync` `ok/matched=1/source=v2`
  instead of repeated `matched=0`; 4) answer once via the Bridge and
  confirm the same session resumes. Do not auto-approve and do not use
  `/tmp` merely to force `external_directory`.

# Unreleased — safe operational container logging (adapter 0.1.7)

- Bridge and adapter now emit one-line JSON operational logs suitable for
  `docker compose logs -f bridge opencode-adapter` (existing json-file
  rotation unchanged). Bridge covers `bridge_ready`, `run_created`,
  `dispatch_started`/`dispatch_failed`, `run_state` transitions
  (waiting_permission, waiting_question, running, completed, failed,
  cancelled, orphaned), `permission_asked`/`permission_replied`,
  `permission_resync` (`ok` with matched count vs `degraded` with sanitized
  code), `event_pump_error`/`event_rejected`, and startup
  `reconcile_start`/`reconcile_result`/`startup_reconcile`. The adapter (0.1.7)
  covers `adapter_ready`, `eventhub_transition`/`eventhub_error`, and
  `permission_list`/`permission_event`. `WB_LOG_LEVEL` (DEBUG/INFO/WARNING/
  ERROR, default INFO) configures both services; invalid values fail fast at
  bridge startup while the adapter falls back to INFO. Uvicorn access logs
  stay disabled. All records carry bounded scalar IDs/state/codes/counts
  only — never prompts, response text, paths, permission scopes/metadata,
  credentials, or raw error bodies — and logging failures can never change
  run state. SQLite audit events and run/permission semantics are unchanged.

# Unreleased — permission-recovery observability (corrects 0.1.5)

- Supersedes the 0.1.5 wording that implied missed asks are always
  recovered. Live validation showed a real `external_directory` ask
  (bash writing outside the project, e.g. `/tmp` scratch) staying
  invisible: the run stayed `running` with `pending_request_count=0`.
  The likely upstream cause is `GET /permission` failing its entire
  response encoding when one pending request carries an undefined
  metadata object (open upstream issue, 2026-07-26); the 0.1.5 adapter
  additionally swallowed every listing failure, so the cause was
  invisible in `read_opencode_run`.
- Permission resync failures are now observable but non-terminal
  (adapter 0.1.6). `_resync_permissions` no longer swallows
  `BridgeError`: a list/binding failure records a bounded sanitized
  `permission_sync` diagnostic (`degraded` with `permission_list_failed`
  or `session_binding`) while the run stays active with
  `pending_request_count=0` and no approval fabricated, approved,
  rejected, resolved, or broadened. A successful listing records
  `ok` with a matched count, so a successful empty list is
  distinguishable from a failed listing; absence from a successful list
  still never resolves a persisted wait. A later successful resync
  containing the exact session ask recovers it idempotently into
  `waiting_permission` and replaces the transient diagnostic.
- Adapter `EventHub` tracks sanitized subscription health
  (`starting`/`subscribed`/`reconnecting` with transition count,
  last-transition time, and consecutive-failure count; no event
  contents or secrets), exposed via `GET /health` as `event_stream`
  and parsed by the Python runtime health/`runtime_status`. A new run
  started while the stream is not confirmed subscribed is marked
  degraded (`event_stream_not_subscribed`) with one bounded probe at
  start — no tight poll, no start failure. Upstream HTTP 400 from the
  adapter now maps to `RuntimeRejected` (a rejection, never `[]`).
- Reconnect/cursor: only observed (forwarded) events advance the
  adapter cursor, so reconnect never skips buffered events. The
  installed `@opencode-ai/sdk` 1.18.31 event-subscribe surface takes
  only directory/workspace with no cursor/last-event-id parameter, so
  gaps across a disconnected upstream stream cannot be replayed; the
  bounded in-adapter ring buffer replays only what it observed. No
  second recovery source was added: sibling generated surfaces
  (session-scoped `/api/session/{id}/permission`, location-scoped
  `/api/permission/request`) are unverified against the installed
  native server, so remote recovery remains best-effort while upstream
  listing is broken. No TUI scraping, internal DB/state reads, private
  endpoints, scope inference, auto-approval, or permission-policy
  change. Live revalidation against a real OpenCode server with a real
  external-directory ask is still required; unit coverage alone does
  not prove the live path.

# Unreleased — missed permission-ask recovery

- A missed `permission.asked` event no longer leaves a run falsely `running`
  forever. The adapter (0.1.5) adds a narrow session-scoped pending-permission
  read (`GET /sessions/:id/permissions?directory=...` over the official
  `@opencode-ai/sdk` 1.18.31 `permission.list` / `GET /permission` surface,
  which returns `PermissionV1.Request` items with `sessionID`); listed
  requests normalize through the same canonical mapping as live events
  (`requested_patterns` separately reviewable, `pattern` exactly OpenCode's
  proposed always scope, bounded/sanitized metadata, no secret persistence).
  `read_opencode_run` resyncs an active run before reporting state, restart
  reconciliation resyncs before inspecting persisted waits, and a bounded
  background sweep (at most one listing per distinct starting/running
  session every 60s) recovers notifications without a status poll. Recovery
  is idempotent, strictly session-scoped, and fail-closed: list errors or
  malformed responses never become an empty success, and absence from the
  listing never auto-resolves an already persisted request. Responding once /
  always / reject on a recovered request works exactly as on an
  event-captured one and resumes the same session. No permission policy,
  auto-approval, or unrelated surface changes. Known upstream limitation:
  OpenCode v1 `GET /permission` may itself fail encoding when a pending
  request metadata object contains undefined; that failure stays retryable
  and pending permissions are not durable across an OpenCode server restart.

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
