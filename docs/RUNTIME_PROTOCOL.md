# Runtime protocol v1

Workspace Bridge owns workspace authorization, handoffs, model policy, run
records, and audit. An authoritative Node owns the workspace filesystem/Git/
handoff data plane and runtime adapter registry/secrets. A runtime adapter owns
its native process or SDK, session files, model syntax, and security enforcement.
Pi and Codex adapters implement the same resource contract; Bridge never sends a
vendor RPC directly to an adapter.

## Bridge routing identities

`RuntimeType` is the protocol family (`pi` or `codex`) in the native descriptor.
It describes behavior and does not identify a destination. `AdapterInstance` is
one Node-owned destination with an opaque `adapter_id`, unique display name,
runtime type, Node-local/reachable base URL, Node-held per-instance token,
enabled state, and connection revision. Multiple AdapterInstances can share one
runtime type. `WorkspaceRoute` is the exact `(workspace_id, adapter_id)`
permission and security binding, and its adapter must belong to the workspace's
authoritative Node.

Bridge stores sanitized adapter references, Node credentials, model policies,
routes, conversations, and runs in private SQLite. Its local Manager owns
Bridge-to-Node connection settings; each Node stores adapter credentials in
private Node SQLite. The native Pi/Codex daemon's listen port, bootstrap token, state path,
and process lifecycle remain configured on that host. A daemon's own
`WB_RUNTIME_TOKEN` is its HTTP credential; it is not a Bridge-wide registry or
shared Bridge environment variable.

## Resources

| Resource | Meaning |
| --- | --- |
| Conversation | Bridge-owned native context bound to one workspace, exact same-Node `adapter_id`, Node/adapter connection revisions, descriptive `runtime_type`, security source, and applied security revision. |
| Run | One accepted operation on one AdapterInstance in a conversation, with immutable Node/adapter revision and effective-security evidence. Pi: one prompt through native idle and a terminal assistant message. Codex: one turn. |
| Activity | One command, file change, tool call, search, subagent action, or other observable action within a run. |
| Interaction | One live blocking request with exact adapter-provided response options or form fields. |

The HTTP adapter surface is token authenticated and private:

```text
GET    /v1/descriptor
GET    /v1/models?workspaceId=...
GET    /v1/profiles[?workspaceId=<id>&directory=<absolute-path>&fresh=1]
POST   /v1/profiles
DELETE /v1/profiles/{id}
POST   /v1/conversations
GET    /v1/conversations/{id}
POST   /v1/conversations/{id}/security
POST   /v1/conversations/{id}/runs
GET    /v1/conversations/{id}/runs/{clientRunId}
GET    /v1/runs/{id}
POST   /v1/runs/{id}/cancel
POST   /v1/runs/{id}/steer
GET    /v1/runs/{id}/interactions
POST   /v1/interactions/{id}/resolve
GET    /v1/runs/{id}/activities
GET    /v1/activities/{id}
GET    /v1/events?after=...&waitMs=...
```

`descriptor` has `protocol: {major: 1, minor: 0}`, a stable runtime-type ID,
adapter/native versions, an instance ID, and a map of feature names to positive
integer contract versions. Missing features mean unsupported. The required v1
features are `models`, `conversations`, `runs`, `activities`, and
`interactions`; `events`, `steering`, `imageInput`, and `securityRebind` are optional. A feature
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
while the run is active. Codex reports the owned turn's `tokenUsage.last`
snapshot (never cumulative thread totals); Pi aggregates provider-reported
assistant/model-call usage for that run's tool loop (never streaming snapshots
or previous conversation turns). The field is omitted when native usage is
unavailable; present counters must be non-negative safe integers and absent
counters are never synthesized as zero or estimated. Unknown or malformed
usage fails closed as an invalid run snapshot. Already-consumed usage remains
visible on failed, interrupted, and cancelled runs when the native runtime
reported it.

`POST /conversations/{id}/runs` is **idle-only**. It must either accept exactly
one new run or return `409 conversation_busy`; it must never queue or steer.
Steering requires a separate request with the exact expected active run ID.
Bridge supplies a unique `clientRunId` on admission. The adapter stores it
before asking its native engine to start and returns the same run for a repeat
with identical input. After a lost response or Bridge restart, the lookup above
rebinds only that owned native run; no prompt is replayed.
The adapter refuses conversations it did not create and refuses a workspace or
profile mismatch. A model selector is an opaque string returned by the adapter
and is passed back unchanged. Model changes do not alter the conversation's
security binding.

The local Manager discovers and edits security profiles through the selected
Node-owned AdapterInstance. `GET /profiles` returns each profile's opaque config, revision, and mutable flag
so the manager can show only controls that runtime implements. A runtime may
need workspace context to discover profiles. In that case Bridge supplies the
exact workspace ID and a validated directory; the adapter independently
checks the directory beneath its configured project root. Pi may ignore this
context. Codex uses it to resolve native permission-profile IDs and effective
security revisions. For Codex, the response can also include a separate
`runtimeConfig` observation containing an opaque revision and bounded summary;
it is not a profile row. Raw native rules and managed config are never exposed.

WorkspaceRoute bindings identify `source: profile` with a profile ID/revision or
`source: runtime-config` with no profile ID. The latter is supported only by a
Codex AdapterInstance and follows that destination's current config for the
workspace; revision changes do not invalidate the binding. A conversation
records its adapter ID, runtime type, adapter connection revision, applied
security revision, and bounded security summary.
Before a later Codex turn, the adapter verifies the thread is idle, reads the
current native security state, and applies supported changes through
`thread/settings/update` before `turn/start`. It confirms
`thread/settings/updated` and updates the saved conversation snapshot only
after confirmation. The active turn keeps its captured settings. If the
change cannot be represented or confirmed safely, the adapter creates a fresh
native conversation and reports a bounded replacement reason.

Profile-bound conversations record the live observed revision for new runs; the stored workspace-route revision is last-bound evidence (`bound_revision` vs `observed_revision`). An explicit `continue_from_run_id` authorizes carrying the existing conversation across a named-profile ID/revision change: when the adapter advertises the optional `securityRebind` capability, Bridge calls the bounded `rebind_conversation(conversation_id, security_binding)` (`POST /v1/conversations/{id}/security`) before the next prompt, requires the same runtime conversation ID/history to be idle and to report the requested `{source:'profile', profile:{id,revision}}`, updates the Bridge conversation only after proof, and records a bounded `security_binding_rebound` activity with only old/new source/profile IDs/revisions and conversation IDs. Busy, unconfirmed, or mismatched rebinds fail before any prompt with no blank-thread fallback; an ambiguous accepted-but-unproven native update invalidates the runtime conversation (requiring a fresh conversation) rather than continuing with unproven permissions, and adapters without `securityRebind` remain protocol-compatible but cannot transition. A proven rebind is audited with `security_binding_rebound` before the next prompt is sent, so even a rejected run start leaves the transition durably auditable. Adapters persist an in-progress rebind marker before native mutation; a restart encountering an incomplete rebind refuses ownership and requires a fresh conversation. Markers carry no profile config. A workspace binding-source change still requires a conversation created for the new source. Continuation itself is proven by current runtime ownership, not by history: any terminal source run (succeeded, failed, cancelled, or interrupted) may continue, the model may change between turns, and stored Node/adapter revision drift alone does not invalidate the conversation — Bridge reads (or rebinds) the stored native conversation on the current same-Node adapter and requires it to exist, belong to this workspace, and be idle before sending a prompt; after that proof it refreshes only the conversation's current Node/adapter metadata and never rewrites historical run evidence. Missing, foreign, mismatched, or busy conversations fail closed before any prompt. Built-in profiles are immutable starting points. Custom definitions
persist in the adapter's private state; edits require the prior definition
revision. Deletion requires all workspaces to be reassigned and no active
native run for that profile.

Codex wrapper profiles contain only permissions, approvalPolicy, and
approvalsReviewer. The adapter sends the native permissions selector and
omits legacy sandbox from thread start/resume requests; permissions and
sandbox are mutually exclusive in the Codex protocol. Codex permission
definitions, filesystem/network rules, and config layering remain Codex-owned.

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

The Bridge admits a run only when the workspace is enabled, the exact same-Node
WorkspaceRoute and AdapterInstance are enabled, the handoff is prepared in that
workspace (or the run was published as one from a bounded direct instruction),
the requested model is allowed by the adapter's configured policy (or, with no
Bridge policy, is simply present in the live catalog when explicitly requested),
the Node is reachable, and the conversation binding matches. Each accepted run
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
advertise `imageInput`; the current Pi and Codex adapters advertise text only.

## Recovery

When an adapter advertises events, they have an instance ID and monotonic
cursor. They are latency hints; conversation, run, interaction, and activity
snapshots are the source of truth. Bridge currently polls active run snapshots
and does not rely on an event cursor. On restart it reconciles those records;
it may rebind a live run or interaction only when the adapter positively
identifies the same native operation/request. Otherwise the run becomes
`interrupted` or `orphaned` and its interactions become stale. It never replays
a prompt or approval on recovery.

The adapter may resume a conversation it created. It must never adopt a Pi TUI
session, Codex Desktop/TUI thread, or another client's context. Pi's extension
policy and Codex's native sandbox/approval policy are different enforcement
mechanisms; a security profile describes the adapter's exact claims and never
asserts they are equivalent.

## Conformance gate

A second adapter must run against one Bridge coordinator without changing its
state machine, database schema, MCP schemas, workspace authorization logic,
or generic admin routes. Conformance tests cover idle-only admission, exact
interaction routing, opaque models, ownership, and restart reconciliation.
Events are optional and consumed only by adapters that advertise them.
Adapter-specific management (credentials, packages, process restart) stays
outside this protocol. Profile configuration is an explicit local-admin
operation through Bridge and the private adapter surface, never an MCP tool.
