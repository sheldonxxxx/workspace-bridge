# End-to-end setup — new environment (greenfield)

This is the ordered runbook for bringing up a fresh deployment from this
source checkout. No migration or backup-restore is covered here; every state
store starts empty. Follow the steps in order — each one depends on the
previous.

Topology (local host + Docker):

```text
Docker:       workspace-bridge Bridge + mcp-tunnel sidecar
Host native:  workspace-bridge-node service + native Pi/Codex adapters
              (macOS launchd LaunchAgent or Linux systemd system unit)
```

References: [Docker](DOCKER.md) for container detail,
[Operations](OPERATIONS.md) for Node service management and state rules,
[Pi adapter](../runtime/pi-host-adapter/README.md) for Pi specifics,
[Tunnel](TUNNEL_SETUP.md) for the ChatGPT side.

## 0. Prerequisites

- Docker Engine/Desktop with Compose v2, host Python 3, `uv`, Node/npm.
- Decide: `<projects-parent>` (e.g. `/Volumes/data2`), host ports
  (`WB_MCP_PORT`, `WB_ADMIN_PORT`, distinct, 1024–65535), Node port (default
  `8770`), Pi port (`8780`), Codex port (`8772`).
- Generate two secrets now (never commit them, never pass as CLI args):
  a Pi `WB_RUNTIME_TOKEN` and a Codex `WB_RUNTIME_TOKEN`
  (e.g. `python3 -c "import secrets;print(secrets.token_hex(32))"` each).

## 1. Bridge control plane (Docker)

```sh
python3 scripts/configure_docker.py --mcp-port 8875 --admin-port 8766
docker compose config --quiet
docker compose up -d --build
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

Expected: fresh state auto-initializes on first start; offline doctor shows
only `core.gateway_credential_configured` / `core.gateway_enabled` as
`action_required`. Save the admin token for step 5:

```sh
docker compose exec bridge workspace-bridge --state /state show-admin-token
```

## 2. Node data plane (native, per host)

The Node owns workspace files and adapter secrets. Keep it native — never add
it to Compose — so absolute workspace paths match the adapters.

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "<projects-parent>" --host 0.0.0.0 --port 8770
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
```

Expected: `service status` reports `healthy` with `available: 1` roots.
The same plain nested `service install|status|start|stop|restart|uninstall`
verbs work on macOS (launchd) and Linux (system unit); installation remains
`uv tool install workspace-bridge`. On Linux, run `service install` without
leading sudo and inspect the journal manually with
`journalctl -u workspace-bridge-node.service` when needed. The default is a
non-root Node; an intentional root mode (root-owned state, root-controlled
executable) is documented under Service persistence in OPERATIONS.md.
Loopback-only (`127.0.0.1`) Nodes are not reachable from the Bridge
container; use `0.0.0.0` with a host firewall review, or
`http://host.docker.internal:<port>` as the Bridge-side URL (see DOCKER.md).
Save the Node token:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" show-token
```

## 3. Runtime adapters (native, per host)

### 3a. Pi adapter

```sh
mkdir -p "$HOME/Library/Application Support/workspace-bridge/pi-host-adapter"
rsync -a --exclude '/test/' --exclude '/launchd/' --exclude '/README.md' \
  runtime/pi-host-adapter/ \
  "$HOME/Library/Application Support/workspace-bridge/pi-host-adapter/"
```

Copy `runtime/pi-host-adapter/launchd/com.workspace-bridge.pi-host-adapter.plist`
to `~/Library/LaunchAgents/`, replacing every placeholder (home paths,
`<projects-parent>` as `WB_PI_PROJECTS_DIR`, Pi port, Pi `WB_RUNTIME_TOKEN`).
`chmod 600` the plist, then:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.workspace-bridge.pi-host-adapter.plist"
curl -s http://127.0.0.1:8780/health
```

Expected: `{"ok":true,...,"pi_usable":true,"protocol":1}`. `/health` needs no
token; every other endpoint takes `X-Runtime-Token:`.

### 3b. Codex adapter

Build the wheel from this checkout and install it into a dedicated venv:

```sh
uv build --wheel --out-dir /tmp/wb-wheels .
/usr/bin/python3 -m venv "$HOME/Library/Application Support/workspace-bridge/codex-host-adapter/venv"
uv pip install --python "$HOME/Library/Application Support/workspace-bridge/codex-host-adapter/venv/bin/python" \
  --no-deps --reinstall /tmp/wb-wheels/workspace_bridge-*.whl
```

Create `~/Library/LaunchAgents/com.workspace-bridge.codex-host-adapter.plist`
with `ProgramArguments` pointing at
`.../codex-host-adapter/venv/bin/workspace-bridge-codex-adapter` and
environment `WB_CODEX_ADAPTER_STATE` (private dir, mode 0700),
`WB_CODEX_PROJECTS_ROOT=<projects-parent>`,
`WB_CODEX_ADAPTER_PORT=8772`, `WB_RUNTIME_TOKEN=<codex-token>`.
`chmod 600`, bootstrap it, and confirm it serves (authenticated endpoints
return 401 without the token — that is the healthy signal):

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.workspace-bridge.codex-host-adapter.plist"
curl -s http://127.0.0.1:8772/health   # expect {"error":"Unauthorized",...}
```

The Codex SQLite state file is created automatically on first start.

The Codex LaunchAgent must set `RunAtLoad=true` and `KeepAlive=true`.
The Codex adapter and its owned `codex app-server --stdio` form one
supervised failure domain: unexpected native app-server loss intentionally
terminates the host adapter non-zero so launchd restarts the whole unit.
Without supervisor restart the HTTP adapter would stay alive but
permanently degraded. Active runs are marked `interrupted` on restart and
are never replayed; idle persisted conversations remain resumable.

## 4. Register Node, workspaces, adapters (Manager or admin API)

Open `http://127.0.0.1:<admin-port>/` with the admin token, or use the API
(`Authorization: Bearer <admin-token>`). Order matters:

1. **Node**: add `http://host.docker.internal:8770` with the Node token.
2. **Workspaces**: one mapping per canonical Node-local root (no invented
   aliases), then enable it and set write scope (`handoff` default).
   API: `POST /api/workspaces {name, root, node_id}` →
   `POST /api/workspaces/{id} {operation: enable|set_write_scope}`.
   (`set_agent_enabled` remains accepted for compatibility but is inert:
   execution is gated by the exact workspace route, not a workspace switch.)
3. **Adapter instances** (Bridge-side `base_url` is resolved by the Node host,
   so loopback works): Pi → `http://127.0.0.1:8780`, Codex →
   `http://127.0.0.1:8772`, each with its `WB_RUNTIME_TOKEN`.
   API: `POST /api/nodes/{node_id}/adapters {name, runtime_type, base_url, token}`.
4. **Model policy** per adapter — first list what exists, then save:
   `GET /api/adapters/{id}/models?workspace_id={ws}` →
   `POST /api/adapters/{id}/model-policy {enabled[], default, reasoning_defaults{}}`.
   Every selector must currently exist or the save is rejected.
5. **Routes** per workspace: `POST /api/workspaces/{ws}/routes/{adapter}
   {enabled, profile_id, security_source, is_default}`. Pi profiles are
   built in; Codex custom profiles (e.g. `coding`) live in Codex adapter
   state — if the profile is missing, recreate it first via
   `POST /api/adapters/{codex}/profiles {id, config}`.

## 5. Gateway credential and tunnel sidecar

```sh
# POST /api/bridge {"operation":"rotate_token"} — token is shown ONCE.
```

Put it in `tunnel.env` as `WORKSPACE_BRIDGE_TOKEN` (mode 0600; the sidecar
reads `X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN` from
`tunnel-client.yaml`), then recreate the sidecar:

```sh
chmod 600 tunnel.env
docker compose up -d mcp-tunnel
```

Rotation also enables the gateway. Never tunnel the manager listener.

## 6. Verify

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor
```

Expected: `Overall: PASS`, every workspace/adapter route listed under
`Runnable routes` as `[ready]` with its default model. `doctor --offline`
skips adapter/network checks; the Manager's route-readiness view is the same
report.

## Gotchas

- Bridge state auto-initializes **only** into an empty directory (plus
  `bootstrap.lock`). A half-deleted state dir fails closed — restore or wipe
  fully.
- Node `service start` refuses an `unmanaged` unit. The
  `launchagent-manifest.json` (macOS) or `systemd-system-manifest.json`
  (Linux) in Node state tracks the managed unit by SHA;
  if state was wiped but the unit kept, restore the manifest (or
  `uninstall` + `install`).
- The adapter-sync helper (`sync_adapters_local.sh`, untracked) requires the
  Bridge up and both LaunchAgent plists present; its final diagnostics check
  only passes once Node + adapters are registered (step 4).
- Adapter `base_url` values are host-relative as seen by the Node, not the
  container — `127.0.0.1` is correct there, while the Node URL itself must be
  `host.docker.internal` from the container.
- Old model selectors and custom profiles are **not** validated until save
  time; always read the live catalog first.
