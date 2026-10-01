# Agent setup runbook

Deterministic runbook for an AI/coding agent asked to install or configure
Workspace Bridge for a user. An agent following only this file prepares the
local host safely and knows exactly when it cannot proceed automatically.

Rules that override everything below:

- Never print, copy, or expose secrets into chat. Tokens stay in local files
  and local commands only. Refer to them as `<admin-password>`,
  `<gateway-token>`, `<node-token>`, `<adapter-token>` when talking to the
  user.
- Never invent URLs, versions, ports, paths, or validation claims. Use the
  commands and files in this runbook against the current checkout.
- Do not restart, install, or uninstall live services silently; confirm each
  mutating step and report the exact command plus outcome.
- Do not access Linux test servers, publish packages, push, deploy, rotate
  credentials on your own initiative, or modify private or ignored configs
  except the explicit local state and tunnel files below.

## Stage 0 — host, platform, and prerequisite discovery

Run first. Stop if the platform is unsupported.

```sh
uname -a
python3 --version
uv --version
node --version
npm --version
docker --version
docker compose version
```

Record: OS (macOS or Linux), native Windows unsupported (WSL2 only),
and the checkout path (use the actual current directory; never a personal
machine path from examples). Prerequisites are conditional:

- Python 3.11+ and `uv` are always needed for Workspace Bridge itself.
- Node 20+ with npm is needed only when the user selects the Pi runtime
  (or for contributor/web work), not for a Codex-only deployment.
- Docker/Compose is needed only for container Bridge (Path B).

Check only what the planned deployment needs: always `python3 --version`
and `uv --version`; add `node --version` and `npm --version` for Pi or
contributor work; add `docker --version` and `docker compose version` for
Path B.

Checkpoint: report OS, checked tool versions, checkout path, and whether
container Bridge is available. Ask the user to choose Path A (native
evaluation) or Path B (container Bridge plus native Node) from
[Setup](SETUP.md). Do not assume.

Stop/ask: if the OS is native Windows, stop and explain WSL2 is required.
If required tools are missing, stop and propose the install step for the
detected OS without executing it. Any downloaded installer or `sudo`
package install is an explicit stop/ask step; do not auto-execute it.

Supported install sources only (examples; use the host's manager and confirm
first):

```sh
# macOS
brew install uv python3
brew install node   # only when the Pi runtime is selected
# Debian/Ubuntu (install Python and, only for Pi, Node/npm with the
# distro package manager as applicable; do not assume an apt package for uv)
sudo apt-get update && sudo apt-get install -y python3
```

Install `uv` using an Astral-documented method; see the Astral install docs at
https://docs.astral.sh/uv/getting-started/installation/. Do not assume an
`apt` package for `uv` and never present an `apt` command that installs it.

## Stage 1 — local preparation the agent may run

These commands do not mutate live services and do not handle secrets.

```sh
uv tool install workspace-bridge
workspace-bridge --version
```

Runtime package installs happen in Stage 3 after the user selects Pi-only,
Codex-only, or both (Codex needs no npm package; Pi needs
`npm install -g workspace-bridge-pi-host-adapter` on its host).

Validate checkout tests only if the user asked for contributor verification
(see [Contributing](../CONTRIBUTING.md)); otherwise skip to Stage 2.
Never install or update browsers or test assets implicitly.

Checkpoint: report installed versions. If installation fails, stop and paste
the exact error; do not retry with broader permissions.

## Stage 2 — plan state, ports, and runtimes (no mutation)

Agree with the user before creating anything:

- Projects parent (must already exist; generic form `$HOME/Projects`).
- Bridge state, Node state, and one adapter state directory per SELECTED
  instance (generic form `$HOME/.local/state/<name>`; directories must not
  overlap a mapped project).
- Distinct MCP and Manager host ports (1024–65535).
- Node port (default `8770`); Pi port (`8780`) only for Pi; Codex port
  (`8772`) only for Codex.
- Runtime selection: Pi-only, Codex-only, or both. Every later stage
  executes ONLY the selected runtime(s): never initialize or install Pi
  for a Codex-only setup, and never omit Codex for a both-runtimes setup.

Command templates (fill in the agreed values; include ONLY the selected
runtime blocks; do not run until the user confirms the plan):

```sh
workspace-bridge init
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects"
# Pi only (needs Node 20+/npm on this host):
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" init \
  --runtime pi --projects-root "$HOME/Projects" --port 8780 > /dev/null
# Codex only (no npm package; executable ships with uv tool install):
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" init \
  --runtime codex --projects-root "$HOME/Projects" --port 8772 > /dev/null
```

For Docker Desktop Bridge, the Node template instead uses an explicit
non-loopback host (requires a firewall review; see [Setup](SETUP.md)):

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects" --host 0.0.0.0 --port 8770
```

Checkpoint: present the exact planned commands and paths. Wait for explicit
confirmation.

## Stage 3 — create state (agent-runnable after confirmation)

Run only after the user approved the Stage 2 plan, and only the selected
runtime block(s).

Token-safety rule: an agent tool transcript counts as disclosure to the
agent even if nothing is pasted into chat. Adapter `init` prints its
one-time runtime token on stdout, so the agent MUST redirect that stdout to
`/dev/null` (the token remains safe in the state's private `runtime-token`
file; only a non-secret stderr note stays visible). The user later runs
`show-token` locally for Manager entry. Bridge `init` and Node `init` print
no tokens and may run normally; every `show-token` command
remains a user/manual step the agent never executes.

```sh
workspace-bridge init
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects"
# Pi only, when selected:
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" init \
  --runtime pi --projects-root "$HOME/Projects" --port 8780 > /dev/null
# Codex only, when selected:
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" init \
  --runtime codex --projects-root "$HOME/Projects" --port 8772 > /dev/null
```

Checkpoint: confirm each created state directory (Bridge, Node, plus each
selected adapter instance) exists with private permissions, without ever
displaying a token. If a state directory is half-empty and `init` fails
closed, stop and ask whether to restore or wipe it fully.

## Stage 4 — services (explicit stop/ask points)

Service install, start, stop, restart, and uninstall are mutating. Confirm
each command with the user first.

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
# Each selected adapter instance only:
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service status
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" service status
```

Select exactly the instances chosen in Stage 2 (Pi-only, Codex-only, or
both); checkpoints enumerate those instances by name.

Stop/ask before proceeding when any of these appear:

- `sudo` is requested (Linux systemd steps): explain the narrow
  administration being authorized and wait for approval. Never prepend
  `sudo` yourself to a command that forbids it.
- macOS privacy (TCC) prompts for Files & Folders or external storage: tell
  the user which executable or interpreter needs access and wait for them to
  grant it in System Settings. Do not automate or bypass the prompt.
- Password or token entry (`show-token`, Manager login,
  Node/adapter forms): the user performs the copy or entry. The agent never
  reads a token aloud into chat.
- Firewall or non-loopback decisions (`--host 0.0.0.0`,
  `http://host.docker.internal:<port>`, remote-admin widening): explain the
  exposure and wait for an explicit choice. Default to loopback.
- ChatGPT or MCP client UI connection steps: give the user the exact click
  path and wait. The agent cannot complete another app's UI.
- Root mode, per component: a root-owned Node state managed as root runs the
  Node as root with full Node filesystem authority; a root-owned adapter
  state managed as root runs that adapter and its native agent as root.
  Running the Node as root does not by itself make separately non-root
  adapter services root. Proceed only on explicit request with a root-owned
  state.

Checkpoint after each service command: report installed/running state and
the bounded health excerpt. On failure, stop and report; do not retry with
escalated privilege.

## Stage 5 — Manager registration (user/manual UI actions)

The agent cannot log into the Manager for the user. Hand over with exact
steps:

1. User opens `http://127.0.0.1:8766/`, signs in as `admin` with temporary
   password `admin`, and changes the password before using the Manager. If
   they lose it later, `workspace-bridge reset-admin-password` is local recovery.
2. User adds the Node URL plus `<node-token>`.
3. User adds one workspace mapping per canonical Node-local root, enables
   it, and sets write scope (`handoff` default).
4. User adds each adapter instance on its Node with name, runtime type, the
   Node-host-relative base URL, and `<adapter-token>`.
5. User lists the live model catalog per adapter, then saves enabled models
   plus default.
6. User enables the exact `(workspace_id, adapter_id)` route and sets its
   security binding.

Checkpoint: ask the user to confirm each row (Node reachable, mapping
enabled, adapter healthy, policy saved, route enabled) before continuing.

## Stage 6 — tunnel and verification

Tunnel secrets stay local. The agent prepares files; the user authorizes the
client.

```sh
workspace-bridge doctor
workspace-bridge doctor --json
workspace-bridge doctor --offline
```

For container Bridge:

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

Expected: overall pass and each intended route ready with its default model.
Offline skips live checks; readiness depending on live freshness is unknown,
never ready.

Tunnel handover: the user creates or rotates the shared gateway credential
in the Manager, saves it to the local tunnel environment (mode `0600`),
restarts the tunnel process, and completes the client connection to `/mcp`
only. The Manager is never tunnelled. Confirm the Platform tunnel
permissions the official guide requires and that the ChatGPT account has
developer-mode access, then hand over the current OpenAI documented flow
(product UI may evolve; see [References](REFERENCES.md) and the official
Secure MCP Tunnel guide): in ChatGPT Plugins, use the plus/create
developer-mode app flow, choose `Tunnel` as the Connection, and select the
associated tunnel or enter its `tunnel_id`. Never expose runtime API keys
or tunnel secrets in this step.

## Rollback and cleanup

- `service uninstall` removes only the managed unit; state, tokens,
  bindings, and logs remain. Use it to undo Stage 4 without losing data.
- Deleting a state directory is destructive: back up stopped private state
  and handoff folders first, preserving permissions and SQLite files.
- `docker compose down` removes containers and network, not host bind
  directories.
- Never delete or modify ignored user-local scripts, tunnel credentials or
  configs, `compose-prod.yaml`, or private config and state merely for
  cleanup. Never commit, push, or publish.
- If any step contradicts current `--help` output or package metadata, stop
  and report the inconsistency instead of working around it.
