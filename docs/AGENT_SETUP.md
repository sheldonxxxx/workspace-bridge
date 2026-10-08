# Agent setup runbook

Use this runbook when a user asks an AI/coding agent to install or configure
Workspace Bridge. [Setup](SETUP.md) is the canonical human guide;
[Contributing](../CONTRIBUTING.md) covers repository development.

Follow the steps in order, using the user's existing choices and approvals.
Ask only for outstanding decisions or authorization. Substitute approved
paths and ports in the templates; run only the selected blocks.

## Execution rules

- **Agent checks:** inspect the host, command help, package metadata,
  permissions, service status, and diagnostics. Report observed results.
- **Mutations:** present exact commands before execution. Approval must cover
  package installs, state creation, service changes, and any privileged or
  non-loopback step. Stop on failure; never retry with broader permissions.
- **User steps:** passwords, tokens, runtime login, tunnel authorization, and
  client connection stay with the user. Never collect credentials through
  chat, tools, screenshots, or handoffs.
- **Secrets:** tool output counts as disclosure. Never execute `show-token`
  or read secret files. Redirect adapter `init` stdout to `/dev/null` because
  it prints a token. Bridge and Node `init` print no tokens. Sanitize errors.
- **Scope:** preserve existing state and private or ignored configuration.
  Unrelated hosts, commits, pushes, publication, deployment, credential
  rotation, and deletion require explicit authorization. Keep the Manager
  loopback-only and never tunnel it.

If a command or package entry point differs from this guide, stop and report
it. Source and current `--help` output are authoritative.

## 1. Discover the host and required tools

```sh
uname -a
pwd
python3 --version
uv --version
```

Supported hosts are macOS and Linux; native Windows requires WSL2. Python
3.11+ and `uv` are needed for the native Node and Python adapters, including
when the Bridge itself runs in a container.

Establish deployment and runtime choices before checking optional tools:

| Choice | Additional checks |
|---|---|
| Path A: native evaluation | No Docker requirement |
| Path B: container Bridge, native Node and adapters | `docker --version`, `docker compose version`; locate the checkout containing Compose and the setup helper |
| Pi | `node --version` (20+), `npm --version` |
| Codex | `codex --version`; confirm the native CLI and required runtime login on its host |
| Claude Code | Confirm the host user's existing Claude Code login; the optional Python SDK extra supplies its CLI |

For missing tools, propose an installation using [References](REFERENCES.md),
including Astral's instructions for `uv`; do not assume an `apt` package for
`uv`. Approve downloads and privileged installs before proceeding.

**Checkpoint:** report OS, checked versions, checkout path where relevant,
deployment, and runtime choices. Do not require Pi or Node/npm for a
Codex-only or Claude-only setup. Contributor tests run only if requested;
never install test assets implicitly.

## 2. Confirm the installation plan

Agree on the projects parent, component hosts, paths, ports, and instances.
These are generic examples, not detected settings:

| Component | State path | Port |
|---|---|---|
| Native Bridge (Path A) | `$HOME/.local/state/workspace-bridge` | MCP `8765`, Manager `8766` |
| Container Bridge (Path B) | `$HOME/.local/state/workspace-bridge-docker` on the host, `/state` inside the container | Published MCP `8875`, Manager `8766` |
| Native Node | `$HOME/.local/state/workspace-bridge-node` | `8770` |
| Pi instance | `$HOME/.local/state/workspace-bridge-adapter-pi` | `8780` |
| Codex instance | `$HOME/.local/state/workspace-bridge-adapter-codex` | `8772` |
| Claude Code instance | `$HOME/.local/state/workspace-bridge-adapter-claude` | `8774` |

Use distinct available ports in `1024–65535` for listeners on the same host.
Keep package installs, state, and private tunnel files outside mapped
projects. Each adapter instance needs its own state directory and port.
Adapter state must not already exist, even as an empty directory; its parent
must already exist. Do not overwrite an existing installation.

Use an existing dedicated projects parent such as `$HOME/Projects`, not a
home or filesystem root. The Node owns files, Git, handoffs, and adapter
secrets under its `allowed_roots` ceiling; the Bridge never opens roots directly.

Default to loopback. For Path B, decide how the container reaches the Node
before initialization. Docker Desktop normally uses
`http://host.docker.internal:8770` with an explicitly approved non-loopback
Node listener and firewall review. Linux Engine needs a host address reachable
from the container. Adapter URLs are relative to the Node host, where
loopback is appropriate. See [Docker](DOCKER.md).

**Checkpoint:** present the selected commands, paths, ports, and privilege or
network exposure. Obtain outstanding approvals and retain them during execution.

## 3. Install the selected packages

On each native component host, choose **one** Python install command:

```sh
# Without a Claude Code instance:
uv tool install workspace-bridge
# With a Claude Code instance (includes the optional SDK):
uv tool install 'workspace-bridge[claude]'
```

The Python package provides Bridge, Node, Codex adapter, and Claude Code
adapter commands. Native Codex is separate. If adding the Claude extra to an
existing installation requires `--force`, include the replacement in the plan.

Only on hosts selected for Pi:

```sh
npm install -g workspace-bridge-pi-host-adapter
```

Check the installed product:

```sh
workspace-bridge --version
workspace-bridge --help
```

**Checkpoint:** report installed versions or the sanitized failure.

## 4. Initialize the native Node and selected adapters

Run on the Node host, using the approved projects parent:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects" --port 8770
```

For an approved Docker Desktop deployment, add `--host 0.0.0.0` to the Node
initialization. Never expose the Node through the MCP tunnel.

Run selected adapter blocks on their hosts. Tokens remain in private
`runtime-token` files; redirection keeps them out of tool output.

```sh
# Pi only:
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" init \
  --runtime pi --projects-root "$HOME/Projects" --port 8780 > /dev/null
# Codex only:
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" init \
  --runtime codex --projects-root "$HOME/Projects" --port 8772 > /dev/null
# Claude Code only:
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-claude" init \
  --runtime claude --projects-root "$HOME/Projects" --port 8774 > /dev/null
```

Follow [Runtimes](RUNTIMES.md) for login and Claude settings-layer choices.

**Checkpoint:** enumerate created instances and verify directory (`0700`)
and credential-file (`0600`) permissions using metadata only. Do not delete
or reinitialize incomplete existing state.

## 5. Start the approved components

Install and check the native Node service:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
```

For **each selected adapter**, substitute its approved state path:

```sh
workspace-bridge adapter --state "/path/to/selected-adapter-state" service install
workspace-bridge adapter --state "/path/to/selected-adapter-state" service status
```

`service install` also starts the component: a per-user LaunchAgent on macOS,
or a system unit running as the state owner on Linux. Run Linux commands as
the owner; the backend requests `sudo` for narrow systemd operations. Approve
those first and let the user handle password prompts. Never prepend `sudo`.

For macOS privacy prompts, identify the executable or interpreter needing
access; the user grants it in System Settings. Do not bypass TCC or broaden
access automatically. Root mode requires an explicit request and root-owned
state for each component; a root Node does not make non-root adapters root.
See [Operations](OPERATIONS.md).

Start exactly one Bridge path:

### Path A: native evaluation

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" init \
  --mcp-port 8765 --admin-port 8766
workspace-bridge --state "$HOME/.local/state/workspace-bridge" serve
```

Keep `serve` attached; stopping it stops the Bridge. The CLI has no Bridge
`service install`; see [Operations](OPERATIONS.md) for persistent service choices.

### Path B: container Bridge

Run from the approved checkout as the normal host user:

```sh
cd /path/to/workspace-bridge
python3 scripts/configure_docker.py \
  --state-dir "$HOME/.local/state/workspace-bridge-docker" \
  --mcp-port 8875 --admin-port 8766
```

The helper creates private state and a credential-free `.env`, without
starting Docker or overwriting `.env`. Have the user prepare Compose's local
`tunnel-client.yaml` and `tunnel.env` privately using
[Docker's tunnel setup](DOCKER.md#ports-and-the-single-tunnel).
Start only the Bridge until step 7:

```sh
docker compose config --quiet
docker compose up -d --build bridge
docker compose ps bridge
```

The container initializes fresh Bridge state at `/state`; do not run native
Bridge `init` for Path B. Keep the Node and adapters native.

**Checkpoint:** report each component's service state and bounded health,
plus the Manager URL. A running process does not prove route readiness.

## 6. Register components in the Manager — user steps

Hand the user these steps for the loopback Manager (for the example port,
`http://127.0.0.1:8766/`):

1. On fresh state, sign in as `admin` with temporary password `admin`, then
   change it before configuring the Manager. Existing installs use their
   current account; recovery is covered in [Operations](OPERATIONS.md).
2. Reveal the Node token **locally** with
   `workspace-bridge node --state <node-state> show-token`, then add its URL
   and token in the Node form.
3. Add one mapping per canonical Node-local workspace root, enable it, and
   choose its write scope (`handoff` by default).
4. Reveal each adapter token **locally** with
   `workspace-bridge adapter --state <adapter-state> show-token`. On its
   owning Node, register the instance name, runtime type, Node-host-relative
   base URL, and token.
5. Read each adapter's live model catalog, then save enabled selectors and
   the default model.
6. Enable each exact same-Node `(workspace_id, adapter_id)` route with its
   security binding. Pi and Claude Code use a Bridge profile; Codex can use
   a Bridge profile or its native configuration source.

Write scope controls publication and mutation; route enablement controls
runs. Neither grants the other. Runtime type never selects a destination.

**Checkpoint:** obtain non-secret registration results: Node reachable,
mapping enabled, adapters healthy, model policies saved, and routes enabled
with security bindings.

## 7. Connect the tunnel and client — user steps

Follow [Setup's MCP connection steps](SETUP.md#8-mcp-tunnel-and-client-connection)
and the official guides in [References](REFERENCES.md). Verify the current
client UI before giving click instructions.

The user creates or rotates the gateway credential in the Manager, saves it
in the local tunnel environment (`0600`), authorizes the official client,
and starts or restarts the approved tunnel process. Tunnel only `/mcp`;
keep the Manager local and credentials out of chat and tool output.

For Path B, the approved tunnel start is:

```sh
docker compose up -d mcp-tunnel
```

**Checkpoint:** have the user confirm tunnel authorization and client
connection without sharing secrets. Local health checks do not prove this.

## 8. Verify and hand over

Run Doctor against the running Bridge's state and network context.
For Path A:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" doctor
```

For Path B:

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor
```

Add `--json` for structured output or `--offline` to skip network and adapter
calls. Offline readiness depending on live freshness is unknown, never ready.
Fresh state can report `action_required` before gateway and route setup;
report the actual result and remaining steps.

Expected after setup: overall `pass`, with every intended route ready and its
configured default model. Have the user verify discovery and a small file
read in a real client conversation using a nonsensitive sample project.
See [Setup verification](SETUP.md#9-verify) for further acceptance checks.

**Handover:** report selected components, non-secret paths and ports, command
outcomes, Doctor results, client verification evidence, and unfinished user
steps or blockers. Distinguish local checks from observed client behavior.

## Rollback and cleanup

- With approval, `service uninstall` removes the selected Node or adapter's
  managed service. State, tokens, bindings, and logs remain.
- With approval, `docker compose down` removes containers and the network,
  not host bind directories.
- Deleting state is destructive. Back up stopped private state and workspace
  handoff folders together first, preserving permissions and SQLite files.
- Preserve ignored local scripts, tunnel files, `compose-prod.yaml`, and
  private configuration. Never delete them merely for cleanup.
