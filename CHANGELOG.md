# Changelog

## Unreleased

- `workspace-bridge plugin test <manifest|id>` runs the plugin conformance
  kit in a temporary folder: protocol checks by default, and with `--live`
  runs, interactions, events, recovery without replay, and continuation.
  It exits non-zero unless the plugin passes.
- Fixed: shell command rules now apply to commands wrapped in a shell, such
  as `zsh -lc 'git push'`. Codex runs every command this way, so its ask and
  deny command rules were not enforced before.
- Fixed: a Codex conversation accepts new runs after a failed turn and after
  a restart, and a run that names no model uses the thread's own model
  instead of the last run's.
- Breaking: agents run only as Node-supervised plugins. The HTTP adapter
  services are gone: the `workspace-bridge adapter` command (init, serve,
  show-token, service, open-session, reclaim-session), the
  `workspace-bridge-claude-adapter`, `-codex-adapter` and
  `-opencode-adapter` executables, adapter tokens and ports, and the Pi
  HTTP server (`workspace-bridge-pi-adapter` now requires `--stdio`). Re-add
  each agent with `workspace-bridge node adapter add <plugin> --name
  <name>`. Existing HTTP adapter rows stay visible so their run history is
  kept, but can only be renamed, disabled or deleted; runs on them fail
  with `adapter_removed`. The Manager API accepts only plugin adapters.
  OpenCode's terminal handoff and reclaim commands are removed.
- Breaking: security profiles move from the agents to the Bridge. A profile
  sets deny, ask or allow for reads and writes inside and outside the
  workspace, the shell, web access and extensions, plus protected paths,
  external roots and shell command rules; the built-ins are `read-only`,
  `reviewed` and `trusted`. Every run carries its route's current rules and
  the plugin echoes what it enforces, so a profile edit reaches the next run
  and continuation with no rebind. Local admins manage profiles at
  `/api/profiles`; saving an edit that loosens a profile, or that a routed
  agent cannot enforce, asks for confirmation, and a profile in use cannot be
  deleted. A route is refused when its agent cannot enforce the profile. The
  per-adapter profile endpoints, security rebind and the `runtime-config`
  source are removed; Codex routes use `native` security instead, or a
  profile its sandbox can enforce (`trusted`). On upgrade, routes on the old
  `read-only` and `workspace-write-reviewed` profiles move to `read-only` and
  `reviewed`, and routes on an agent-owned custom profile are disabled until
  a Bridge profile is chosen. Codex routes moved to a profile Codex cannot
  enforce must be switched to `trusted` or `native`, which the Manager's
  route security dialog now offers.
- Breaking: `workspace-bridge support bundle` is removed. Use
  `workspace-bridge doctor --json` and the service and plugin logs.

- Agent plugins (Agent Protocol v2): a Node can run Claude Code, Codex,
  OpenCode and Pi as supervised child processes speaking JSON-RPC over
  stdio, with no adapter port, token or service. Add one with
  `workspace-bridge node adapter add <plugin> --name <name>` (or
  `local-node adapter add`), or through the Manager API with a `plugin`
  field; `GET /api/nodes/{id}/plugins` lists what a Node can run. The Node
  restarts a crashed plugin with backoff, buffers pushed events for the
  Bridge, and keeps each plugin's stderr in a size-capped log. Pi gains a
  `--stdio` mode.

- The Manager can delete a Node (not the Bridge's embedded Node). Deleting
  or disabling a paired Node now closes its live gateway connection, and
  the Node stays refused until it is enabled again or re-paired.

- One slow Node or adapter no longer stalls the whole Bridge. The global
  service lock was held across Node file and Git calls, and every MCP run
  mutation queued behind one global lock. Now the database lock covers only
  SQLite statements. Writes and handoff publication serialize per workspace,
  run starts per workspace route, and steer, queue and interaction answers
  per run. A cancel reaches the runtime without waiting in any of these
  queues. Queued calls still authenticate when their turn comes. The Node also serializes checked writes per workspace.

- `workspace-bridge service install|status|start|stop|restart|uninstall`
  runs the Bridge itself as a LaunchAgent (`com.workspace-bridge.bridge`) or
  systemd unit (`workspace-bridge.service`), reusing the Node's hardened
  service lifecycle. Settings come from a private `bridge.env` in the state
  folder, limited to known `WB_*` keys; real environment variables win.
- The gateway records paired Node connection state. `doctor` reports
  connected, disconnected, never connected or not reported, and shows paired
  workspace roots as unknown instead of failed. `node service status` reads
  the Node's own `link-status.json`.
- Paired Nodes: a Node can dial the Bridge instead of listening. The Bridge
  runs a TLS Node gateway (default port 8767, loopback unless
  `WB_NODE_GATEWAY_HOST` is set) and issues single-use pairing strings with
  `workspace-bridge pairing create` or `POST /api/nodes/pairings`; the Node
  redeems one with `workspace-bridge node pair`. Nodes pin the gateway
  certificate and authenticate with an Ed25519 key; no inbound port or Node
  token is needed, and reconnecting keeps runs alive.
- `workspace-bridge local-node enable --allow-root <path>` runs the Bridge
  host's Node inside the Bridge process, so one machine needs one service.
- Every Node request now runs in a worker thread through one shared handler,
  so a slow adapter or filesystem call no longer blocks the Node.
- New dependencies: `websockets` and `cryptography`.
- Runs no longer stay active forever after a Node or adapter URL or token
  changes. They are marked `interrupted` (`runtime_authority_changed`) with
  stale interactions, without contacting the replacement endpoint. The
  background sweep skips runs whose Node or adapter is disabled or changed,
  so they cannot starve newer runs, and visits the least recently updated
  runs first.
- New local-admin `POST /api/runs/<run_id>/abandon` marks an unreachable run
  `orphaned` (`abandoned_by_admin`) without contacting its runtime.
- Git evidence neutralizes clean/smudge/process filter drivers defined in a
  repository's `.git/config`; an unparseable filter section fails closed.
- The Manager's workspace routes and handoff document endpoints no longer
  block the admin listener while waiting for the Node.
- Direct-instruction runs publish their audit handoff under the same service
  lock as `prepare_handoff`.
- Run checkpoints: the owning Node captures each run's workspace before the
  native start and after it ends, in a private shadow Git store that never
  writes the workspace or its `.git`. `git_diff` `mode=run` returns the
  run-window diff, `read_agent_run` reports checkpoint status, changed paths and
  overlapping runs, and the Manager shows a Changes panel plus storage controls.
  Capture failures never block a run. Retention is 14 days and 1 GiB per
  workspace.

## 0.3.0

- Add the OpenCode v2 host adapter with shared native storage, owned server
  lifecycle, security profiles, approvals, native handoff, steering and queueing.
- Accept Node-decoded workspace image references on agent run starts.
- MCP image uploads: `write_image` saves validated raster images under Node-owned
  handoff paths with create-only or hash-protected replacement. `prepare_handoff`
  accepts image references with descriptions and original-file hashes in `CONTEXT.md`.
- Live run updates: bounded adapter event readers reconcile authoritative
  snapshots and notify the authenticated Manager through SSE, with polling
  retained for recovery and adapters without events. Reconnects reload REST
  state; streams contain only run IDs and respect session revocation.
- Manager Run Inspector is split into UI, API loaders, feed helpers, state
  hooks, and reusable event subscriptions. Refreshes coalesce bursts, preserve
  older timeline pages, and discard requests from a closed or previous run.
- Steering and follow-up queues are available through MCP and Manager for
  supported Pi, Codex, Claude Code, and OpenCode routes, using exact adapter
  capability checks and existing security bindings.
- Pi records bounded, redacted tool intent and results durably, detects
  interrupted work after crashes, and fails closed when audit persistence is
  unavailable. Recovery never replays prompts, approvals, or tool effects.
- MCP event subscriptions use verified signed webhooks with durable delivery
  recovery. App-bound skill packaging builds a local snapshot for an existing
  private or workspace MCP app.
- Version `0.3.0` across the Python product, host adapters, Pi package, Manager,
  OpenCode permission plugin, Docker image tag, and served release metadata.

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
