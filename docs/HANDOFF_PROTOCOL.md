# Handoff protocol

## Responsibilities

ChatGPT owns understanding, technical decisions, scoped instructions, acceptance
criteria and code review. The local agent implements and runs checks. After
publishing or dispatching a handoff, ChatGPT reports the handoff id and next step
directly to the user; it does not need to watch implementation. For manual dispatch,
the user carries instructions to the agent and can paste its response back into
ChatGPT for audit. When local agent execution is enabled, ChatGPT starts one bounded
Runtime Protocol run and reads its result itself. Run state is tracked independently
of handoff publication state.

## Publish

`prepare_handoff` stores exactly three fixed documents in Bridge state:
`TASK.md`, `CONTEXT.md`, `ACCEPTANCE.md`. Nothing is written into the workspace,
so a handoff never appears in `git status`, and publishing needs no write scope.
It returns the handoff id and, when it fits, a `copy_prompt` that carries the
full text of all three documents; `read_handoff` serves the same prompt as
`PROMPT.md`. The owning Node also saves a read-only copy of the three documents
in its own state, and both prompts name that folder: an agent whose context was
compacted re-reads its plan there. A run may read its handoff's folder but not
write it, and each run start restores a changed copy from Bridge state. The
same `request_id` and content replay the original publication; changed content
needs a new ID.

The optional `context_hashes` map checks named files only; it does not enumerate
the project or save old source. Handoffs published by earlier versions stay in
their workspace `.workspace-handoff/jobs/` folder; for those, `read_handoff`
reports a locally modified planning document with `matches_published=false`.
This is a planning integrity signal, not an automated code-review gate.

For visual context, upload a raster image with `write_image` under
`.workspace-handoff/attachments/`, then include its workspace-relative `path`,
`description` and returned original-file `sha256` in `prepare_handoff.attachments`.
Existing allowed workspace images can also be referenced using the source hash
from `read_file`. Up to 20 unique references are validated and checked at publication,
and recorded in `CONTEXT.md` with their formats, sizes and dimensions. Context and
attachments share a 64 MiB total read budget. Images remain at their referenced
paths; publication does not copy or snapshot them. Verify current hashes before
using them, and stop an implementer before replacing a dispatched reference.
Uploading an image alone does not publish a job or start an agent.

## Revising a plan

A published handoff cannot be edited. Publish a new corrective handoff for a
revision or a new milestone, explain what changed, and tell the user which
handoff to send. Never silently move completed acceptance criteria. The user
still pastes the agent's normal reply into ChatGPT; no report file is required.

## The agent's reply

Use normal prose, not a required file or machine schema. Include the handoff id,
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
detect every deletion, or prove who changed a file. It runs only the checks the
user configured, through `run_checks`, and never the agent's claimed commands. Current
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
adapter-provided choice or validated form answers, once, recording who decided
and why. The Manager cannot answer; it can request cancellation of a run bound to that conversation. A corrective iteration creates
a new Bridge run and can reuse a terminal conversation only after live ownership
of the stored native conversation (same adapter, same workspace, idle) and
compatible security are proven. See `MCP_TOOLS.md` and
`RUNTIME_PROTOCOL.md`.
