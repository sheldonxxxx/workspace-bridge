# Runtime protocol v1

Workspace Bridge owns workspace authorization, handoffs, model policy, run
records, and audit. A runtime adapter owns its native process or SDK, session
files, model syntax, and security enforcement. Pi and Codex adapters implement
the same resource contract; Bridge never sends a vendor RPC to an adapter.

## Resources

| Resource | Meaning |
| --- | --- |
| Conversation | Bridge-owned, long-lived native context, bound to one workspace, runtime, and immutable security profile revision. |
| Run | One accepted operation in a conversation. Pi: one prompt through native idle and a terminal assistant message. Codex: one turn. |
| Activity | One command, file change, tool call, search, subagent action, or other observable action within a run. |
| Interaction | One live blocking request with exact adapter-provided response options or form fields. |

The HTTP adapter surface is token authenticated and private:

```text
GET    /v1/descriptor
GET    /v1/models?workspaceId=...
GET    /v1/profiles
POST   /v1/profiles
DELETE /v1/profiles/{id}
POST   /v1/conversations
GET    /v1/conversations/{id}
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

`descriptor` has `protocol: {major: 1, minor: 0}`, a stable runtime ID,
adapter/native versions, an instance ID, and a map of feature names to positive
integer contract versions. Missing features mean unsupported. The required v1
features are `models`, `conversations`, `runs`, `activities`, and
`interactions`; `events`, `steering`, and `imageInput` are optional. A feature
may be advertised only when its full contract is implemented.
Unknown optional feature names and higher optional feature versions are
ignored by this v1 coordinator; the five core features must remain version 1.

## State and admission

A run has `phase` (`starting`, `active`, `terminal`), `activeState`
(`running`, `waiting_interaction`, or null outside the active phase), and
`outcome` (`succeeded`, `failed`, `cancelled`, `interrupted`, or `orphaned`
only in the terminal phase). An interaction is a separate record; waiting is
not an outcome. Final assistant text is evidence, never proof of file changes.

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

The local manager can create, edit, and delete named custom security profiles.
`GET /profiles` returns each profile's native `config`, revision, and `mutable`
flag so the manager can show only controls that runtime implements. The
adapter validates the full config on `POST`; edits require the prior revision.
Built-in profiles are immutable starting points. Custom definitions persist in
the adapter's private state, and the Bridge updates assigned workspace
revisions after a successful edit. New conversations use the new revision;
continuation from an older revision is refused. Deletion requires all
workspaces to be reassigned and no active native run for that profile.

Model records may also include `reasoningOptions` (supported effort strings)
and `defaultReasoningEffort` (the native default when the adapter reports one).
An admin may save a `reasoning_defaults` map alongside the enabled model policy.
When a model has an explicit default in that map, Bridge sends it as the
run's optional `reasoning` value; when omitted, the adapter keeps its native
per-model behavior. The adapter must reject an effort the selected model does
not support.

For example, Pi exposes `off`, `minimal`, `low`, `medium`, `high`, `xhigh`
(Extra high), and `max` when supported by that model. Codex efforts come from
the live app-server model list and are passed to `turn/start` as `effort`.

The Bridge admits a run only when the workspace is enabled, the workspace-wide
agent switch is enabled, the selected runtime is explicitly enabled for that
workspace, the handoff is prepared in that workspace, the selected model is
allowed, and the conversation binding matches. Installing a new runtime
never grants access to any existing workspace.

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
