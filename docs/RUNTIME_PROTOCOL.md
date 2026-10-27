# Runtime protocol

Workspace Bridge owns workspace authorization, handoffs, model policy, run
records, and audit. An authoritative Node owns the workspace filesystem/Git/
handoff data plane and supervises its agent plugins. An agent plugin owns
its native process or SDK, session files, model syntax, and security enforcement.
Pi, Codex, Claude Code, and OpenCode plugins implement the same resource contract;
Bridge never sends a vendor RPC directly to an agent. The resource model below
is version 1 (descriptors report `protocol.major` 1); it travels over
[Agent Protocol v2](#agent-protocol-v2-stdio-plugins), JSON-RPC over the
plugin's stdio.

## Bridge routing identities

`RuntimeType` is the protocol family (`pi`, `codex`, `claude`, or `opencode`) in the native descriptor.
It describes behavior and does not identify a destination. `AdapterInstance` is
one Node-owned destination with an opaque `adapter_id`, unique display name,
runtime type, plugin, projects root, plugin settings, enabled state, and
configuration revision. Multiple AdapterInstances can share one
runtime type. `WorkspaceRoute` is the exact `(workspace_id, adapter_id)`
permission and security binding, and its adapter must belong to the workspace's
authoritative Node.

Bridge stores sanitized adapter references, Node credentials, model policies,
routes, conversations, and runs in private SQLite. Its local Manager owns
Bridge-to-Node connection settings; each Node stores its plugin adapters in
private Node SQLite and runs them as child processes. Plugins have no port
and no credential.

## Resources

| Resource | Meaning |
| --- | --- |
| Conversation | Bridge-owned native context bound to one workspace, exact same-Node `adapter_id`, Node/adapter connection revisions, descriptive `runtime_type`, security source, and applied security revision. |
| Run | One accepted logical operation on one AdapterInstance in a conversation, with immutable Node/adapter revision and effective-security evidence. It includes the initial prompt and any explicitly admitted corrections or queued follow-ups, through native settlement. |
| Activity | One command, file change, tool call, search, subagent action, or other observable action within a run. |
| Interaction | One live blocking request with exact adapter-provided response options or form fields. |

The operations, written as their version 1 routes (the v2 method table below
maps each one to a JSON-RPC method):

```text
GET    /v1/descriptor
GET    /v1/models?workspaceId=...
POST   /v1/conversations
GET    /v1/conversations/{id}
POST   /v1/conversations/{id}/runs
GET    /v1/conversations/{id}/runs/{clientRunId}
GET    /v1/runs/{id}
POST   /v1/runs/{id}/cancel
POST   /v1/runs/{id}/steer
POST   /v1/runs/{id}/queue
GET    /v1/runs/{id}/interactions
POST   /v1/interactions/{id}/resolve
GET    /v1/runs/{id}/activities
GET    /v1/activities/{id}
GET    /v1/events?after=...&waitMs=...
GET    /v1/usage-limits          (optional `usageLimits` feature)
```

`descriptor` has `protocol: {major: 1, minor: 0}`, a stable runtime-type ID,
adapter/native versions, an instance ID, and a map of feature names to positive
integer contract versions. Missing features mean unsupported. The required v1
features are `models`, `conversations`, `runs`, `activities`, and
`interactions`; `events`, `steering`, `queueing`, `inputControls`, `imageInput`, and
`usageLimits` are optional. A feature
may be advertised only when its full contract is implemented.
Unknown optional feature names and higher optional feature versions are
ignored by this v1 coordinator; the five core features must remain version 1.

## State and admission

A run has `phase` (`starting`, `active`, `terminal`), `activeState`
(`running`, `waiting_interaction`, or null outside the active phase), and
`outcome` (`succeeded`, `failed`, `cancelled`, `interrupted`, or `orphaned`
only in the terminal phase). An interaction is a separate record; waiting is
not an outcome. Final assistant text is evidence, never proof of file changes.

A run snapshot may carry an optional additive `usage` object with normalized
native provider counters: `inputTokens`, `cachedInputTokens`,
`cacheWriteInputTokens`, `outputTokens`, `reasoningOutputTokens`, and
`totalTokens`. Usage is run-scoped for exactly one Bridge run, including
continuation runs which start a fresh accounting boundary, and may be partial
while the run is active. Codex derives the owned turn's aggregate as the
fieldwise delta of the stable cumulative `tokenUsage.total` snapshots
(`total - baseline`, where the baseline is the predecessor's cumulative
thread total captured at admission and persisted for restart); `tokenUsage.last`
carries only the newest model response and is never used alone as the run
total. Claude Code sums confirmed result usage across responses, with
per-message usage for the active response added to the interim value. Pi aggregates provider-reported assistant/model-call usage for that
run's tool loop (never streaming snapshots or previous conversation turns).
Duplicate `tokenUsage.total` notifications are idempotent snapshots (never
summed), and decreasing/reset or malformed cumulatives are dropped fieldwise
without fabricating counters. The field is omitted when native usage is
unavailable; present counters must be non-negative safe integers and absent
counters are never synthesized as zero or estimated. Unknown or malformed
usage fails closed as an invalid run snapshot. Already-consumed usage remains
visible on failed, interrupted, and cancelled runs when the native runtime
reported it.

Run-scoped token usage is separate from account-level quota. An adapter that
advertises the optional `usageLimits` feature exposes
`GET /v1/usage-limits` (no parameters, no workspace context): a normalized
account usage-limits snapshot `{available, ordinaryUsageAllowed, buckets}`.
Each bucket mirrors one native RateLimitSnapshot: bounded metadata
(`limitId`, `limitName`, `planType`, `rateLimitReachedType`,
`spendControlReached`, bounded credits/spend-control observations when
present) plus a nested `windows` list normalized from the native
`primary` then `secondary` RateLimitWindow when present, each window
preserving `usedPercent` and `resetsAt`/`windowDurationMins` (null stays
null) and deriving `remainingPercent` = clamp(100 - usedPercent) for
display only. Labels come only from `windowDurationMins`; primary/secondary
meanings are never assumed. A bucket with neither valid windows nor usable
metadata is dropped; remaining-quota display always requires at least one
valid window, and missing data is never rendered as 0% remaining.
Runtime-specific raw payloads (account IDs, upsell data) and unknown native
fields never cross the adapter boundary. Failed, unsupported, or empty
reads surface as an explicit unavailable state, not as adapter
unhealthiness, and quota is never part of `/api/status`.

`POST /conversations/{id}/runs` is **idle-only**. It must either accept exactly
one new run or return `409 conversation_busy`; it must never queue or steer.
Steering and queueing require separate requests with the exact expected active run ID.
Bridge supplies a unique `clientRunId` on admission. The adapter stores it
before asking its native engine to start and returns the same run for a repeat
with identical input. After a lost response or Bridge restart, the lookup above
rebinds only that owned native run; no prompt is replayed.
The adapter refuses conversations it did not create, a workspace mismatch, and
a security block it cannot validate. A model selector is an opaque string returned by the adapter
and is passed back unchanged. Model changes do not alter the conversation's
security binding.

`POST /runs/{id}/steer` and `/queue` accept
`{expectedRunId, requestId, input:[{type:"text", text:"..."}]}`. The Bridge
sends one bounded text instruction. Each adapter advertises contract version 1
of `steering` and `queueing` only when supported. The additional `inputControls:1`
feature confirms the request-ID and receipt contract below; the Bridge requires
it before sending either operation, so older steering-only adapters fail before
receiving input. A request ID is unique per
run across both modes; exact retries return the stored receipt without
resubmission, including after a terminal outcome or restart. Conflicting input
or mode returns `409 idempotency_conflict`. New input requires an active owned
run; admission is serialized against native completion and cancellation.

Queueing follows the current successful response in FIFO order, within the
same run and native conversation under its captured settings. The adapter
advances queued work without Bridge reads. Codex starts additional owned native
turns; Pi uses its native follow-up queue; Claude uses successive queries on the
same client. Steering uses Codex/Pi native steering or Claude's native
interrupt followed by a new correction. It never replays the original prompt.
The run stays active until all admitted work settles; its usage includes those
responses and its result is the last response.

Snapshots may contain `inputMessages`, up to 50 text-free receipts
`{requestId, mode, status}`. Status is `queued`, `delivered` (native submission),
`discarded`, or `unknown` (delivery unconfirmed). There may be at most 20
pending inputs. Adapters persist metadata and input hashes for idempotency,
while pending text exists only in the live native/adapter process. Cancellation,
terminal failure, and restart clear pending input without resubmission.
Ambiguous delivery is retained as unknown and never retried automatically.

## Security on every run

Security profiles live in the Bridge, not in plugins. A profile is a set of
rules: the categories `read_inside`, `read_outside`, `write_inside`,
`write_outside`, `shell`, `web`, and `extensions`, each `deny`, `ask`, or
`allow`, plus `protected` paths, `external_roots` with their own read and write
modes, and `commands` prefix rules for the shell. Paths under `.git`, `.env`
files (templates such as `.env.example` excepted) and the root `checks.toml`
are always protected. The built-in profiles are `read-only`, `reviewed`, and
`trusted`; see [Security](SECURITY.md#security-profiles). A profile's revision
is `prof_` followed by the first 32 hex characters of the SHA-256 of its
canonical JSON, so the plugin can recompute it.

Each WorkspaceRoute names a profile or, where the plugin supports it, the
agent's native security. Every `POST /conversations` and
`POST /conversations/{id}/runs` body carries a `security` block, one of:

```json
{"source": "profile", "profile": {"id": "reviewed", "revision": "prof_…", "rules": {}}}
{"source": "native"}
```

The plugin validates the rules and their revision (`400 invalid_profile`
otherwise), enforces them for the whole run, and echoes `securityBinding`
(`{"source": "profile", "profile": {"id", "revision"}}` or
`{"source": "native"}`) on the conversation and on the run. The Bridge refuses
a run whose echo differs from what it sent (`binding_mismatch`) and checks the
live run's binding again before steering or queueing. A run keeps the rules it
started with. A profile edit reaches the next run, including a continuation of
the same conversation, with no separate step. Switching a route between a
profile and native security needs a fresh conversation
(`security_source_changed`).

A plugin manifest lists the rules its plugin enforces in `enforces`, and the
Node reports them with the plugin's adapters. The Bridge refuses to route a
profile whose restrictive rules the plugin cannot enforce
(`security_unavailable`), and refuses native security unless the manifest
declares the `native_security` feature.

How each plugin enforces the rules:

- **Claude Code**: a closed built-in tool list, plus a pre-tool hook that
  judges every tool call, sub-agent and MCP calls included. Edits never reach
  `.claude` or `.mcp.json`, and `extensions: deny` keeps MCP servers from
  starting. With native security, Claude Code keeps its own tools and
  permission settings in the plugin's `permission_mode` (or the settings'
  `defaultMode`), and each permission prompt it raises becomes an
  interaction. Bypass mode stays disabled either way.
- **Pi**: the trusted permission extension judges every built-in tool call, and
  tools the rules can never allow are not offered. A continuation under a new
  revision reopens the idle session from its session file with the new rules.
- **OpenCode**: the bundled permission plugin judges every tool call. Edits
  never reach `.opencode`, `.agents`, or `opencode.json`.
- **Codex**: profile threads run in Codex's `:read-only` permission profile
  with `approvalPolicy: on-request` and `approvalsReviewer: user`. Codex asks
  before each write or command that leaves its sandbox, and the plugin answers
  from the rules: commands by `commands` and then `shell`, file changes by the
  paths they touch, and requests for extra sandbox permissions are always
  declined. Only `ask` reaches the Bridge as an interaction. Reads, web access,
  and extensions inside the sandbox are not gated, so Codex enforces only
  `write_inside`, `write_outside`, `protected`, and `commands`. With native
  security, Codex runs under its own configuration and each native approval
  becomes an interaction. Threads created before 1.0 are refused
  (`security_source_changed`).

In every plugin, `ask` becomes a Bridge interaction.

A continuation is proven by current runtime ownership, not by history. Any
terminal source run (succeeded, failed, cancelled, or interrupted) may
continue, and the model may change between runs. Before sending a prompt, the
Bridge reads the stored native conversation on the current same-Node adapter
and requires it to exist, belong to this workspace, and be idle; Node or
adapter revision drift alone does not invalidate it. After that proof the
Bridge refreshes only the conversation's current Node and adapter metadata and
never rewrites historical run evidence. Missing, foreign, or busy
conversations fail closed before any prompt.

Model records may also include `reasoningOptions` (supported effort strings)
and `defaultReasoningEffort` (the native default when the adapter reports one).
An admin may save a `reasoning_defaults` map alongside an enabled model policy.
When a model has an explicit default in that map, Bridge sends it as the
run's optional `reasoning` value; when omitted, the adapter keeps its native
per-model behavior. The adapter must reject an effort the selected model does
not support.

For example, Pi exposes `off`, `minimal`, `low`, `medium`, `high`, `xhigh`
(Extra high), and `max` when supported by that model. Codex efforts come from
the live app-server model list and are passed to `turn/start` as `effort`.
Claude Code models and supported efforts (`low`, `medium`, `high`, `xhigh`,
`max`) come from the live Claude Code model list; the `default` selector
sends no model so Claude Code uses its own default.

The Bridge admits a run only when the workspace is enabled, the exact same-Node
WorkspaceRoute and AdapterInstance are enabled, the handoff is prepared in that
workspace (or the run was published as one from a bounded direct instruction),
the requested model is allowed by the adapter's configured policy (or, with no
Bridge policy, is simply present in the live catalog when explicitly requested),
the Node is reachable, and the plugin can enforce the route's profile. Each accepted run
stores a bounded immutable
`effective_security` snapshot alongside the Node and adapter revisions.
Installing an AdapterInstance never grants access to any existing workspace.

## Interactions and evidence

An interaction has an adapter-owned opaque ID, native request ID, exact run ID,
kind (`choice`, `grant`, or `form`), state, and bounded display data. Choices
carry opaque IDs and optional semantic labels; Bridge forwards the selected ID
and cannot synthesize a broader grant. Form answers are validated against the
adapter's field description. Resolving requires a live matching native request;
a persisted copy alone is insufficient. Interaction content is redacted and
bounded before entering SQLite or MCP output.

Activities have stable IDs within a run and kinds `command`, `file_change`,
`tool_call`, `search`, `subagent`, or `other`. Snapshots contain bounded input
and result summaries, with no environment variables, credentials, hidden
reasoning, or full output bodies. Image input is reserved for adapters that
advertise `imageInput`; the current Pi, Codex, and Claude Code adapters advertise text only.

## Recovery

A run is bound to the Node and adapter connection revisions it started
under. When either revision changes, Bridge marks the run `interrupted` with
stale interactions instead of contacting the replacement endpoint, and frees
its conversation. Disabled Nodes and adapters keep their revisions and their
runs stay recoverable. The background sweep only reconciles runs whose
authority is currently usable.

When an adapter advertises events, they have an instance ID and monotonic
cursor. They are latency hints; conversation, run, interaction, and activity
snapshots are the source of truth. Bridge reads events for active runs on the
exact Node/adapter authority and reconciles bound snapshots after matching
hints. A page's envelope cursor can be ahead of its delivered events; readers
advance to the final delivered cursor, detect gaps and instance changes, and
refresh snapshots on reconnection. Event cursors are process-local and never
authorize a state transition. A periodic snapshot sweep remains available for
adapters without events, lost hints, and failed event reads. On restart Bridge
reconciles those records;
it may rebind a live run or interaction only when the adapter positively
identifies the same native operation/request. Otherwise the run becomes
`interrupted` or `orphaned` and its interactions become stale. It never replays
a prompt or approval on recovery.

The authenticated local Manager receives separate SSE invalidation hints at
`GET /api/run-events`, optionally filtered by `run_id`. `run.changed` contains
only a Bridge run ID; `reset` tells the UI to reload its REST projections.
Every connection and reconnection starts with a reset, including when the
client supplies `Last-Event-ID`; there is no durable SSE replay. Slow clients
receive a reset when bounded queues overflow. Session expiry, logout, or
password changes terminate existing streams. This Manager transport is
independent of [MCP run events](EVENTS.md) and their signed webhook outbox.

The adapter may resume a conversation it created. It must never adopt a Pi TUI
session, Codex Desktop/TUI thread, a Claude Code terminal session, or another
client's context. The Claude Code adapter proves a session is its own by
creating it with an adapter-chosen session ID, confirming that ID in the
native init message of every run, and checking the session still exists
before it resumes. Each plugin enforces a profile in its own way (see
[Security on every run](#security-on-every-run)); `enforces` states exactly
which rules it covers.

## Agent Protocol v2 (stdio plugins)

Version 2 keeps the resource model above and replaces HTTP with JSON-RPC 2.0
over the plugin's stdin and stdout. The Node starts the plugin from its
manifest, owns both pipes, and is the only client. Each message is one UTF-8
JSON object on one line, at most 16 MiB. Nothing else may be written to the
plugin's stdout; logs go to stderr, which the Node keeps in a size-capped
`plugin.log`.

1. The Node sends `initialize` with `{protocol: 2, state, projectsRoot,
   worktreesRoot, settings, adapterId}`. `state` is a private folder for the
   plugin; `settings` holds only the keys the manifest lists, plus `log_level`.
   `worktreesRoot` is the Node's folder of run worktrees: a plugin that
   declares the `worktrees` feature accepts a conversation `directory` that
   is exactly `<worktreesRoot>/<workspace>/<worktree>`, besides folders under
   `projectsRoot`, and nothing else under `worktreesRoot`. The
   plugin answers `{protocol: 2, instanceId}` once it is ready, even if its
   native runtime is not usable yet; that is reported per request.
2. Requests use these methods, with path parameters and query values as
   camelCase params and any v1 request body as `body`:

| v2 method | v1 route |
| --- | --- |
| `descriptor`, `models`, `usageLimits` | `GET /v1/descriptor`, `/v1/models`, `/v1/usage-limits` |
| `conversations.create`, `conversations.get` | `POST /v1/conversations`, `GET /v1/conversations/{id}` |
| `runs.start`, `runs.find` | `POST /v1/conversations/{id}/runs`, `GET .../runs/{clientRunId}` |
| `runs.get`, `runs.cancel`, `runs.steer`, `runs.queue` | `GET /v1/runs/{id}`, `POST .../cancel`, `.../steer`, `.../queue` |
| `runs.interactions`, `runs.activities` | `GET /v1/runs/{id}/interactions`, `.../activities` |
| `interactions.resolve`, `activities.get` | `POST /v1/interactions/{id}/resolve`, `GET /v1/activities/{id}` |

3. A failed request returns `error: {code: -32000, message, data: {status,
   code}}`, where `status` and `code` keep their v1 meaning (409
   `conversation_busy`, 501 unsupported, and so on).
4. A plugin that advertises `events` pushes `{method: "event", params:
   {instanceId, event}}` notifications as events happen. The Node buffers
   the latest 1,000 and serves the Bridge's long poll from them; snapshots
   stay authoritative.
5. `shutdown`, or end of stdin, asks the plugin to close cleanly. The Node
   stops the whole process group if the plugin has not exited within
   five seconds.

A manifest is a TOML file:

```toml
id = "myagent"            # lowercase name used by `adapter add`
version = "1.0.0"
protocol = 2
runtime_type = "pi"       # pi, codex, claude or opencode for now
command = ["myagent-plugin", "--stdio"]
settings = ["binary"]     # setting keys the plugin accepts
# Profile rules the plugin enforces; a route refuses profiles that restrict others.
enforces = ["read_inside", "read_outside", "write_inside", "write_outside", "shell",
            "web", "extensions", "protected", "external_roots", "commands"]
# Informational, except native_security (a route may use the agent's own security)
# and worktrees (runs may use isolation: "worktree").
features = ["steer", "queue"]
```

Install a third-party manifest with `workspace-bridge node plugin add
<manifest.toml>`. The Node runs the command without a shell, in its own
process group, with the Node's environment minus every `WB_*` variable.

## Conformance gate

A second adapter must run against one Bridge coordinator without changing its
state machine, database schema, MCP schemas, workspace authorization logic,
or generic admin routes. Run the conformance kit on a plugin before adding
it to a Node:

```sh
workspace-bridge plugin test path/to/manifest.toml
workspace-bridge plugin test path/to/manifest.toml --live
```

The kit starts the plugin in a temporary folder of its own, never a real
workspace, and exits non-zero unless every check passes or is skipped.
Without `--live` it runs only the checks that start no agent turn: the
descriptor, refusal of unknown methods, opaque model selectors, refusal of a
tampered or missing security block, the projects root, the security echo on
a new conversation, and ownership (another plugin's or an unknown
conversation, run or interaction is never adopted). With `--live` it also
runs the agent and checks idempotent run start, that an unknown model never
succeeds, that a command its profile asks about pauses on an interaction
(running it unasked fails), idle-only admission while that interaction
waits, events that agree with snapshots, recovery after a kill without
replaying the prompt or the approval, and a continuation afterwards. A live
check the agent's own choices leave undecided is reported as inconclusive.
Give a built-in plugin's id (`claude`, `codex`, `opencode`, `pi`) instead of a
path to test it; `--model`, `--set key=value`, `--timeout`, `--keep` and
`--json` adjust a run. Live checks use the agent's own login and quota.
Events are optional and consumed only by adapters that advertise them.
Adapter-specific management (credentials, packages, process restart) stays
outside this protocol. Profiles are edited by a local admin in the Bridge
(`/api/profiles`), never through an MCP tool.

OpenCode v2 is a first-party Python host adapter using native v2 sessions,
messages, inbox delivery, permissions, forms, and durable logs. Its native
storage is shared with the terminal; exact adapter-owned IDs govern recovery,
not database isolation. See [Runtimes](RUNTIMES.md#opencode-v2).
