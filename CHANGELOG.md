# v0.7.0 — Docker Compose deployment

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
