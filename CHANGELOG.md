# Changelog

## Unreleased

- **Breaking:** one service module runs the Bridge and the Node. `service
  install|status|start|stop|restart|uninstall` installs a user service by
  default (a LaunchAgent on macOS, a systemd user unit on Linux); `--root`
  installs a system-wide service that starts at boot, must be run as root
  (for example with sudo) and runs as the state folder's owner. The service
  writes its own log, capped at 10 MiB plus two older files, to
  `<state>/logs/bridge.log` or `node.log`; the macOS log helper is gone.
  Definitions installed by 0.3 are left alone: remove them with the 0.3
  `service uninstall` (or delete the plist or unit) before installing.
- **Breaking:** the Node HTTP API, its listener and its token are removed.
  Nodes join only by pairing and dial out to the Bridge; `node init` takes no
  `--host`/`--port`, `node show-token` is gone, and the Manager can no longer
  add a Node by URL. Pairing refuses a Node whose version is not compatible
  with the Bridge (same major version; same minor before 1.0).
- **Breaking:** release bundles, manifests, staged rollout, Pi runtime
  artifacts, the Docker image and Compose files are removed, with
  `workspace-bridge release` and `/api/system/versions`. Releases ship a
  wheel and the Pi npm package; `--version`, `/api/status` and
  `doctor --json` (`versions`) report versions, and doctor warns when a
  Node's version differs from the Bridge's.
- **Breaking:** handoffs live in Bridge state, not in the workspace.
  `prepare_handoff` writes nothing to the project, so handoffs never show in
  `git status`; the run prompt and the copy-paste prompt carry the full TASK,
  CONTEXT and ACCEPTANCE text, and `read_handoff` also serves the copy-paste
  prompt as `PROMPT.md`. The Node also saves a read-only copy of each plan in
  its own state and the prompts name that folder, so an agent whose context
  was compacted can re-read its plan; a run may read its handoff's folder and
  each run start restores a changed copy. Handoffs published by earlier
  versions are still read from their `.workspace-handoff/` folder.
- **Breaking:** the write scope is either read-only (`none`, now the default)
  or edit (`workspace`). Preparing handoffs and starting runs need no write
  scope; editing files, `write_image` and ad hoc `run_checks` need edit.
  Workspaces set to the old `handoff` scope become read-only.
- New MCP tool `run_checks` runs checks the local administrator named in the
  workspace's `checks.toml`, kept in Node state outside the repository and
  edited through the Manager API (`/api/workspaces/{id}/checks`). Commands run
  without a shell, with a timeout and a bounded, redacted output tail. A
  workspace can also run named checks after each run: `read_agent_run`, the
  `run.finished` event and the Discord notice report how many passed and
  failed.
- `git_diff` with `mode=run` lists the `apply_patch` edits ChatGPT made
  during the run as `chatgpt_edits`.
- `start_agent_run` takes `isolation: "worktree"`: the agent works in its own
  Git worktree on a `wb/<id>` branch, made from the workspace's current files
  without touching its index, branch or working files. Continuations keep the
  same worktree. New MCP tools `apply_run` (copies the changes into the
  workspace as one edit that `undo_edit` reverts, all or nothing on conflict)
  and `discard_run`; unused worktrees are removed after 14 days. `read_file`,
  `list_dir`, `glob`, `grep_files` and `run_checks` take `run_id` to review an
  unapplied worktree before applying it. Plugins that
  declare the `worktrees` feature receive `worktreesRoot` in `initialize`.
- The project-lead skill (3.9.0) covers read-only and edit workspaces,
  Bridge-stored handoffs and their read-only plan folder, `run_checks` and
  after-run checks in the audit, and isolated runs.
- New MCP tools `apply_patch` and `undo_edit`. `apply_patch` edits, creates,
  moves and deletes up to 50 files (1 MiB of text) in one step: every
  existing file needs its current hash, and all changes apply or none do. It
  returns an `edit_id`; the Node journals the earlier content (30 days, or
  500 MiB per workspace) and `undo_edit` restores it while the files still
  hold what the patch wrote, otherwise naming the files changed since. Both
  follow the workspace write scope. The Manager API lists the journal at
  `GET /api/workspaces/{id}/edits`.
- Writes refuse only secret-like text that the file did not already hold, so
  files containing such a match are editable again with `write_file`,
  `edit_file` and `apply_patch`.
- A workspace exclusion entry `!name` shows one hidden-by-default generated
  or dependency folder, such as `!build` or `!vendor`. Credential and
  version-control names stay denied and cannot be shown.
- The project-lead skill (3.6.0) changes source with `apply_patch` and
  reports the `edit_id` for undo.
- **Breaking:** approvals are answered only in ChatGPT.
  `respond_agent_interaction` now requires `decided_by` (`chatgpt` or
  `owner`) and a `reason`, both recorded with the chosen option and time. An
  interaction is answered once; a retry or second answer fails with
  `already_answered`. The Manager no longer answers interactions (its
  `POST /api/runs/{id}/interactions/{id}` route is removed): it shows a
  waiting request read-only, who decided an answered one, and an approval log
  at `GET /api/approvals?decided_by=…`. `list_agent_runs` lists runs waiting
  on an answer first, and the Discord notice points to ChatGPT.
- The project-lead skill (3.5.0) says which requests ChatGPT approves on its
  own and which it asks you about, and subscribes to run events before every
  run, telling you plainly when the chat cannot receive them.
- Claude Code routes can use native security: runs keep Claude Code's own
  tools and permission settings, and each permission prompt becomes a Bridge
  interaction. The plugin's new `permission_mode` setting picks the mode, for
  example `--set permission_mode=auto` for auto mode, which Claude Code does
  not take from `defaultMode` under its SDK. Bypass mode stays disabled.
- `adapter set <adapter-id> --set KEY=VALUE` changes an adapter's plugin
  settings in place, keeping its ID and routes.
- `workspace-bridge plugin test <manifest|id>` runs the plugin conformance
  kit in a temporary folder: protocol checks by default, and with `--live`
  runs, interactions, events, recovery without replay, and continuation.
  It exits non-zero unless the plugin passes.
- Fixed: shell command rules now apply to commands wrapped in a shell, such
  as `zsh -lc 'git push'`. Codex runs every command this way, so its ask and
  deny command rules were not enforced before.
- The Pi plugin pushes events: run start and finish, approval requests, tool
  steps and retries reach the Bridge as they happen instead of on its
  5-second snapshot sweep.
- The Pi plugin now uses Pi SDK 1.4.0 and needs Node 22.19 or newer. Pi's
  ChatGPT login (provider `openai`, with models such as `openai/gpt-6-luna`)
  works with it; SDK 0.87 listed no models for that login.
- Fixed: an OpenCode conversation accepts new runs after the plugin
  restarts; they used to fail with "permission plugin is unavailable". A
  failed OpenCode run now reports OpenCode's own reason, such as a provider
  usage limit, instead of `native_execution_failed`.
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
