# Setup

Single canonical human installation and configuration guide for Workspace
Bridge `0.3.0`. Follow it top to bottom. For container detail see
[Docker](DOCKER.md); for daily operation see [Operations](OPERATIONS.md);
for Pi/Codex/Claude Code specifics see [Runtimes](RUNTIMES.md).

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
  Node 20+ with npm is needed only for the Pi adapter (and for
  contributor/web work), not for a Codex-only deployment. Docker
  Engine/Desktop with Compose v2 is needed only if you run the Bridge in a
  container.
- Decide: a projects parent that already exists (for example
  `$HOME/Projects`), a Bridge state directory, a Node state directory, and
  distinct host ports in 1024–65535 for MCP and Manager.
- Default paths used below (generic examples; substitute your own):
  Bridge `$HOME/.local/state/workspace-bridge`, Node
  `$HOME/.local/state/workspace-bridge-node`. Agent plugins keep their state
  inside the Node state folder.

Docker is optional. Compose builds the Bridge image locally by default, and each
release also publishes a multiarch image to
`ghcr.io/sheldonxxxx/workspace-bridge`; see [Docker](DOCKER.md).

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

The Manager uses an admin password; HTTP Node and MCP access use tokens.
Paired Nodes use keys, and agent plugins need no credential of their own.
Token creation, storage, and local reveal are distinct steps: reveal commands only read back a stored secret, they do
not create it. Treat initial creation output as sensitive and local, and
never paste tokens into chat, project files, or handoffs.

| Token | Created / stored | How to reveal / enter locally |
|---|---|---|
| Manager admin account | Username `admin`; temporary password `admin`; salted scrypt hash in private `admin-account.json` | Sign in locally and change the password on first login; recover with `workspace-bridge reset-admin-password` |
| Node token | Created during `workspace-bridge node --state <node-state> init`, stored privately in Node state (`node-token`) | Reveal locally with `workspace-bridge node --state <node-state> show-token`; enter in the Manager Node form (or `POST /api/nodes`); stored in private Bridge SQLite (mode `0600`) |
| Shared MCP gateway token | Created or rotated by the Manager or `POST /api/bridge {"operation":"rotate_token"}` while serving, or by `workspace-bridge rotate-bridge-token` while stopped; stored in Bridge state | Copy the displayed value locally into the tunnel environment file (mode `0600`) as the `X-Bridge-Token` header; never tunnel the Manager |

Normal list and status APIs report only whether a token exists, never its
value. Blank token fields on edit preserve the stored value. Rotation of the
shared gateway credential also enables the gateway; update the one tunnel
environment and restart the tunnel process.

## 4. Path A — evaluation (native Bridge on loopback)

Run everything natively on one host. Safe loopback defaults apply.

```sh
workspace-bridge init
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects"
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge serve
```

Open `http://127.0.0.1:8766/`, sign in as `admin` with temporary password `admin`, change the password, and continue with
section 6. Keep this terminal attached; stopping it stops the Bridge. To run
the Bridge persistently instead, stop it and install it as a service, with any
`WB_*` settings in `bridge.env` in its state folder (see
[Operations](OPERATIONS.md#service-persistence)):

```sh
workspace-bridge service install
workspace-bridge service status
```

### Embedded Node and paired remote Nodes

Instead of a separate Node service on the Bridge host, the Bridge can run
that machine's Node in its own process:

```sh
workspace-bridge local-node enable --allow-root "$HOME/Projects"
```

Restart `workspace-bridge serve` afterwards. The local Node appears in the
Manager with transport `local` and needs no URL or token.

A Node on another machine can dial the Bridge instead of listening for it.
The Bridge's Node gateway (TLS, default port 8767) binds `127.0.0.1` unless
`WB_NODE_GATEWAY_HOST` names another address, normally the Bridge's VPN
(for example Tailscale) IP. Set `WB_NODE_GATEWAY_URL` when Nodes should use a
hostname, such as `wss://bridge.example.ts.net:8767/v1/node-gateway`. Then
issue a single-use pairing string on the Bridge host (or with
`POST /api/nodes/pairings` in the Manager API) and redeem it on the Node:

```sh
# Bridge host: valid for 10 minutes, shown once
workspace-bridge pairing create --name laptop
# Node host, after node init with its allowed roots:
NODE_STATE="$HOME/.local/state/workspace-bridge-node"
workspace-bridge node --state "$NODE_STATE" pair 'wbpair1:...'
workspace-bridge node --state "$NODE_STATE" service restart
```

The pairing string carries the gateway certificate fingerprint, so the first
connection is already pinned. A paired Node opens no inbound port and its
Node token is no longer used. It reconnects on its own after network loss
without affecting runs. To give it a new key, issue
`pairing create --name laptop --replace` and pair again; runs started under
the old key become interrupted. `doctor` cannot see paired connections and
reports them as unknown; check them in the Manager.

## 5. Path B — persistent deployment (container Bridge, native Node)

Run the Bridge control plane in Compose and keep the Node, with its agent
plugins, native on each data-plane host so absolute workspace paths match.
Do not add the Node or its agents to Compose. See [Docker](DOCKER.md) for the
full container reference; this section is the minimal supported flow.

```sh
cd /path/to/workspace-bridge
python3 scripts/configure_docker.py --mcp-port 8875 --admin-port 8766
docker compose config --quiet
docker compose up -d --build
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

Fresh state initializes only into an empty directory. A half-deleted state
directory fails closed; restore or wipe it fully.

Initialize the native Node on its host. Host-only use keeps the loopback
default; Docker Desktop reachability needs an explicit non-loopback listen
host plus a host firewall review:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects" --port 8770
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
```

For Docker Desktop, run the `init` above with `--host 0.0.0.0` instead and
register the Node in the Manager as `http://host.docker.internal:8770`
(using your configured port). A Node bound only to `127.0.0.1` is not
assumed reachable from the Bridge container. Non-loopback binding exposes
the authenticated Node on host interfaces: use a host firewall or private
network and never put the Node behind the MCP tunnel.

Save the Node token:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" show-token
```

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
adapter add ...` instead. `--projects-root` defaults to the Node's only
allowed root and must stay inside the allowed roots. Plugin settings are
passed with `--set`, for example `--set binary=/opt/homebrew/bin/claude` or
`--set setting_sources=user`; `plugin list` shows each plugin's settings.
Pi needs the npm package `workspace-bridge-pi-host-adapter` installed so its
`workspace-bridge-pi-adapter` command is on the Node's `PATH`. The Manager
can create the same adapters through `POST /api/adapters` with a `plugin`
field. The Node starts a plugin on first use, restarts it with backoff if it
exits, and writes its stderr to `plugins/<adapter-id>/plugin.log` in the
Node state folder. The Bridge discovers the adapter on its next refresh.

### Platform notes

Agent plugins run as child processes of the Node, as the Node's OS user,
and are covered by the Node service (section 4). On macOS, privacy controls
(Files & Folders, external-storage access) can still block a workspace path:
grant only the required access to the actual executable or interpreter when
prompted. Full Disk Access is not mandatory when a narrower permission
suffices. On Linux, check the Node service journal with
`journalctl -u <unit-name>`; each plugin's own stderr is in its
`plugin.log`.

Root mode is advanced only and intentional: a root-owned Node state managed
as root runs the Node, and therefore every agent plugin it starts, as root
with full filesystem authority. A root invocation against a user-owned state
is rejected, never converted. See [Operations](OPERATIONS.md).

## 7. Register Node, workspaces, adapters, routes

Sign in to the Manager with the admin account. Local API clients must POST
`{"username":"admin","password":"<password>"}` as JSON to `/api/login`
and keep the HttpOnly session cookie. A temporary-password session can only
read `/api/account`, change the password through `/api/account/password`
(with JSON `current_password` and `new_password`), or sign out. Bearer tokens
are not accepted by the Manager. Order matters:

1. **Node:** add the Node URL with its token. Native evaluation uses the
   loopback Node URL; container Bridge uses the host-reachable Node URL from
   section 5. Embedded and paired Nodes (section 4) register themselves and
   skip this step.
2. **Workspaces:** add one mapping per canonical Node-local root, then enable
   it and set write scope (`handoff` default; `none` denies all writes
   including handoff publication; `workspace` allows permitted source text).
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

4. Run the tunnel with the official client (the bundled helper only prompts
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

For container Bridge, run Doctor inside the Bridge container so it reads the
same `/state` and container network context:

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor
docker exec workspace-bridge workspace-bridge --state /state doctor --json
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

Expected: overall `pass`, and every intended workspace/adapter route listed
as ready with its default model. Offline mode skips adapter and network
calls; readiness that depends on live freshness is then reported unknown and
never ready. The Manager uses the same server-side report: only a ready
exact route enables a prepared-handoff start. If diagnostics cannot refresh,
the Manager marks readiness unavailable and disables starts while leaving
other data visible.

Also validate in a real client conversation with a nonsensitive sample
project: discovery lists only enabled mappings, wrong-workspace IDs fail,
disabled mappings disappear, pause denies requests, and rotation revokes the
old credential.

## 10. Updates

There is no automatic or remote updater. Update each component locally on its
host, then explicitly restart its service. A package upgrade never restarts
anything by itself. The Manager System / Versions view shows component
versions and compatibility as information only; it performs no installs.

Bridge and Manager (Docker):

```sh
git pull
docker compose up -d --build
```

Node and its Claude Code, Codex and OpenCode plugins (shared Python package),
plus the Pi plugin if you use it:

```sh
uv tool upgrade workspace-bridge
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge --version
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
```

Restarting the Node restarts its plugins. These steps do not update the
native Codex, Claude Code, or Pi CLIs. Restarting interrupts any run in
flight on that Node, so check for active runs first. See
[Operations](OPERATIONS.md#manual-upgrades) for details.

## 11. Uninstall

Stop and remove services before deleting state. Service removal preserves
state, tokens, bindings, and logs; deleting state is the destructive step.

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service uninstall
docker compose down
```

Then, only if you intend to lose local data, remove the state directories
and the tunnel environment. Back up stopped private state and workspace
handoff folders together first, preserving permissions and SQLite files.
Removing containers or units never deletes host bind directories by itself.

## 12. Troubleshooting first aid

- Doctor reports `action_required` on a fresh Bridge for gateway credential
  and gateway enabled: create the shared credential in the Manager.
- `service start` refuses an unmanaged unit: reinstall the managed unit
  (`uninstall` then `install`) so the state-local manifest matches again.
- Container Bridge cannot reach the Node: the Node URL must be reachable
  from inside the container (`host.docker.internal` on Docker Desktop, a
  real host address on Linux Engine). Loopback inside the container is the
  container itself.
- An adapter fails its test: run `workspace-bridge node adapter test
  <adapter-id>` on the Node host and read the `plugin.log` it names.
- Model saves fail: read the live catalog first; stale selectors are
  rejected at save time.
- No live service restart, publish, or credential rotation is performed by
  reading this guide. See [Operations](OPERATIONS.md) for recovery detail.
