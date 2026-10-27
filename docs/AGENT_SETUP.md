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
- **Secrets:** tool output counts as disclosure. Never read secret files or
  print the shared gateway token. A pairing string is single use and goes from
  the Bridge host straight to `pair` on the Node host; keep it out of chat and
  reports. Bridge and Node `init` print no tokens. Sanitize errors.
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
3.11+ and `uv` are needed on every host that runs the Bridge or a Node.

Establish the topology and runtime choices before checking optional tools:

| Choice | Additional checks |
|---|---|
| One host: Bridge with its embedded Node | None |
| More hosts: a paired Node on each | Network path from each Node host to the Bridge's Node gateway (TLS, default port `8767`), normally a VPN such as Tailscale |
| Pi | `node --version` (20+), `npm --version` |
| Codex | `codex --version`; confirm the native CLI and required runtime login on its host |
| Claude Code | Confirm the host user's existing Claude Code login; the optional Python SDK extra supplies its CLI |

For missing tools, propose an installation using [References](REFERENCES.md),
including Astral's instructions for `uv`; do not assume an `apt` package for
`uv`. Approve downloads and privileged installs before proceeding.

**Checkpoint:** report OS, checked versions, topology, and runtime choices. Do
not require Pi or Node/npm for a Codex-only or Claude-only setup. Contributor
tests run only if requested; never install test assets implicitly.

## 2. Confirm the installation plan

Agree on the projects parent, component hosts, paths, ports, and instances.
These are generic examples, not detected settings:

| Component | State path | Port |
|---|---|---|
| Bridge (with its embedded Node) | `$HOME/.local/state/workspace-bridge` | MCP `8765`, Manager `8766`, Node gateway `8767` |
| Paired Node on another host | `$HOME/.local/state/workspace-bridge-node` | none (dials out) |
| Agent plugins | inside the Node state (`plugins/<adapter-id>/`), or the Bridge state for the embedded Node | none |

Use distinct available ports in `1024–65535` on the Bridge host. Keep
package installs, state, and private tunnel files outside mapped projects. Do
not overwrite an existing installation.

Use an existing dedicated projects parent such as `$HOME/Projects`, not a
home or filesystem root. The Node owns files, Git, handoffs, and adapter
secrets under its `allowed_roots` ceiling; the Bridge never opens roots directly.

Default to loopback. The Node gateway binds `127.0.0.1` unless
`WB_NODE_GATEWAY_HOST` names another address; for paired Nodes, agree on that
address (normally the Bridge's VPN IP) and any `WB_NODE_GATEWAY_URL` hostname
before starting the Bridge. Never expose a Node through the MCP tunnel.

Services are user services by default. `--root` (a LaunchDaemon or a system
unit, run with `sudo`) needs an explicit request.

**Checkpoint:** present the selected commands, paths, ports, and privilege or
network exposure. Obtain outstanding approvals and retain them during execution.

## 3. Install the selected packages

On each host, choose **one** Python install command:

```sh
# Without a Claude Code instance:
uv tool install workspace-bridge
# With a Claude Code instance (includes the optional SDK):
uv tool install 'workspace-bridge[claude]'
```

The Python package provides the Bridge, the Node, and the Codex, Claude Code,
and OpenCode v2 plugins. OpenCode v2 is a separate existing native
installation. Native Codex is separate. If adding the Claude extra to an
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

## 4. Initialize the Bridge and its embedded Node

Run on the Bridge host, using the approved projects parent:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" init \
  --mcp-port 8765 --admin-port 8766
workspace-bridge --state "$HOME/.local/state/workspace-bridge" local-node enable \
  --allow-root "$HOME/Projects"
```

Skip `local-node enable` when this host has no projects. Add each selected
agent on the Node that owns the projects. Agents run as plugins of the Node:
there is no adapter port, token or service.

```sh
# Pi only:
workspace-bridge --state "$HOME/.local/state/workspace-bridge" local-node adapter add pi --name Pi
# Codex only:
workspace-bridge --state "$HOME/.local/state/workspace-bridge" local-node adapter add codex --name Codex
# Claude Code only:
workspace-bridge --state "$HOME/.local/state/workspace-bridge" local-node adapter add claude --name "Claude Code"
```

Follow [Runtimes](RUNTIMES.md) for login and Claude settings-layer choices
(`--set setting_sources=...`).

**Checkpoint:** run `workspace-bridge --state <bridge-state> local-node adapter
test <adapter-id>` for each added adapter and report success or the stated
reason. Do not delete or reinitialize incomplete existing state.

## 5. Start the Bridge

Install and check the Bridge service:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" service install
workspace-bridge --state "$HOME/.local/state/workspace-bridge" service status
```

`service install` also starts it: a LaunchAgent on macOS, a systemd user unit
on Linux, running as the state owner without `sudo`. A Linux user unit
starts at boot only with lingering enabled; `status` reports
`starts_at_boot`. Put any `WB_*` settings (such as `WB_NODE_GATEWAY_HOST`) in
`bridge.env` in the Bridge state folder (mode `0600`) and restart the
service. The embedded Node starts its agent plugins itself.

For macOS privacy prompts, identify the executable or interpreter needing
access; the user grants it in System Settings. Do not bypass TCC or broaden
access automatically. See [Operations](OPERATIONS.md#service-persistence).

**Checkpoint:** report the service state and health, plus the Manager URL. A
running process does not prove route readiness.

## 6. Pair Nodes on other hosts

Skip this step for a single host. For each other Node host, the user issues a
pairing string on the Bridge host:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" pairing create --name laptop
```

Then, on the Node host:

```sh
NODE_STATE="$HOME/.local/state/workspace-bridge-node"
workspace-bridge node --state "$NODE_STATE" init --allow-root "$HOME/Projects"
workspace-bridge node --state "$NODE_STATE" pair 'wbpair1:...'
workspace-bridge node --state "$NODE_STATE" adapter add codex --name Codex
workspace-bridge node --state "$NODE_STATE" adapter test <adapter-id>
workspace-bridge node --state "$NODE_STATE" service install
workspace-bridge node --state "$NODE_STATE" service status
```

The user runs `pairing create` and `pair` themselves, or approves you running
`pair` with the string they place in a private file, so the string never
passes through chat. The Node service can be installed only after pairing.
`status` reports the Node's dial-out connection.

**Checkpoint:** report each Node's service state and connection status.

## 7. Configure the Manager — user steps

Hand the user these steps for the loopback Manager (for the example port,
`http://127.0.0.1:8766/`):

1. On fresh state, sign in as `admin` with temporary password `admin`, then
   change it before configuring the Manager. Existing installs use their
   current account; recovery is covered in [Operations](OPERATIONS.md).
2. Confirm each Node shows as connected; the embedded Node and paired Nodes
   register themselves.
3. Add one mapping per canonical Node-local workspace root, enable it, and
   choose its write scope (read-only `none` by default, or `workspace` to let
   ChatGPT edit files and run checks).
4. Confirm each adapter added in sections 4 and 6 appears under its Node (the
   Bridge discovers them on refresh); nothing needs to be entered.
5. Read each adapter's live model catalog, then save enabled selectors and
   the default model.
6. Enable each exact same-Node `(workspace_id, adapter_id)` route with its
   security binding. Pi, Claude Code, and OpenCode use a Bridge profile; Codex can use
   a Bridge profile or its native configuration source.

Write scope controls ChatGPT's file edits and checks; route enablement
controls runs. Neither grants the other. Runtime type never selects a destination.

**Checkpoint:** obtain non-secret registration results: Nodes connected,
mapping enabled, adapters healthy, model policies saved, and routes enabled
with security bindings.

## 8. Connect the tunnel and client — user steps

Follow [Setup's MCP connection steps](SETUP.md#8-mcp-tunnel-and-client-connection)
and the official guides in [References](REFERENCES.md). Verify the current
client UI before giving click instructions.

The user creates or rotates the gateway credential in the Manager, saves it
in the local tunnel environment (`0600`), authorizes the official client,
and starts or restarts the approved tunnel process. Tunnel only `/mcp`;
keep the Manager local and credentials out of chat and tool output.

**Checkpoint:** have the user confirm tunnel authorization and client
connection without sharing secrets. Local health checks do not prove this.

## 9. Verify and hand over

Run Doctor against the running Bridge's state:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" doctor
```

Add `--json` for structured output or `--offline` to skip network and adapter
calls. Offline readiness depending on live freshness is unknown, never ready.
Doctor reports paired Node connections from the Bridge's own records. Fresh
state can report `action_required` before gateway and route setup; report the
actual result and remaining steps.

Expected after setup: overall `pass`, with every intended route ready and its
configured default model. Have the user verify discovery and a small file
read in a real client conversation using a nonsensitive sample project.
See [Setup verification](SETUP.md#9-verify) for further acceptance checks.

**Handover:** report selected components, non-secret paths and ports, command
outcomes, Doctor results, client verification evidence, and unfinished user
steps or blockers. Distinguish local checks from observed client behavior.

## Rollback and cleanup

- With approval, `service uninstall` removes the Bridge's or a Node's
  service definition. State, keys, bindings, and logs remain.
- Deleting state is destructive. Back up stopped private state first,
  preserving permissions and SQLite files; Bridge state holds the handoffs.
- Preserve ignored local scripts, tunnel files, and private configuration.
  Never delete them merely for cleanup.
