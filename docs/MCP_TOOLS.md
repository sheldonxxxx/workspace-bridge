# MCP tool reference

One endpoint: `/mcp`, served by the official MCP Python SDK in stateless JSON
mode. It answers the 2025 protocol versions and 2026-07-28; the
`MCP-Protocol-Version` header selects the era. Header: `X-Bridge-Token`. This
header belongs in the local tunnel configuration/environment, not tool
arguments. A request without an `id` is a notification and is never executed;
`GET /mcp` answers 405.

24 tools are advertised:

| Group | Tools |
|---|---|
| Discovery | `list_workspaces`, `workspace_info` |
| Reading | `list_dir`, `glob`, `grep`, `read_file` |
| Editing | `apply_patch`, `undo_edit`, `upload_image` |
| Git and checks | `git_status`, `git_diff`, `run_checks` |
| Handoffs | `save_handoff`, `list_handoffs` |
| Agent runs | `list_targets`, `start_run`, `list_runs`, `read_run`, `run_log`, `send_run_input`, `cancel_run`, `answer_interaction`, `apply_run`, `discard_run` |

All arguments are strictly typed and unknown fields rejected. The read tools
are marked read-only and idempotent. `apply_patch`, `undo_edit`, `start_run`,
`send_run_input`, `answer_interaction`, `apply_run` and `discard_run` are
marked destructive. The agent run tools that reach a runtime are marked
open-world; agent output is untrusted evidence. Annotations are hints, not
enforcement.

Local diagnostics are not an MCP tool. The authenticated local manager exposes
`GET /api/diagnostics` and the Doctor CLI uses the same server-side report. A
local MCP gateway being enabled does not establish runtime readiness or remote
ChatGPT/tunnel connectivity; the latter remains unobserved by Bridge.

`workspace_id` is **required on every project tool**, including all handoff tools. Only `list_workspaces` is unscoped. Copy the exact opaque `ws_...` value returned by discovery. Workspace names are display labels, not unique selectors. Every project result includes `workspace_id` for attribution. `workspace_info` also reports the authoritative Node ID/name and node-local root. All file, Git, handoff, and runtime evidence resolves through that Node; Bridge never falls back to a local checkout when it is unavailable. Paths are relative POSIX paths inside that project; use `""` to list/search the root. Absolute paths, `..`, `.` segments and backslashes are rejected.

## MCP run events

MCP 2.0 `server/discover` advertises `capabilities.events: {}`. The same
authenticated endpoint implements `events/list`, `events/subscribe` and
`events/unsubscribe` with verified, signed webhook delivery for ChatGPT.
Events share Bridge authentication and enabled-workspace policy, and add no
tools. See [Run events](EVENTS.md) for filters, bounds, refresh, delivery and
the ChatGPT test workflow. No polling method is exposed.

## Project-lead skill (MCP skills extension)

The project-lead skill is not a tool. The server declares
`capabilities.extensions["io.modelcontextprotocol/skills"]` alongside `capabilities.resources` and serves the
bounded static subset of the draft Skills extension:

- `skills/list` returns one entry: `uri` (`skill://workspace-bridge/project-lead/SKILL.md`),
  parsed `frontmatter` (`name`, `description`, `version`), and `resources` with a
  `sha256:` digest of the UTF-8 content. No pagination.
- `skills/get` with that `uri` returns the entry under `skill` (SEP-2640) and also
  as top-level fields (the shape OpenAI's import docs describe).
- `resources/read` with that `uri` returns the complete Markdown as `text/markdown`.

Any other URI is rejected; no path, workspace ID or repository read is involved.
Bridge authentication, pause and token revocation apply. Clients importing the
MCP-served skill take a submission-time snapshot (for that ChatGPT import path,
run **Scan Tools** again after skill changes and submit a new plugin version).
For an existing private/workspace app connected through a Tunnel, the
[plugin skill packaging guide](PLUGIN_SKILL_PACKAGING.md) builds the snapshot
directly from canonical source using the registered app ID and local package
state; byte-identical skills produce no new ZIP or version bump. The MCP Skills
extension remains available. `list_workspaces` and
`workspace_info` return a small `project_lead_skill` pointer (name, version,
URI), not a repeated copy. Canonical content:
`workspace_bridge/skills/project-lead/SKILL.md`, packaged in source
distributions and wheels. The skill is advisory and cannot expand permissions
or ensure model compliance. It is not part of the release/build ID.

## list_workspaces

```text
list_workspaces(offset=0, limit=20)
```

Maximum `limit`: 40. Returns `workspaces` with `workspace_id`, `name`, `read_scope`, `write_scope`, `agent_execution` and `source_access`, plus `total`, `next_offset`, shared endpoint/access notes. Disabled mappings and their host paths are not disclosed. Follow `next_offset`; discovery does not set an active workspace. An authenticated gateway can be enabled with zero enabled projects, in which case discovery returns an empty list.

## workspace_info

```text
workspace_info(workspace_id)
```

Returns the selected Node ID/name, node-local root, text limits, `image_reading` and `image_writing` capabilities/limits, exclusions, current `write_scope` (`none` for read-only or `workspace` for edit), an informational `agent_execution` marker (`per-route`: execution is admitted per exact enabled workspace route, and any legacy workspace-wide switch is inert) and workflow contract. Policy changes are local-admin-only; reads remain general-purpose. Host paths are intentionally available here and in manual copy prompts, after explicit selection.

## list_dir

```text
list_dir(workspace_id, path="", depth=1, offset=0, limit=60,
         expected_listing_sha256=null)
```

Lists files **and directories**, including empty directories. `depth=1` means immediate children; maximum 4. Directories at the requested depth are shown without descending further. Maximum `limit`: 100. Deterministic path ordering. Each entry has its type and relative path (file entries also have size).

Result: `entries`, `total`, `next_offset`, `listing_sha256`, `skipped_count`, `listing_is_live`. Carry `listing_sha256` into subsequent calls as `expected_listing_sha256` to reject an observed listing change. Without it, pages are independent live observations. This hash covers returned listing metadata/policy, not every file's contents. Output bounds can shorten a page below `limit`; always use the returned continuation.

## read_file

```text
read_file(workspace_id, path, offset=1, limit=200, expected_sha256=null,
          representation="auto", max_image_dimension=null)
```

`offset` is a **1-based line number**, not a byte offset. Maximum `limit`: 400. For text, UTF-8 only, no NUL/binary, maximum source file 512 KiB. Text behavior is unchanged. Result includes numbered `lines`, raw-content `sha256`, `total_lines`, `next_offset`, `redacted` and a trust label. Carry `sha256` into subsequent pages as `expected_sha256`. Long lines and total output are bounded; some pathological encodings/long-line pages can return an output-limit error rather than an oversized response.

Use small ranges around relevant symbols rather than pulling entire repositories into context. Offsets and line numbers remain stable across multiline secret redaction, though redaction can alter columns/content. Hashes refer to the underlying bytes, not redacted display text.

For PNG/JPEG/WebP/GIF/BMP/TIFF, `auto` instead returns **two native content blocks**:
text metadata plus `type:"image"` with base64 `data` and `mimeType`. No base64 is
inserted into the text block. `representation="image"` explicitly requests a
supported raster preview; `text` suppresses image dispatch and applies the original UTF-8/NUL checks.
Filename suffixes are hints for useful failure handling; bytes are decoded and validated.
For images omit `offset` and `limit` (non-default line pagination is rejected).
`max_image_dimension` is image-only, integer 256–4096; null means 2048. It is a
ceiling, not an exact requested size: the 2 MiB preview cap can shrink it further.
Source input is separately bounded to 20 MiB/40MP; ordinary text keeps its smaller limit.

Image metadata includes `workspace_id`, `path`, **source** `sha256`, source format,
size/dimensions, oriented and preview dimensions, transformation flags, preview
MIME/size/hash, `next_offset:null`, first-frame policy and privacy/trust notices.
Use the source `sha256` for stale-read or optional handoff `context_hashes`, NOT
`preview_sha256`. Referenced context is bounded to 64 MiB total per publication.
Image input hashes do not prove visual equivalence. Previews are
not color-managed and may be downscaled or JPEG re-encoded; no OCR or visual secret
filter runs. Metadata removal does not remove secrets visible in pixels.

Animated and multi-page raster formats return frame/page 0 only. No arbitrary
binary reading, SVG rendering or PDF/Office/RAW/HEIC/AVIF handling is supported.
The exact client/tunnel route must be tested for model-visible image content; a
successful HTTP response alone is not that proof. See `IMAGE_SUPPORT.md`.

## glob

```text
glob(workspace_id, pattern, path="", offset=0, limit=60,
     expected_listing_sha256=null)
```

Finds **files** by pattern, relative to `path`, returning workspace-relative result paths. Same page shape/hash behavior as `list_dir`. Maximum `limit`: 100.

Supported: `*`, `?`, character classes `[]` and whole-segment `**`.

```text
**/*.py          # Python files at any level, INCLUDING the selected root
*.py             # Only Python files immediately inside the selected path
src/**/test_*.py  # Test files in src or any descendant
```

No brace expansion (`*.{py,js}`), shell expansion, extglob, following links or arbitrary filesystem resolution. Use separate queries for multiple extensions. Matching is case-sensitive. Patterns filter a safe inventory and cannot override exclusions.

## grep

```text
grep(workspace_id, pattern, path="", include="**/*",
     fixed_strings=false, case_sensitive=false,
     context_lines=0, limit=40, cursor=null, run_id=null)
```

Line-oriented regex by default; `fixed_strings=true` is literal matching. `include` uses the same filename glob rules relative to `path`. `context_lines`: 0–5. Maximum `limit`: 100 matching lines. Case-insensitive by default. Uses the Python `regex` engine, not a shell command or ripgrep/PCRE compatibility layer. Regex syntax is not claimed to match other agents exactly.

Matches include path, 1-based line/column, a bounded text snippet, `text_truncated`, file SHA-256, redaction flag and optional context. A matching line is returned once even when it contains several occurrences. Columns refer to searched/redacted text; a truncated snippet is not a replacement for `read_file`.

Always follow `next_cursor` until null, **even when a page has no matches**. Cursors are opaque and HMAC signed. Reuse the same workspace, pattern, path, include, flags and context; the page limit may change. A cursor cannot switch projects or queries. `stale_cursor` requires restarting after an observed inventory/file/policy change; `invalid_cursor` rejects tampered or mismatched continuations.

A page reads/skips at most 100 candidate files, reads at most 8 MiB, and has approximately 5 seconds of matching work in addition to separately bounded enumeration. Each regex line match has a 20 ms timeout. Simplify a pattern after `regex_timeout`; do not retry unbounded patterns blindly. Pattern compilation and OS I/O are not a hardened process sandbox; use trusted local administrators and OS limits for stronger isolation.

`search_complete` means exhaustion of the **policy-filtered UTF-8 search scope**, not all data on disk. `skipped_files`, `skipped_this_page`, `unsafe_entry_count` and truncation flags expose omissions. Count skips across pages. Binary/oversize files, excluded directories and secret-redacted text are not searched as original content. Searches redact before matching, so a query is not a reliable secret-discovery mechanism.

## apply_patch and undo_edit

```text
apply_patch(workspace_id, changes, message)
undo_edit(workspace_id, edit_id)
```

`changes` is a list of up to 200 operations touching at most 50 files and
carrying at most 1 MiB of text:

| `op` | Fields | Effect |
|---|---|---|
| `edit` | `path`, `sha256`, `old`, `new` | Replace one exact, unique, case-sensitive occurrence |
| `create` | `path`, `content` | New UTF-8 file (256 KiB at most); fails if anything exists there |
| `move` | `path`, `to`, `sha256` | Move a file; never overwrites |
| `delete` | `path`, `sha256` | Delete a file |

Each file appears once, except that several `edit` operations may change one
file: each gives that file's original hash and they apply in order. Every
existing file needs its current SHA-256. The Node checks the whole patch
first (write scope, exclusions, hashes, matches, the text and secret checks),
journals the earlier content, stages every new file, and then switches them.
A failed switch puts back the files already switched, so all changes apply or
none do. `message` (1–500 characters) is recorded with the entry.

Paths are workspace-relative. The server applies that mapping's current
`write_scope`: `none` (read-only, the default) denies all changes; `workspace`
(edit) permits allowed files. No tool argument or MCP tool can expand
permission. A change that adds secret-like text the file did not already hold
is refused (`secret_content`); matches already in the file may stay, so such
files remain editable. Binary content, unsafe paths and exclusions remain
denied in all modes. A single-file change is an `apply_patch` with one
change; there is no separate whole-file write tool. See
[file-access semantics and limits](FILE_ACCESS.md).

The result holds `edit_id`, each file's `previous_sha256` and new `sha256`
(`null` when absent), and `created_folders`. `undo_edit` restores every file
of that edit while each still holds the hash the edit wrote; otherwise it
changes nothing and returns `undo_conflict` naming the files changed since.
It also removes folders the edit created while they are empty. An undo is
its own journal entry, so it can be undone too; a second undo of the same
edit returns `already_undone`. Both tools follow the current write scope, are
marked destructive, and execute nothing.

The journal lives on the Node and keeps entries for 30 days or until a
workspace's entries pass 500 MiB, oldest first; an expired `edit_id` returns
`not_found`. Each entry records the actor (`chatgpt`) and message. The
Manager lists entries, without file content, at
`GET /api/workspaces/{workspace}/edits`.

## Image upload

```text
upload_image(workspace_id, data_base64, mime_type)
```

Uploads original PNG/JPEG/WebP/GIF/BMP/TIFF bytes for a handoff. The
authoritative Node keeps them by hash in its own state, never in the
workspace, so the call needs no write scope and read-only workspaces take
uploads too. Each workspace keeps up to 512 MiB of uploads. Uploading the same
bytes again returns the same `sha256`. Uploading does not create a job or start
an agent.

`data_base64` is strict base64 without whitespace or a data-URL prefix. MIME type
must be `image/png`, `image/jpeg`, `image/webp`, `image/gif`, `image/bmp` or
`image/tiff` and match the decoded format. Images are validated with the
bounded first-frame decoder. Limits: 20 MiB original bytes, 40MP, 27,962,028
base64 characters. Only this tool gets the larger 28,027,564-byte JSON request
budget; other MCP calls keep 1 MiB.

The result has the original `sha256`, `bytes`, format, MIME and source
dimensions. Name that `sha256` in `save_handoff.attachments` without a `path`.
Original bytes and embedded metadata are preserved. Pixels are not secret-scanned
or redacted. `read_file` still returns a transformed, metadata-stripped preview.
There is no URL fetch or generic binary writer.

## Handoff tools

```text
save_handoff(workspace_id, request_id, title, goal, plan, acceptance,
             constraints=..., context=..., context_hashes={}, attachments=[])
list_handoffs(workspace_id, offset=0, limit=20, job_id=null, document=null,
              start_line=1, max_lines=100)
```

`save_handoff` stores TASK.md, CONTEXT.md and ACCEPTANCE.md in Bridge state.
Nothing is written into the workspace, so a handoff never appears in
`git status`, and it needs no write scope. A run's prompt carries the full text
of all three documents. The owning Node also saves a read-only copy in its own
state and returns its folder as `plan_folder`; both prompts name it so an agent
that lost its context can re-read the plan. A run may read that folder and
nothing else in Node state, and each run start restores any changed copy. A
Node that cannot save the copy leaves `plan_folder` out at publication, and a
run then fails with `plan_unavailable` unless the Node predates plan files.
The result has `stored: "bridge"` with each document's hash and size and,
when it fits in 12,000 characters, a `copy_prompt` for pasting into a local
agent; otherwise `copy_prompt_chars` and a note to read `PROMPT.md`. Optional
context hashes check specifically named files at publication; no source
snapshot is captured at publication ([run checkpoints](#run-checkpoints)
belong to runs). The local agent replies in its conversation and the user
pastes that reply into ChatGPT. Review uses the same normal browsing tools.
Findings stay in chat.

Optional `attachments` contains up to 20 unique image references, each one of:

- an upload: `{"sha256":"<upload_image sha256>", "description":"Layout to match"}`.
  The plan folder gets a read-only copy at `attachments/<sha256>.<ext>`, and
  CONTEXT.md names that file.
- a workspace image: `{"path":"design/reference.png", "description":"Layout to
  match", "sha256":"<original-file hash>"}`, with the source hash from
  `read_file`. It is referenced in place, not copied.

Each reference is validated and its hash checked at publication; descriptions,
hashes, formats, sizes and dimensions are recorded in `CONTEXT.md`. Attachment
hashes and any matching `context_hashes` must agree. Referenced context and
attachments share a 64 MiB total read budget. A missing upload, a changed or
unavailable workspace file, a non-image or a duplicate reference fails before
publication.

`list_handoffs` without `job_id` lists the workspace's handoffs. With `job_id`
it returns that handoff's summary (the same shape `save_handoff` returns).
With `job_id` and `document` it pages through `TASK.md`, `CONTEXT.md`,
`ACCEPTANCE.md` or `PROMPT.md` (the copy-paste prompt) by 1-based
`start_line`/`max_lines` (up to 200 lines); follow `next_line`. `state` records
publication state, not agent completion. Handoffs that 0.3 wrote into the
workspace's `.workspace-handoff/` folder are still listed with
`stored: "workspace_folder"` and a note, but can no longer be read or run:
save them again.

## Read-only Git evidence

```text
git_status(workspace_id, offset=0, limit=50, expected_status_sha256=null)
git_diff(workspace_id, mode=head|worktree|staged|run, path=null, offset=0,
         max_bytes=3000, expected_status_sha256=null, run_id=null)
```

These tools inspect only a repository with a real `.git` directory directly under
the mapped workspace. A non-Git workspace returns `available=false`; `.git` files,
linked worktrees, bare repositories, symlinked or unsafe metadata fail closed.
`git_status` returns the branch, HEAD, local upstream counts when available,
policy-allowed changed paths, conflict stages, pagination, and a filtered
`status_sha256`. Excluded or sensitive paths contribute only to `hidden_count`.
The opaque hash is process-local; fetch fresh status after a Bridge restart.

`git_diff` compares HEAD to the worktree (`head`), index to worktree (`worktree`),
or HEAD to index (`staged`). It accepts only a fixed mode and an optional exact
changed path; untracked paths have status entries but no patch. It has no arbitrary
Git command, ref, or option surface. External diff/textconv and pagers are disabled,
filter drivers defined in the repository config are neutralized (an unparseable
filter section fails closed), submodules are not traversed, patch secrets are redacted, and output is byte-paginated.
Pass the latest status hash to either tool to reject stale review evidence. A status
or diff is live observation only: it creates no snapshot and establishes no
authorship. Dirty-tree changes may predate the current run. Continue with targeted
current-source reads and runtime activity evidence.

### Run checkpoints

Every Runtime Protocol run gets a checkpoint before its native start and after
it becomes terminal. `git_diff` with `mode=run` and `run_id` returns the
redacted, byte-paginated patch between the two, so edits that were already in
the worktree before the run are excluded. It works for non-Git workspaces too.
`run_id` is required with `mode=run` and not accepted otherwise;
`expected_status_sha256` is not used because checkpoints never change.

The diff covers the run's time window, not authorship: edits by people and by
other runs listed in `checkpoint.overlapping_runs` are included. A run that is
still active, whose capture failed, or whose checkpoints were removed by
retention fails with `checkpoint_pending`, `checkpoint_unavailable` or
`checkpoint_expired`. A path excluded after capture is no longer shown.

The result also has `chatgpt_edits`: the `apply_patch` and `undo_edit` journal
entries made between the run's pre checkpoint and its end, each with `edit_id`,
`kind`, `state`, `created`, `message` and `paths`, plus the combined `paths`.
Where those paths also appear in the run diff, ChatGPT and the agent both
changed them. `status` is `unavailable` when the Node's journal cannot be read,
and `not_applicable` for an [isolated run](#isolated-runs), whose checkpoints
cover only its worktree.

The owning Node captures checkpoints into its own private Git store, reading
files with the same exclusions, built-in deny list and 512 KiB per-file limit as
`read_file`. A Git workspace's `.gitignore` rules also apply, tracked files are
captured first, and checkpoints never write the workspace or its `.git`. A larger
file, or a file beyond the per-checkpoint cap (10,000 files or 256 MiB), is
recorded as a placeholder and reported as `not_captured`, so it reads as
changed rather than deleted. Symlinks, hard-linked and special files are skipped.

The pre capture has a 15 second budget and never blocks the run: a failure only
marks the checkpoint unavailable. The post capture is durable across Bridge
restarts; one taken more than a minute after the run ended is marked `late`.
Checkpoints are kept for 14 days and up to 1 GiB per workspace, oldest first;
active runs and the newest finished run are never removed. They never restore
files.

## run_checks

```text
run_checks(workspace_id, names=null, check_id=null, wait_seconds=30, run_id=null)
```

Runs checks the local administrator named for this workspace, such as `test`
or `lint`, on the owning Node. ChatGPT chooses names only. Commands, folders
and timeouts come from the workspace's `checks.toml`, which the Node keeps in
its own state outside the repository; no MCP tool can read its path or change
it. Omit `names` to list the configured checks and the after-run list. Pass up
to 20 `names` to start them; the call waits up to `wait_seconds` (0 to 50) and
returns `state: running` with a `check_id` if they are still going; call again
with that `check_id` to read the result. Starting checks needs the edit write
scope; one batch runs per workspace at a time (`checks_busy`).

Each result has `name`, `command`, `cwd`, `status` (`passed`, `failed`,
`timed_out`, `error` or `interrupted`), `exit_code`, `duration_ms`, a
redacted `log_tail` of the last 6,000 characters and `output_bytes`. The batch
reports `passed` and `failed` counts. Output is the checks' own text and is
untrusted. Commands run without a shell, with the user's login `PATH`, and
with Bridge variables removed from the environment. A check that passes its
timeout is stopped and reported as `timed_out`.

`checks.toml` holds one `[checks]` table. Each entry is a command string, or a
table with `command` (string or list), an optional workspace-relative `cwd`
and `timeout_seconds` (default 600, at most 3600):

```toml
[checks]
test = "uv run pytest -q"
lint = { command = ["npm", "run", "lint"], cwd = "web", timeout_seconds = 300 }
```

A workspace may also name checks to run after every run that completes or
fails. They start after the run's post checkpoint, so their output files are
not counted as the agent's changes; `read_run` shows them under
`checks`, and the run's `run.finished` event and Discord notice wait for them
and report the counts. After-run checks follow route enablement, not the write
scope. The Manager edits both at `GET`/`PUT /api/workspaces/{workspace}/checks`
(`text` for `checks.toml`, `after_run` for the list).

## Agent run tools

```text
list_targets(workspace_id, adapter_id=null, query="", limit=25)
start_run(workspace_id, adapter_id, request_id, job_id=null, instruction=null,
          model=null, parent_run_id=null, continue_from_run_id=null,
          isolation=null, images=[])
list_runs(workspace_id, adapter_id=null, offset=0, limit=20)
read_run(workspace_id, run_id)
run_log(workspace_id, run_id, offset=0, limit=50, activity_id=null)
send_run_input(workspace_id, run_id, instruction, request_id, mode)
cancel_run(workspace_id, run_id)
answer_interaction(workspace_id, run_id, interaction_id, choice_id=null,
                   answers=null, decided_by, reason)
apply_run(workspace_id, run_id)
discard_run(workspace_id, run_id)
```

`send_run_input` with `mode: "steer"` sends a correction to active work.
Codex and Pi use native steering; Claude Code interrupts the current response
and sends the new correction in the same session, ahead of queued follow-ups.
If the response finishes before interruption, the correction becomes the next
response. `mode: "queue"` adds a follow-up after the current response succeeds.
Follow-ups run in FIFO order without Bridge polling, within the same logical
run, conversation, model settings, and security binding. They do not create
another handoff or change the run's authority. The exact workspace route
must still be enabled, and the captured Node/adapter revisions must match.
Adapters must advertise `inputControls:1` alongside the requested operation;
an older adapter without safe input receipts is rejected before receiving text.

`instruction` is text of at most 8000 characters and `request_id` is 1–100
letters, digits, dots, underscores, or hyphens, starting with a letter or
digit. IDs are unique per run across both modes. Retry with exactly the
same instruction and mode to check the existing receipt; changed input under
the same ID fails closed. A run retains at most 50 message receipts and allows
at most 20 pending inputs. `input_message` is the accepted request's receipt;
`input_messages` on run reads contains text-free receipt history with
`requestId`, `mode`, and `status` (`queued`, `delivered`, `discarded`, or
`unknown`). Delivered means native submission, not successful completion.
Unknown delivery is never retried automatically. Failure, cancellation, and
adapter restart discard pending inputs; prompt text is never persisted as a
replay queue. The Manager shows the receipts but cannot send input.

Call `list_targets` first. Without `adapter_id` it returns only configured
AdapterInstances owned by the selected workspace's authoritative Node,
including sanitized `adapter_id`, display name, `node_id`/`node_name`,
`runtime_type`, route and adapter enablement, default flag, default model,
effective security summary, supported `isolation` modes,
canonical readiness, and a bounded `descriptor` summary (`status`, safe `code`
on failure, `features` plus instance/adapter/native versions on success).
Descriptor diagnostics are observability only and never change `available`/
`readiness`. It does not return endpoints, tokens, URLs, paths, profiles,
prompts, or raw runtime payloads. `runtime_type`
(`pi`, `codex`, `claude` or `opencode`) describes protocol behavior; only
`adapter_id` selects a destination. Do not infer an AdapterInstance from a
runtime type. Use a ready default when one is reported; if there is no default
and exactly one ready target exists it may be used, otherwise ask which
destination to use. Never fail over from an unavailable default.

`start_run` accepts exactly one of a prepared handoff (`job_id`) or a
bounded direct `instruction` (at most 8000 characters) in the selected workspace. It does not accept a
free-form prompt path. A direct instruction is published as a minimal
auditable prepared handoff in Bridge state with a
deterministic handoff request ID derived from the run idempotency domain
(`adapter_id` + `request_id`); exact retries reuse the same audit handoff,
and a changed instruction under the same adapter + request_id is rejected by
the handoff content conflict before any duplicate row or artifact is created.
That
AdapterInstance and exact WorkspaceRoute must be enabled; a configured model
policy is enforced, and with no Bridge policy the runtime chooses its default
unless a live catalog model is requested.
An exact retry with the same request ID returns the existing run; reusing that
ID for a different request or adapter is rejected. A continuation creates a new
Bridge run in a terminal run's conversation for a NEW prepared handoff in the
same workspace (or a bounded direct instruction, which is first published as
one); the new handoff's full text is sent.
Ownership is proven live against the stored native conversation on the current
same-Node adapter: it must exist, belong to the workspace, and be idle.
Historical outcome (failed/cancelled/interrupted), model changes, and
Node/adapter revision drift do not block a proven continuation; missing,
foreign, or busy conversations still fail closed before any prompt. The
handoff ID is not required to match the source run. Endpoint/token edits that
move the runtime fail closed at the ownership proof instead of silently
starting a fresh conversation; renaming an adapter
does not change its connection revision. Every run carries the route's current
profile rules, so a profile edit applies to a continuation with no extra step.
Switching the route between a profile and native security fails the
continuation with `security_source_changed` before any prompt.

`read_run` includes `phase`, `active_state`, `outcome`, bounded result,
sanitized error, notification delivery summary, pending `interactions`, and
`token_usage` with run-scoped native counters (`input_tokens`,
`cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, `total_tokens`) when the native runtime reported
them, otherwise null. The same canonical run representation is returned by
admin list/read APIs and MCP run tools; there is no separate usage endpoint.
It also includes the immutable `node_id`/revision, adapter revision, and
`effective_security` snapshot used immediately before that run started; this
snapshot remains historical if workspace settings later change. When a
profile-bound run got read access to its handoff's `plan_folder`, the snapshot
names that folder and keeps the saved profile's `profile_revision` beside the
run's own `revision`.
Runs started with checkpoint support include `checkpoint`: overall `status`
(`pending`, `complete`, `partial`, `unavailable` or `expired`), per-phase status,
capture time and error code, `late`, `overlapping_runs`, and after the post
capture `changed_paths` with up to 100 `changes` (`path`, `status`, optional
`not_captured`). See [Run checkpoints](#run-checkpoints).
A run in a workspace with after-run checks includes `checks`: `state`
(`waiting`, `running`, `complete` or `unavailable`), the check `names`, and once
complete the `passed` and `failed` counts with each check's status, exit code
and duration. See [run_checks](#run_checks).
`read_run` lists every interaction with its exact live request and supported
choices. ChatGPT is the only place an interaction is answered: the Manager and
Discord only show that a run is waiting, and nothing answers on a timeout.
`list_runs` lists runs waiting on an answer first. Submit the adapter's
opaque choice ID unchanged as `choice_id`, or a form's validated `answers`
keyed by question ID, with `answer_interaction`, with `decided_by`
(`chatgpt` when ChatGPT decided under the project-lead skill's approval rules,
`owner` when it relays the user's own answer) and a `reason` of up to 500
characters. The Bridge cannot verify `owner`; it records what ChatGPT reports.
An interaction is answered once: the Bridge claims it with compare-and-set,
then rechecks that the native request is still live and forwards it, so a
retry or a second answer fails with `already_answered`. If the plugin refuses
the answer, the claim is released, or the interaction becomes stale when the
native request is gone. Form interactions accept only validated answers. A
stale interaction fails closed. Answered interactions carry a `decision`
(`decided_by`, `reason`, `decided`, `choice_id`); the Manager's approval log
at `GET /api/approvals?decided_by=chatgpt|owner` lists them newest first.
Activities are bounded adapter evidence and do not independently verify the
agent's final claim.

`read_run` also returns `notifications`: up to 20
recent semantic event summaries, per-channel delivery status/attempts and an
overall `sent`, `partial`, `failed`, `pending`, `disabled` or `none` state.
Run listings stay compact. `/api/status` reports configured channel ids and
readiness without endpoints or credentials. Notification status is delivery
evidence only and never establishes run success.

`list_targets` with `adapter_id` reads that AdapterInstance's model list and returns exact canonical selectors annotated with that adapter's
policy status. A configured Bridge policy annotates each model as
admin-enabled (`enabled`) or not, plus the `policy_default`; with no Bridge
policy the response is marked unrestricted (`policy_restricted: false`), every
listed model is usable, and the runtime chooses its own default. A `query`
filters or ranks candidates; it never selects one.
`start_run` is handoff-bound on the explicitly selected adapter: it
requires a prepared handoff in the same workspace, or a bounded direct
instruction, exactly one of the two; a direct instruction is published as a
minimal auditable prepared handoff in Bridge state with a
deterministic handoff request ID derived from the `adapter_id` + `request_id`
idempotency domain, so exact retries
reuse the same audit handoff and never replay an accepted prompt, a changed
instruction under the same adapter + request_id fails the handoff content
conflict before any duplicate is created, different adapters may reuse one
request_id without colliding, and no
arbitrary prompt channel or shell capability is added. A configured adapter
policy is enforced exactly as before (omitting `model` uses its configured
default; an explicit `model` is allowed only when its exact selector is enabled
and currently available, `model_not_enabled`/`model_unavailable` otherwise;
MCP cannot change the policy). With no Bridge model policy the models are
unrestricted by Bridge governance: omitting `model` sends no selector so the
runtime chooses its own default, and an explicit `model` only has to be in the
live catalog. The local administrator may save an optional thinking or reasoning
default per model; runs use that effort only when it is configured for the
selected model and otherwise retain the runtime's own default. MCP cannot
override it. It is idempotent per
`request_id` within the exact `(workspace_id, adapter_id)` and never replays a
run from a different AdapterInstance. It accepts no free-form prompt path and
returns `run_id`, `conversation_id`, `adapter_id`,
adapter name, runtime type, and exact model (null for a native default). For a
small corrective follow-up
in the same workspace with unchanged adapter ID and compatible security,
`continue_from_run_id` reuses a terminal run's conversation as a new
Bridge run for a NEW prepared handoff ID (the new handoff's prompt is sent).
Continuation implies `parent_run_id`: when it is omitted the parent is
canonicalized to the continuation source, and a different explicit parent is
rejected with `continuation_parent_mismatch`. The source run may be failed,
cancelled, or interrupted (only a nonterminal run cannot continue), the model
may change, and stored Node/adapter revision drift alone does not invalidate an
owned idle conversation: Bridge proves current runtime ownership by reading the
stored native conversation on the current same-Node adapter and requires it to
exist, belong to this workspace, and be idle before sending any prompt; after
that proof it refreshes only the conversation's current Node/adapter metadata
and never rewrites historical run evidence. Missing, foreign, mismatched, or
busy conversations fail closed before any prompt (`conversation_busy`). Each
run carries the route's current profile rules, and prior runs keep their
immutable `effective_security`. Switching the route between a profile and
native security fails explicit continuation with `security_source_changed`
instead of silently starting a fresh conversation; an ordinary fresh run
without `continue_from_run_id` remains allowed. Each run owns only
the activities and results recorded for that iteration. The caller must select
the exact adapter explicitly; Bridge does not silently switch destinations after
failure.

`list_runs` lists this workspace's runs across AdapterInstances (waiting on
an answer first, then newest first) with phase, active state, outcome, Node and adapter ID/name, runtime type,
model, security-used snapshot, run-scoped `token_usage` (null when unreported),
conversation id, and timestamps; pass `adapter_id` to filter to one destination.
`cancel_run` requests
cancellation of only the bound conversation and records the result from the
adapter snapshot. `run_log` pages through the run's recorded activities
(commands, file changes, tool calls, searches and subagents) as bounded
summaries; with `activity_id` it returns one activity with sanitized
input/result previews (no output bodies, reasoning, or secrets). These are
adapter snapshots rather than an independent completeness audit.

The run's `node_id` plus `adapter_id` identify the owning destination;
`runtime_type` is descriptive metadata. If a ChatGPT connector or cached tool
registration still advertises older tool names or `runtime=` arguments,
refresh or reconnect that connector before dispatch. Do not add compatibility aliases for a stale
registration.

A run's final result is what the agent reported. It is unverified evidence: audit
current source with the general tools. This server does not independently run tests
and exposes no arbitrary command tool.

### Isolated runs

`start_run` with `isolation: "worktree"` runs the agent in its own Git
worktree instead of the workspace, so the user's files, index and branch stay
untouched until ChatGPT applies the result. The owning Node:

- builds a snapshot commit of the workspace's current files in a temporary
  index (committed, uncommitted and untracked files; ignored files, admin
  exclusions and built-in denied paths such as `.env` are left out), so the
  workspace's index and `HEAD` never change;
- creates branch `wb/<worktree_id>` at that commit and a worktree for it in the
  Node's private state, with Git hooks disabled;
- starts the agent's conversation there, and the prompt names the worktree, its
  branch, and that ignored files such as installed dependencies are absent.

Isolation needs a workspace that is the top level of a plain Git repository
(`isolation_unavailable` otherwise) and a plugin that reports worktree support:
`list_targets` lists the modes each adapter supports in `isolation`. The default, `none` (or
omitting `isolation`), works in the workspace itself.

A continuation keeps its source run's isolation and keeps working in the same
worktree, so every run in one conversation builds on the last. Pass the same
`isolation` value or omit it; a different value fails with
`isolation_mismatch`, and continuing after the worktree was applied, discarded
or removed fails with `worktree_unavailable`. Checkpoints and after-run checks
of an isolated run cover its worktree, so `git_diff` with `mode=run` shows what
the agent changed there. When each run ends, the Node also commits the
worktree's state to its branch, for anyone who wants to read it with Git.

Before applying, review the run where it is. `read_file`, `list_dir`, `glob`
and `grep` take an optional `run_id` that reads that run's worktree
instead of the workspace, with the same exclusions, deny list and redaction;
listing hashes and search cursors from the worktree and the workspace never
mix. `run_checks` with `names` and `run_id` runs the configured checks in the
worktree; it needs the edit write scope like any ad hoc check and fails with
`run_active` while a run is working there. A `run_id` for an ordinary run fails
with `not_isolated`, and one whose worktree was applied, discarded or expired
with `worktree_unavailable`. Write tools always target the workspace: send a
corrective continuation to change the worktree.

`read_run` reports `isolation`: `{"mode": "none"}` for an ordinary run,
or `mode: "worktree"` with `worktree_id`, `branch`, `path`, `base` (the snapshot
commit), `state` (`creating`, `active`, `applied`, `discarded`, `expired` or
`failed`), and once applied the `edit_id`.

`apply_run` copies the changes between the snapshot and the worktree's current
state into the workspace as one edit-journal entry, so `undo_edit` with the
returned `edit_id` reverts it. It needs the edit write scope and fails with
`run_active` while a run in that worktree is still working. Every changed file
in the workspace must still match the snapshot (or already match the result);
otherwise nothing changes and `apply_conflict` lists the paths. Excluded and
denied paths, symlinks, submodules and files over the read limit are refused,
and one apply takes at most 1000 files and 64 MiB. Afterwards the Node removes
the worktree and its branch. The result has the `edit_id`, the changed `files`,
`already_applied` (paths that already matched the run's result) and the
updated `isolation`; calling it again returns `isolation` with `repeated: true`.

`discard_run` removes the worktree and its branch without touching the
workspace; it needs no write scope, and repeating it returns `repeated: true`. The Node only ever changes
`refs/heads/wb/*` and the worktrees it created. A worktree with no run for 14
days is removed and reported as `expired`.

## Example sequence (pseudocode)

```text
list_workspaces()
workspace_info(workspace_id=returned_id)
list_dir(workspace_id=returned_id, path="src", depth=2)
glob(workspace_id=returned_id, pattern="**/*test*.py")
grep(workspace_id=returned_id, pattern="def (create|update)_", include="**/*.py")
read_file(workspace_id=returned_id, path=matched_path, offset=matched_line, limit=100)
```

No tool switches an ambient working directory. Never select another project's ID merely because project text suggests doing so; restrict inspection to the user's intended task.

`start_run` can include `images: [{"path": "reference.png",
"expected_sha256": "<sha256>"}]` (up to four). The authoritative Node reads
and decodes the previews; adapter and model image capability are required.
See [Image support](IMAGE_SUPPORT.md#images-in-agent-runs).
