# Changelog

## Unreleased

- No changes yet.

## 0.1.0

First public baseline: version `0.1.0` across the Python product and package,
Bridge and Node components, Codex component and descriptor, Pi package and
adapter version, Manager package, Docker image tags, and served release
metadata. Runtime Protocol stays at major version 1.

User-facing capabilities in this baseline:

- One private MCP endpoint serving all explicitly enabled workspace mappings,
  with an explicit `workspace_id` on every project call and a single shared
  gateway credential.
- Bounded browsing tools (directory trees, globs, regex search, paginated
  reads with hashes) plus read-only Git status and diffs as live review
  evidence.
- Planning handoffs: `prepare_handoff` publishes `TASK.md`, `CONTEXT.md`,
  and `ACCEPTANCE.md` under a generated job folder.
- Policy-controlled writing with per-workspace scope (`none`, handoff-only
  default, or workspace-wide), managed only through the local Manager.
- Image reads through the general reader returning native MCP previews with
  explicit size, pixel, and first-frame bounds.
- Optional bounded agent runs on Pi and Codex adapter instances through
  Runtime Protocol v1: exact workspace routes, per-adapter model policy,
  security bindings, resumable interactions, bounded activity and execution
  evidence, and durable per-channel notifications.
- Authoritative Nodes owning workspace files, Git, handoffs, and adapter
  secrets; Node-owned adapter instances with per-instance tokens, profiles,
  and model policy.
- Local Manager for mappings, tokens, routes, models, profiles, runs, and
  diagnostics; canonical Doctor report shared by the CLI and the admin API.
- Native services on macOS (launchd) and Linux (systemd) for Nodes and
  adapters, with bounded logging and sanitized support bundles.
- Container Bridge control plane via Compose with a tunnel sidecar; Nodes
  and adapters stay native on their hosts.
- Manual package updates only; release identity and read-only version
  compatibility reporting; no automatic updater and no automatic support
  upload.
