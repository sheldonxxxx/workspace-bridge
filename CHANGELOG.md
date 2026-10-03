# Changelog

## 0.2.0

First release from the public repository.

- Claude Code runtime: a `claude` adapter type driven through the Claude Agent
  SDK (`uv tool install 'workspace-bridge[claude]'`, executable
  `workspace-bridge-claude-adapter`, `workspace-bridge adapter init --runtime
  claude`) with Bridge-managed `edits`/`shell`/`web`/`extensions` security
  profiles, approvals through Bridge interactions, run-scoped usage, and
  Manager support. Claude Code loads its user, project and local settings,
  `CLAUDE.md`, skills, sub-agents, plugins and MCP servers as the CLI does
  (`--claude-setting-sources` narrows the layers); every tool call, including
  sub-agent and MCP calls, still passes the profile gate, and edits never
  reach `.claude` or `.mcp.json`. Account usage limits report the five-hour and
  seven-day windows from the last run. The Node now treats `.claude` and
  `.mcp.json` as protected names for Bridge reads and writes.
  Existing Bridge and Node databases are upgraded in place to accept the new
  runtime type. Claude Code is not part of the release manifest or verified
  bundle.
- Public README and package listings: one tagline ("Plan in ChatGPT. Build
  with your local agents."), a current Manager screenshot, PyPI and npm
  project links, keywords, and classifiers.
- Security policy with private vulnerability reporting, plus bug and feature
  issue templates.
- Documentation now matches shipped behavior: the published GHCR image,
  in-place v4 state upgrades, and Claude Code alongside Pi and Codex.
- Version `0.2.0` across the Python product and package, Bridge and Node
  components, Codex and Claude Code components, Pi package and adapter,
  Manager package, Docker image tag, and served release metadata.

## 0.1.2

- Terminal-style Manager redesign with a command palette.
- Password-based admin account replaces the admin token for Manager sign-in.
- Codex usage-limits quota reporting and run-scoped usage deltas.
- Release pipeline fixes: validated multiarch image published to GHCR, macOS
  runtime and wheel smoke repairs, and Docker tunnel template inclusion.
- Version `0.1.2` across the Python product and package, Bridge and Node
  components, Codex component, Pi package and adapter, Manager package, Docker
  image tag, and served release metadata.

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
