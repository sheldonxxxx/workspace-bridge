# Handoff protocol

## Responsibilities

ChatGPT owns understanding, technical decisions, scoped instructions, acceptance
criteria and code review. The local agent implements and runs checks. After
publishing or dispatching a handoff, ChatGPT reports the handoff id and next step
directly to the user; it does not need to watch implementation. For manual dispatch,
the user carries instructions to the agent and can paste its response back into
ChatGPT for audit. When local agent execution is enabled, ChatGPT starts one bounded
run with `start_run` and reads its result itself. Run state is tracked independently
of handoff publication state.

## Publish

`save_handoff` stores exactly three fixed documents in Bridge state:
`TASK.md`, `CONTEXT.md`, `ACCEPTANCE.md`. Nothing is written into the workspace,
so a handoff never appears in `git status`, and publishing needs no write scope.
It returns the handoff id and, when it fits, a `copy_prompt` that carries the
full text of all three documents; `list_handoffs` with the handoff id and
`document: "PROMPT.md"` serves the same prompt. The owning Node also saves a read-only copy of the three documents
in its own state, and both prompts name that folder: an agent whose context was
compacted re-reads its plan there. A run may read its handoff's folder but not
write it, and each run start restores a changed copy from Bridge state. The
same `request_id` and content replay the original publication; changed content
needs a new ID.

The optional `context_hashes` map checks named files only; it does not enumerate
the project or save old source. Handoffs that 0.3 wrote into the workspace's
`.workspace-handoff/` folder are still listed but can no longer be read or run;
save them again.

For visual context, upload a raster image with `upload_image`; the Node keeps
it by hash in its own state, never in the workspace. Name the returned
`sha256` with a `description` and no `path` in `save_handoff.attachments`, and
the plan folder gets a read-only copy at `attachments/<sha256>.<ext>`. An
existing allowed workspace image is referenced by its `path` and the source
hash from `read_file` instead, and stays where it is; stop an implementer
before replacing a dispatched workspace image. Up to 20 unique references are
validated and checked at publication, and recorded in `CONTEXT.md` with their
formats, sizes and dimensions. Context and attachments share a 64 MiB total
read budget. Uploading an image alone does not publish a job or start an
agent.

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

When an exact same-Node workspace route is enabled, `start_run` requires a
prepared handoff in the same workspace — or a bounded direct `instruction`,
which the Bridge publishes as a minimal auditable prepared handoff — and accepts
no free-form prompt path. A
run is bound to one agent conversation and stores its exact model
selector (the configured default when `model` is omitted, the runtime's native
default when no Bridge policy exists, or an explicitly requested live catalog
selector per the project-lead skill model-choice rule; MCP cannot
change policy), lifecycle state, timestamps, and notification status. A run may be
`starting`, `active`, or `terminal`; `waiting_interaction` is an active state, not
an outcome. Terminal outcomes include `succeeded`, `failed`, `cancelled`,
`interrupted`, and `orphaned`.

`answer_interaction` resolves only a live request with its exact
adapter-provided choice or validated form answers, once, recording who decided
and why. ChatGPT is the normal path. As a last resort the signed-in Manager can answer
(recorded as `manager`), steer or queue, start a run and cancel one. A corrective iteration creates
a new Bridge run and can reuse a terminal conversation only after live ownership
of the stored native conversation (same adapter, same workspace, idle) and
compatible security are proven. See `MCP_TOOLS.md` and
`RUNTIME_PROTOCOL.md`.
