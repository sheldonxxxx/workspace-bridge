# Setup

Single canonical human installation and configuration guide for Workspace
Bridge `0.1.2`. Follow it top to bottom. For container detail see
[Docker](DOCKER.md); for daily operation see [Operations](OPERATIONS.md);
for Pi/Codex specifics see [Runtimes](RUNTIMES.md).

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
Authoritative Node (native, per host) -- files, Git, handoffs, adapter secrets
  |
Pi and/or Codex adapter instances (native, per host)
```

The Bridge never inspects workspace files directly; the Node owns the data
plane under its host `allowed_roots` ceiling. Each adapter state owns exactly
one Pi or Codex instance. Keep package installs, state, and tunnel profiles
outside mapped projects.

## 1. Prerequisites

- macOS or Linux (native Windows is unsupported; use WSL2).
- Python 3.11+ and `uv` are always needed for Workspace Bridge itself.
  Node 20+ with npm is needed only for the Pi adapter (and for
  contributor/web work), not for a Codex-only deployment. Docker
  Engine/Desktop with Compose v2 is needed only if you run the Bridge in a
  container.
- Decide: a projects parent that already exists (for example
  `$HOME/Projects`), a Bridge state directory, a Node state directory, one
  adapter state directory per instance, and distinct host ports in
  1024–65535 for MCP and Manager.
- Default paths used below (generic examples; substitute your own):
  Bridge `$HOME/.local/state/workspace-bridge`, Node
  `$HOME/.local/state/workspace-bridge-node`, Pi adapter
  `$HOME/.local/state/workspace-bridge-adapter-pi`, Codex adapter
  `$HOME/.local/state/workspace-bridge-adapter-codex`.

Do not invent repository URLs or support channels. The image is built locally
from this source; no public Workspace Bridge image is published.

## 2. Install packages

Install the Python product persistently (provides `workspace-bridge`, the
nested `node` and `adapter` surfaces, and the `workspace-bridge-codex-adapter`
executable):

```sh
uv tool install workspace-bridge
workspace-bridge --version
```

Install the Pi adapter package on each host that will run Pi:

```sh
npm install -g workspace-bridge-pi-host-adapter
```

The global `--state` option selects a state directory and comes before the
subcommand:

```sh
workspace-bridge --state "$HOME/.local/state/workspace-bridge" doctor --offline
```

## 3. Credentials you will handle

The Manager uses an admin password; Node, adapter, and MCP access use tokens.
Token creation, storage, and local reveal are distinct steps: reveal commands only read back a stored secret, they do
not create it. Treat initial creation output as sensitive and local, and
never paste tokens into chat, project files, or handoffs.

| Token | Created / stored | How to reveal / enter locally |
|---|---|---|
| Manager admin account | Username `admin`; temporary password `admin`; salted scrypt hash in private `admin-account.json` | Sign in locally and change the password on first login; recover with `workspace-bridge reset-admin-password` |
| Node token | Created during `workspace-bridge node --state <node-state> init`, stored privately in Node state (`node-token`) | Reveal locally with `workspace-bridge node --state <node-state> show-token`; enter in the Manager Node form (or `POST /api/nodes`); stored in private Bridge SQLite (mode `0600`) |
| Adapter runtime token | Created during `workspace-bridge adapter --state <adapter-state> init`, printed by `init` and stored privately in adapter state (`runtime-token`) | May be revealed locally again later with `workspace-bridge adapter --state <adapter-state> show-token`; enter in the Manager adapter form for the owning Node; stored in private Node SQLite (mode `0600`) |
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
section 6. Keep this terminal attached; stopping it stops the Bridge unless
you install the Bridge as a persistent service per your OS policy (see
[Operations](OPERATIONS.md)).

## 5. Path B — persistent deployment (container Bridge, native Node and adapters)

Run the Bridge control plane in Compose and keep the Node plus Pi/Codex
adapters native on each data-plane host so absolute workspace paths match.
Do not add the Node or adapters to Compose. See [Docker](DOCKER.md) for the
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

The supported path is package installation followed by the runtime-neutral
`workspace-bridge adapter` lifecycle. Each adapter state owns exactly one Pi
or Codex instance; use a separate `--state` directory per instance. Service
artifacts never contain the runtime token or the projects root.

```sh
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" init \
  --runtime pi --projects-root "$HOME/Projects" --port 8780
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" init \
  --runtime codex --projects-root "$HOME/Projects" --port 8772
```

Save each printed token immediately: it lives only in that state's private
storage (mode `0600`). Reprint locally only with `show-token` on the same
host; never paste it into chat.

Install the persistent service with the same plain verbs as the Node:

```sh
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service status
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" service install
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" service status
```

Expected: `service status` reports the per-instance unit with installed state
plus bounded descriptor health. `GET /health` needs no token; every `/v1/*`
endpoint requires the runtime token header. The Codex instance state is
created under that instance's private runtime area on first start.

Platform notes:

- macOS: `service install` creates a per-user LaunchAgent under
  `~/Library/LaunchAgents/` (starts after user login, restarts on unexpected
  exit). Privacy controls (Files & Folders, external-storage access) can
  still block a workspace path: grant only the required access to the actual
  executable or interpreter when prompted. Full Disk Access is not mandatory
  when a narrower permission suffices.
- Linux: the same command installs a system unit under
  `/etc/systemd/system/` that starts at boot and survives logout, while
  `User=`/`Group=` keep the process non-root as the installing user. Run the
  command without leading `sudo`; the backend requests `sudo` only for the
  narrow systemd administration steps. For normal troubleshooting, check the
  journal manually with `journalctl -u <unit-name>`; routine service status
  does not read journal text. The sanitized support bundle collector is the
  only programmatic reader: it invokes a fixed
  `journalctl -u <validated exact unit> --no-pager --output=cat -n 200`
  with bounded line/byte limits and strict sanitization (see
  [Operations](OPERATIONS.md)). Host journald retention policy is untouched.

Root mode is advanced only and intentional, per component: a root-owned Node
state managed as root runs the Node as root with full Node filesystem
authority, while a root-owned adapter state managed as root runs that
adapter and its native agent as root. Running the Node as root does not by
itself make separately non-root adapter services root. A root invocation
against a user-owned state is rejected, never converted. See
[Operations](OPERATIONS.md).

## 7. Register Node, workspaces, adapters, routes

Sign in to the Manager with the admin account. Local API clients must POST
`{"username":"admin","password":"<password>"}` as JSON to `/api/login`
and keep the HttpOnly session cookie. A temporary-password session can only
read `/api/account`, change the password through `/api/account/password`
(with JSON `current_password` and `new_password`), or sign out. Bearer tokens
are not accepted by the Manager. Order matters:

1. **Node:** add the Node URL with its token. Native evaluation uses the
   loopback Node URL; container Bridge uses the host-reachable Node URL from
   section 5.
2. **Workspaces:** add one mapping per canonical Node-local root, then enable
   it and set write scope (`handoff` default; `none` denies all writes
   including handoff publication; `workspace` allows permitted source text).
3. **Adapter instances:** on the owning Node, add each destination with the
   name, runtime type (`pi` or `codex`), the base URL as seen by the Node
   host (loopback such as `http://127.0.0.1:8780` is correct there), and its
   one-time runtime token.
4. **Model policy** per adapter: list the live catalog first, then save the
   enabled selectors plus default. Every selector must currently exist or the
   save is rejected. With no policy the adapter is unrestricted by Bridge
   governance and the runtime chooses its own default.
5. **Routes** per workspace: enable the exact `(workspace_id, adapter_id)`
   pair and set its security binding. Pi uses a Bridge profile; Codex uses
   either a Bridge profile or its native configuration source. Discovery and
   binding use the exact workspace context.

Endpoint and token edits take effect on the next request without restarting
the Bridge. The Node `allowed_roots` ceiling is configured on the Node host
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

There is no automatic or remote updater. Update each host locally, then
explicitly restart the affected persistent services. A package upgrade never
restarts anything by itself.

```sh
uv tool upgrade workspace-bridge
workspace-bridge --version
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service restart
```

Update the Pi package with npm on its host, then restart that adapter
service. The Manager System / Versions view shows component versions and
compatibility as information only; it performs no installs.

## 11. Uninstall

Stop and remove services before deleting state. Service removal preserves
state, tokens, bindings, and logs; deleting state is the destructive step.

```sh
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-codex" service uninstall
workspace-bridge adapter --state "$HOME/.local/state/workspace-bridge-adapter-pi" service uninstall
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
- Adapter base URLs are host-relative as seen by the Node, not the
  container: loopback there is correct while the Node URL itself must be
  container-reachable.
- Model saves fail: read the live catalog first; stale selectors are
  rejected at save time.
- No live service restart, publish, or credential rotation is performed by
  reading this guide. See [Operations](OPERATIONS.md) for recovery detail.
