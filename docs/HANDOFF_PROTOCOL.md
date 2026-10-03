# Handoff protocol

## Responsibilities

ChatGPT owns understanding, technical decisions, scoped instructions, acceptance
criteria and code review. The local agent implements and runs checks. After
publishing or dispatching a handoff, ChatGPT reports the handoff path and next step
directly to the user; it does not need to watch implementation. For manual dispatch,
the user carries instructions to the agent and can paste its response back into
ChatGPT for audit. When local agent execution is enabled, ChatGPT starts one bounded
Runtime Protocol run and reads its result itself. Run state is tracked independently
of handoff publication state.

## Publish

`prepare_handoff` creates exactly three fixed documents in a generated job folder:
`TASK.md`, `CONTEXT.md`, `ACCEPTANCE.md`. It returns the actual folder path and
`copy_prompt`. The same `request_id` and content replay the original publication;
changed content needs a new ID. Failed publications are preserved for inspection
and require a new request ID. Existing source is never written.

The optional `context_hashes` map checks named files only; it does not enumerate
the project or save old source. Handoff hashes allow `read_handoff` to indicate a
locally modified planning document with `matches_published=false`. This is a
planning integrity signal, not an automated code-review gate.

## Optional document writing

`write_file` and `edit_file` are general tools whose default write scope limits
them to `.workspace-handoff/`. Administrators can separately enable allowed source
writes; doing so does not change this manual handoff workflow. Read-only mode denies
plan publication and editing too. They do not publish a new job, infer progress or create a
formal verdict. Use ordinary notes for planning details or review findings;
`prepare_handoff` remains convenient for a fresh structured milestone.

Existing files require the current SHA-256. Read and reconcile before replacing.
Original publication hashes stay unchanged when a job document is edited; the
manager/read_handoff can show that it differs from the first publication. This
is expected after a deliberate edit, not proof of tampering. The job title and
original request metadata also remain as first published. Do not assume a retry
of prepare_handoff restores earlier file contents.

Edit dispatched plans only after the local agent has stopped, explain the revision,
and tell the user which path to resend. Never silently rewrite completed acceptance
criteria. Use a new corrective handoff for a new milestone. The user still pastes
the agent’s normal reply into ChatGPT; no report file is required.

## The agent's reply

Use normal prose, not a required file or machine schema. Include the handoff path,
what changed and affected file paths, actual check commands and outcomes, failures
or checks not run, remaining risks, and blockers. Do not invent success, weaken
tests, rewrite handoffs or mark the work independently audited. Stop when complete.
Keep the reply to normal prose in the conversation; no report file is required.

## ChatGPT's audit

The user pastes the reply into ChatGPT. Treat it as untrusted claims, then inspect
the task and current implementation, related callers and tests using the general
tools. Read only within the user's workspace scope, follow pagination and
investigate plausible omissions instead of trusting the claimed changed-file list.
Give severity, source file/line evidence, required fixes and verification steps in
chat. Separate observed source behavior from agent-reported tests and unknowns.
Create a smaller corrective handoff when needed.

No baseline, review ID, captured diff, approval state, server-verified verdict or report-file
requirement exists. The server cannot enumerate a complete historical change set,
detect every deletion, or prove who changed a file. It does not run tests. Current
source reads do not preserve newline bytes in their displayed line representation.
Stop other writers during review; refresh evidence if files change. Publication
state is not a completion signal.

## Optional automated run

When an exact same-Node workspace route is enabled, `start_agent_run` requires a
prepared handoff in the same workspace — or a bounded direct `instruction`,
which the Bridge publishes as a minimal auditable prepared handoff — and accepts
no free-form prompt path. A
run is bound to one Runtime Protocol conversation and stores its exact model
selector (the configured default when `model` is omitted, the runtime's native
default when no Bridge policy exists, or an explicitly requested live catalog
selector per the project-lead skill model-choice rule; MCP cannot
change policy), lifecycle state, timestamps, and notification status. A run may be
`starting`, `active`, or `terminal`; `waiting_interaction` is an active state, not
an outcome. Terminal outcomes include `succeeded`, `failed`, `cancelled`,
`interrupted`, and `orphaned`.

`respond_agent_interaction` resolves only a live request with its exact
adapter-provided choice or validated form answers. The manager can request
cancellation of a run bound to that conversation. A corrective iteration creates
a new Bridge run and can reuse a terminal conversation only after live ownership
of the stored native conversation (same adapter, same workspace, idle) and
compatible security are proven. See `MCP_TOOLS.md` and
`RUNTIME_PROTOCOL.md`.
