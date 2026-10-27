# Setup

Single canonical human installation and configuration guide for Workspace
Bridge. Follow it top to bottom. For daily operation see
[Operations](OPERATIONS.md); for Pi/Codex/Claude Code specifics see
[Runtimes](RUNTIMES.md).

If an AI/coding agent is performing this work for you, it follows
[Agent setup](AGENT_SETUP.md) instead. That runbook states what the agent
can run itself and where it must stop and ask you.

## 0. What you are building

```text
ChatGPT / MCP client -- one connection
  |
Private MCP tunnel -- one channel, shared gateway credential
  |
Bridge control plane -- /mcp plus a loopback-only Manager (never tunnel it)
  |
Authoritative Node (native, per host) -- files, Git, handoffs, agent plugins
  |
Claude Code, Codex, OpenCode and/or Pi plugins (child processes of the Node)
```

The Bridge never inspects workspace files directly; the Node owns the data
plane under its host `allowed_roots` ceiling and supervises one plugin
process per adapter. Keep package installs, state, and tunnel profiles
outside mapped projects.

## 1. Prerequisites

- macOS or Linux (native Windows is unsupported; use WSL2).
- Python 3.11+ and `uv` are always needed for Workspace Bridge itself.
  Node 22.19+ with npm is needed only for the Pi adapter (and for
  contributor/web work), not for a Codex-only deployment.
- Decide: a projects parent that already exists (for example
  `$HOME/Projects`), a Bridge state directory, a Node state directory, and
  distinct host ports in 1024–65535 for MCP and Manager.
- Default paths used below (generic examples; substitute your own):
  Bridge `$HOME/.local/state/workspace-bridge`, Node
  `$HOME/.local/state/workspace-bridge-node`. Agent plugins keep their state
  inside the Node state folder.

## 2. Install packages

Install the Python product persistently (provides `workspace-bridge`, the
nested `node` surface, and the Claude Code, Codex and OpenCode agent
plugins):

```sh
uv tool install workspace-bridge
workspace-bridge --version
```

Install the Pi plugin package on each Node host that will run Pi:

```sh
npm install -g workspace-bridge-pi-host-adapter
```

The global `--state` option selects a state directory and comes before the
subcommand:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" doctor --offline
```

## 3. Credentials you will handle

The Manager uses an admin password and MCP access uses one shared token.
Paired Nodes use keys, the embedded Node needs nothing, and agent plugins need
no credential of their own. Treat creation output as sensitive and local, and
never paste tokens or pairing strings into chat, project files, or handoffs.

| Credential | Created / stored | How to use it locally |
|---|---|---|
| Manager admin account | Username `admin`; temporary password `admin`; salted scrypt hash in private `admin-account.json` | Sign in locally and change the password on first login; recover with `workspace-bridge reset-admin-password` |
| Pairing string | `workspace-bridge pairing create --name <node>` on the Bridge host, or `POST /api/nodes/pairings`; single use, valid 10 minutes, shown once | Redeem on the Node host with `workspace-bridge node --state <node-state> pair 'wbpair1:...'`; the Node then keeps its own key |
| Shared MCP gateway token | Created or rotated by the Manager or `POST /api/bridge {"operation":"rotate_token"}` while serving, or by `workspace-bridge rotate-bridge-token` while stopped; stored in Bridge state | Copy the displayed value locally into the tunnel environment file (mode `0600`) as the `X-Bridge-Token` header; never tunnel the Manager |

Normal list and status APIs report only whether a token exists, never its
value. Rotation of the shared gateway credential also enables the gateway;
update the one tunnel environment and restart the tunnel process.

## 4. Bridge and the Node on this machine

The Bridge runs this machine's Node in its own process (the embedded Node),
so a single host needs no separate Node service:

```sh
workspace-bridge init
workspace-bridge local-node enable --allow-root "$HOME/Projects"
workspace-bridge serve
```

`--allow-root` may repeat; it is the ceiling of folders the Node may expose.
Open `http://127.0.0.1:8766/`, sign in as `admin` with temporary password
`admin`, change the password, and continue with section 6. The embedded Node
appears in the Manager with transport `local`. Keep this terminal attached;
stopping it stops the Bridge. To run the Bridge persistently instead, stop it
and install it as a service, with any `WB_*` settings in `bridge.env` in its
state folder (see [Operations](OPERATIONS.md#service-persistence)):

```sh
workspace-bridge service install
workspace-bridge service status
```

This installs a user service: a LaunchAgent on macOS, a systemd user unit on
Linux. `service install --root`, run as root (for example with `sudo`),
installs a system-wide service that starts at boot instead; the service still
runs as the account that owns the state folder.

## 5. Nodes on other machines

A Node on another machine dials the Bridge; it opens no inbound port. The
Bridge's Node gateway (TLS, default port 8767) binds `127.0.0.1` unless
`WB_NODE_GATEWAY_HOST` names another address, normally the Bridge's VPN (for
example Tailscale) IP. Set `WB_NODE_GATEWAY_URL` when Nodes should use a
hostname, such as `wss://bridge.example.ts.net:8767/v1/node-gateway`. Then
issue a single-use pairing string on the Bridge host and redeem it on the
Node:

```sh
# Bridge host: valid for 10 minutes, shown once
workspace-bridge pairing create --name laptop
```

```sh
# Node host
NODE_STATE="$HOME/.local/state/workspace-bridge-node"
workspace-bridge node --state "$NODE_STATE" init --allow-root "$HOME/Projects"
workspace-bridge node --state "$NODE_STATE" pair 'wbpair1:...'
workspace-bridge node --state "$NODE_STATE" service install
workspace-bridge node --state "$NODE_STATE" service status
```

The pairing string carries the gateway certificate fingerprint, so the first
connection is already pinned, and pairing refuses a Node whose version is
incompatible with the Bridge. The Node registers itself; it reconnects on its
own after network loss without affecting runs. To give it a new key, issue
`pairing create --name laptop --replace` and pair again; runs started under
the old key become interrupted. `doctor` cannot see paired connections and
reports them as unknown; check them in the Manager. The Node service takes
`--root` the same way as the Bridge service.

## 6. Runtime adapters (native, per host)

### Plugin adapters (no separate service)

A Node can run each agent itself as a child process (an agent plugin), so
an adapter needs no port, token or service of its own. Install the agent's
own CLI and sign in to it as usual on that host, then add the adapter on the
Node that owns the projects:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" plugin list
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter add claude --name "Claude Code"
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter add codex --name "Codex"
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter test <adapter-id>
```

For the Bridge machine's embedded Node use `workspace-bridge local-node
adapter add ...` instead of `workspace-bridge node ... adapter add`. `--projects-root` defaults to the Node's only
allowed root and must stay inside the allowed roots. Plugin settings are
passed with `--set`, for example `--set binary=/opt/homebrew/bin/claude` or
`--set setting_sources=user`; `plugin list` shows each plugin's settings.
`adapter set <adapter-id> --set KEY=VALUE` changes them later, keeping the
adapter ID and its routes (`KEY=` clears one); the plugin restarts, and runs
active on it are interrupted.
Pi needs the npm package `workspace-bridge-pi-host-adapter` installed so its
`workspace-bridge-pi-adapter` command is on the Node's `PATH`. The Manager
can create the same adapters through `POST /api/adapters` with a `plugin`
field. The Node starts a plugin on first use, restarts it with backoff if it
exits, and writes its stderr to `plugins/<adapter-id>/plugin.log` in the
Node state folder. The Bridge discovers the adapter on its next refresh.

### Platform notes

Agent plugins run as child processes of the Node, as the Node's OS user,
and are covered by the Node or Bridge service. On macOS, privacy controls
(Files & Folders, external-storage access) can still block a workspace path:
grant only the required access to the actual executable or interpreter when
prompted. Full Disk Access is not mandatory when a narrower permission
suffices. A service writes its own size-capped log to `logs/` in its state
folder; each plugin's own stderr is in its `plugin.log`.

A `--root` service still runs the Node, and every agent plugin it starts, as
the account that owns the Node state. Only a root-owned state runs as root,
with full filesystem authority; keep that for hosts that need it. See
[Operations](OPERATIONS.md#service-persistence).

## 7. Register Node, workspaces, adapters, routes

Sign in to the Manager with the admin account. Local API clients must POST
`{"username":"admin","password":"<password>"}` as JSON to `/api/login`
and keep the HttpOnly session cookie. A temporary-password session can only
read `/api/account`, change the password through `/api/account/password`
(with JSON `current_password` and `new_password`), or sign out. Bearer tokens
are not accepted by the Manager. Order matters:

1. **Node:** the embedded Node (section 4) and paired Nodes (section 5)
   register themselves; check that each shows as connected.
2. **Workspaces:** add one mapping per canonical Node-local root, then enable
   it and set write scope (`none`, the default, is read-only; `workspace`
   lets ChatGPT edit permitted files and run checks; handoffs and runs work
   in both).
3. **Adapter instances:** on the owning Node host, add each agent as a plugin
   with `workspace-bridge node adapter add <plugin> --name <name>` (section
   6), or through `POST /api/adapters` with `node_id`, `name` and `plugin`.
4. **Model policy** per adapter: list the live catalog first, then save the
   enabled selectors plus default. Every selector must currently exist or the
   save is rejected. With no policy the adapter is unrestricted by Bridge
   governance and the runtime chooses its own default.
5. **Routes** per workspace: enable the exact `(workspace_id, adapter_id)`
   pair and set its security binding. Pi, Claude Code, and OpenCode use a Bridge profile;
   Codex uses either a Bridge profile or its native configuration source. Discovery and
   binding use the exact workspace context.

Node connection and adapter setting changes take effect on the next request
without restarting the Bridge. The Node `allowed_roots` ceiling is configured on the Node host
and is not editable from the Bridge.

## 8. MCP tunnel and client connection

Only `/mcp` is tunnelled; the Manager stays local. The generated tunnel
profile contains no credentials and points at the configured MCP port.

1. In the Manager, rotate or create the shared gateway credential once and
   put it in the local tunnel environment (mode `0600`) as the
   `X-Bridge-Token` header value. Recreate or restart the tunnel process.
2. Install and authorize the official tunnel client per its own repository
   and the secure-tunnel guide; create or select a tunnel and a scoped
   runtime key. Confirm the Platform tunnel permissions the guide requires
   and that the ChatGPT account has developer-mode access. Do not paste
   account keys into chat.
3. Copy the example profile to a private directory (or use the Manager
   profile), replace the tunnel ID placeholder with the real authorized ID,
   and keep the actual configured local MCP port:

```yaml
config_version: 1
control_plane:
  tunnel_id: tunnel_REPLACE_WITH_YOUR_32_HEX_ID
  api_key: env:CONTROL_PLANE_API_KEY
mcp:
  server_urls:
    - channel: main
      url: http://127.0.0.1:8875/mcp
  extra_headers:
    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN
  discovery_extra_headers:
    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN
```

4. Run the tunnel with the official client (the included helper only prompts
   for secrets and launches that client when you invoke it; it is not an MCP
   tool and the server cannot invoke it).
5. Connect ChatGPT to the authorized tunnel. The current OpenAI documented
   flow (product UI may evolve; see [References](REFERENCES.md) and the
   official Secure MCP Tunnel guide) is: in ChatGPT Plugins, use the
   plus/create developer-mode app flow, choose `Tunnel` as the Connection,
   and select the associated tunnel or enter its `tunnel_id`. Never expose
   runtime API keys or tunnel secrets in this step.
6. Refresh tool discovery after MCP schema changes. Start with
   `list_workspaces`, then `workspace_info` and a small `read_file` with
   the selected ID.

## 9. Verify

```sh
workspace-bridge doctor
workspace-bridge doctor --json
workspace-bridge doctor --offline
```

Expected: overall `pass`, and every intended workspace/adapter route listed
as ready with its default model. Offline mode skips adapter and network
calls; readiness that depends on live freshness is then reported unknown and
never ready. The Manager uses the same server-side report. If diagnostics
cannot refresh, the Manager marks readiness unavailable while leaving other
data visible.

Also validate in a real client conversation with a nonsensitive sample
project: discovery lists only enabled mappings, wrong-workspace IDs fail,
disabled mappings disappear, pause denies requests, and rotation revokes the
old credential.

## 10. Updates

There is no automatic or remote updater. Update each component locally on its
host, then explicitly restart its service. A package upgrade never restarts
anything by itself. The Manager System / Versions view shows component
versions and compatibility as information only; it performs no installs.

The Bridge, the Node and the Claude Code, Codex and OpenCode plugins share one
Python package; the Pi plugin is its own npm package:

```sh
uv tool upgrade workspace-bridge
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge --version
workspace-bridge service restart
```

On a host with a paired Node, restart it too:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
```

Restarting a Node restarts its plugins. These steps do not update the
native Codex, Claude Code, or Pi CLIs. Restarting interrupts any run in
flight on that Node, so check for active runs first. See
[Operations](OPERATIONS.md#manual-upgrades) for details.

## 11. Uninstall

Stop and remove services before deleting state. Service removal preserves
state, tokens, bindings, and logs; deleting state is the destructive step.

```sh
workspace-bridge service uninstall
```

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service uninstall
```

Then, only if you intend to lose local data, remove the state directories
and the tunnel environment. Back up stopped private state first, preserving
permissions and SQLite files; Bridge state holds the handoffs.

## 12. Troubleshooting first aid

- Doctor reports `action_required` on a fresh Bridge for gateway credential
  and gateway enabled: create the shared credential in the Manager.
- `service install` refuses to replace a launchd or systemd definition it
  did not write (one without its marker line): remove that definition by
  hand first.
- A paired Node stays disconnected: check that the Node host reaches the
  gateway address in `WB_NODE_GATEWAY_URL` (or the gateway host and port),
  and read `logs/node.log` in the Node state folder.
- An adapter fails its test: run `workspace-bridge node adapter test
  <adapter-id>` on the Node host and read the `plugin.log` it names.
- Model saves fail: read the live catalog first; stale selectors are
  rejected at save time.
- No live service restart, publish, or credential rotation is performed by
  reading this guide. See [Operations](OPERATIONS.md) for recovery detail.
