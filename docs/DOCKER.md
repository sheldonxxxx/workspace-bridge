# Docker Compose — v0.8.4

Runs the **Workspace Bridge** control plane with an internal tunnel sidecar on the
shared Compose network. The authoritative `workspace-bridge-node` service is a
separate private process on each data-plane host; it owns workspace files, Git,
handoffs and Node-side adapter secrets. Native adapter daemons run on those hosts
and are reached through the Node Protocol. The image is built locally from this
source; no public Workspace Bridge image has been published. Nodes and agents are
not part of this Compose stack; run and manage them on their hosts.

## Requirements and scope

Use Docker Engine or Docker Desktop with the current Compose v2 plugin. Docker
Engine 28+ avoids Docker's documented old localhost-publication exposure to peers
on the same L2 network. This configuration is intended for a local Docker daemon
on Linux, macOS or WSL2. A remote Docker daemon mounts **its own host paths**, not
files on your laptop. Native Windows drive-letter paths are not supported by the
bridge; run from WSL2. Docker Desktop must have access to the selected host folder.

The optional configuration helper needs only host Python 3 standard library. A
manual alternative is provided below. All runtime dependencies, including Pillow,
are installed in the image. Builds require access to the Python image registry
and Python package index. The Docker base tag can be pinned to a reviewed digest
with `WB_PYTHON_IMAGE`; this release does not claim a locked/reproducible image.

**Validation boundary:** this checkout did not run a live Docker Compose plus Node
service smoke. `docker compose config` and the in-process Docker contract tests
cover only the Bridge control-plane shape. The existing
`scripts/test_docker.py` and `scripts/validate_container_transport.py` fixtures
still encode the pre-Node local-bind flow, so they are not v3 end-to-end evidence.
Before deployment, exercise the Bridge, the selected Node, and the tunnel together
on a Docker-equipped host; do not treat an image build or YAML parse as a proof of
the authority boundary.

## Quick start

Extract the package outside the Node host roots. Use your normal host user, not
`sudo`; the helper detects UID/GID and the runtime refuses root.

```sh
cd /path/to/workspace-bridge
python3 scripts/configure_docker.py \
  --mcp-port 8875 \
  --admin-port 8766

docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec bridge workspace-bridge --state /state show-admin-token
docker exec workspace-bridge workspace-bridge --state /state doctor
```

Run Doctor inside the Bridge container to inspect the state mounted at `/state`
with the same saved adapter inventory and container network context as the live
service. Use `--json` for the canonical report or `--offline` to skip
adapter/network checks:

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor --json
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

The helper creates `.env` (0600) and a private Bridge state directory (0700), defaulting
to `$HOME/.local/state/workspace-bridge-docker`. Set `--state-dir /absolute/path`
for a different **separate** state location. It does not overwrite an existing
`.env`, change Node files, run Docker or enable mappings. Later, edit `.env`
deliberately; rerunning the helper is not required.

Open **http://127.0.0.1:8766/** and enter the admin token locally. Add each
Node (its endpoint and write-only token), then register workspace roots that are
canonical paths on that Node. Add AdapterInstances from the Node detail and
configure exact execution routes. Node adapter tokens are stored in private Node
SQLite and are never read back by normal APIs. Do not use `/state` or invented
`/workspace` aliases as workspace roots.

The first startup initializes **only fresh** Docker control-plane state. Bridge state
schema v3 is the development contract; non-v3 state reports
`state_schema_incompatible` and is never migrated or altered. Use a separate fresh
state path for this cutover. Runtime adapter secrets and workspace data remain in
the private Node service. Container recreation retains Node records, mappings,
handoffs and selected write scopes. A new mapping is still disabled and
handoff-only.

### Without host Python

```sh
cp .env.example .env
id -u
id -g
```

Edit `.env`: replace `WB_UID`, `WB_GID`, and `WB_STATE_DIR` with your actual
non-root numeric identity and canonical private state path. Paths can be
single-quoted in `.env`. Do not put tokens/API keys in this file. Node state and
workspace roots are configured on the Node host, not mounted into this Bridge
container.

```sh
mkdir -p "$HOME/.local/state/workspace-bridge-docker"
chmod 700 "$HOME/.local/state/workspace-bridge-docker"
chmod 600 .env
# WB_STATE_DIR must match the directory just prepared.
docker compose config --quiet
docker compose up -d --build
```

The state bind source must exist; Compose does not silently create it as a root-owned
folder (`create_host_path: false`). On Linux, use the same UID/GID as the normal
Bridge service user. On Docker Desktop, verify state bind ownership and read/write
behavior with a disposable state directory; file-sharing implementations can differ.
Node roots and handoff files stay on the Node host. Do not work around failures with
root, privileged mode or world-writable state.

## Ports and the single tunnel

Internal container ports are always 8765 (MCP) and 8766 (management). `.env`
controls the **host** ports independently:

```dotenv
WB_MCP_PORT=8875
WB_ADMIN_PORT=8876
```

Both are published explicitly on **127.0.0.1**, never all host interfaces. Choose
unused, distinct ports in 1024–65535. Apply changes with `docker compose up -d`
(container recreation); `docker compose restart` does not apply changed Compose
environment/port definitions. Do not edit `/state/config.json` ports to change
Docker publishing. Native non-Docker installs still use config.json as before.

`WB_ADMIN_ALLOWED_HOSTS` (empty by default) is the sole opt-in remote-admin
path: comma-separated bare hostnames/IPs that widen only the admin listener to
`0.0.0.0` inside the container and allow those `Host` values. MCP is
unaffected. The default Compose publishing stays `127.0.0.1`-only, so LAN
access additionally requires republishing the admin port (e.g.
`"0.0.0.0:${WB_ADMIN_PORT:-8766}:8766"` via local override) plus firewall/TLS
hardening. Prefer SSH forwarding or VPN; never tunnel the manager.

Behind a TLS-terminating nginx, set the env to the external name the browser
uses (e.g. `WB_ADMIN_ALLOWED_HOSTS=admin.example.com`), not the upstream
address. Bare `Host: admin.example.com` / `:443` and `Origin:
https://admin.example.com` (no internal port) are accepted for allowlisted
names; unknown ports such as `:9999` and unlisted names stay 403. Minimal
proxy snippet (keep the manager off the tunnel):

```nginx
server {
    listen 443 ssl;
    server_name admin.example.com;
    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
    }
}
```

Point the sidecar profile at the internal bridge listener:

```yaml
mcp:
  server_urls:
    - channel: main
      url: http://bridge:8765/mcp
```

The bridge allowlist trusts the Compose DNS names `bridge` and
`workspace-bridge` on the internal MCP port (8765) only when started with
`--container`. The management listener stays loopback-only and must never be
tunneled. Preserve tunnel ID, control-plane configuration and both
runtime/discovery `X-Bridge-Token` settings, then recreate the sidecar.
Neither `/api/` nor the management listener belongs in the tunnel. Host-port
publishing stays `127.0.0.1`-only; published host ports (e.g. 8875) are for
local host access, not for sidecar-to-bridge traffic.

Credentials are obtained/generated in the local manager and supplied to the host
tunnel using the existing setup guide. They are not image build arguments or
Compose environment variables. The container's admin token is available with the
explicit `exec ... show-admin-token` command, never printed in startup logs.

## Native Node on macOS

The local Compose topology has two container services — Bridge and the MCP
tunnel — plus host-native processes on macOS:

```text
Docker:       workspace-bridge Bridge + mcp-tunnel
macOS host:   workspace-bridge-node LaunchAgent + native Pi/Codex adapters
              |-- identical absolute workspace paths and host-owned allowed_roots
```

Do not add the Node to `compose.yaml`: keeping it native preserves the same
host filesystem/data-plane boundary and absolute paths used by the local
runtime adapters. Remote Linux/systemd or a Node container is a future/alternate
deployment, not this local-Mac flow.

For Docker Desktop, initialize the Node with an explicit non-loopback listen
host, then use the host alias from Bridge:

```sh
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "/Volumes/data2" --host 0.0.0.0 --port 8770
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge-node --state "$HOME/.local/state/workspace-bridge-node" show-token
```

On macOS, a successful LaunchAgent start does not prove that the native process
can read a workspace. Privacy controls such as Files & Folders and external or
removable-storage access can gate the executable/interpreter or a root under
`/Volumes/data2`. Check `service status` for the bounded allowed-root
availability summary, then verify the selected workspace through Manager and
Bridge. Grant only the required access to the actual executable/interpreter if
prompted; Full Disk Access is not mandatory when a narrower permission is
sufficient. Do not automate these prompts.

Register `http://host.docker.internal:8770` in Manager, replacing `8770` with
the configured Node port. `localhost` inside the Bridge container is the
container itself, and a Node bound only to `127.0.0.1` is not assumed to be
reachable from Docker Desktop. Binding `0.0.0.0` makes the authenticated Node
listen on host interfaces; restrict access with the macOS firewall/private
network and do not expose the Node through the MCP tunnel or a public reverse
proxy. Node token authentication remains mandatory. The LaunchAgent stores no
Node or adapter token in its plist; state/config/token files remain private.

## Adapter instances (optional)

Agent execution is disabled until you enable it for a workspace and exact
WorkspaceRoute. Start the native Pi or Codex daemon on its host and configure its
own listen address, bootstrap token and process lifecycle there. The Bridge
container does not start or publish those daemons.

In the local Manager's **Adapters** area, add one AdapterInstance for each
destination. Save a distinct name, runtime type, endpoint reachable from the
container, and the credential expected by that daemon. Docker Desktop and
OrbStack commonly use `host.docker.internal`; Linux Engine needs a reachable host
address. Do not assume `localhost` inside a container means the host. The
per-instance Bridge connection token is stored as plaintext in private SQLite
during this development phase; the file is mode `0600`, and the UI never reads
the token back. `.env` does not contain `WB_RUNTIME_ADAPTERS` or a Bridge-wide
`WB_RUNTIME_TOKEN`. The same native daemon may still use `WB_RUNTIME_TOKEN` in
its separate host process environment.

The Manager's **Adapters** area shows each instance's health and allows a
sanitized connection test. Configure model policy and profiles per adapter, then
enable exact targets on each workspace. Two Pi instances can be configured
independently. Bridge changes apply without container recreation or service
restart. For daemon setup, see the [Pi LaunchAgent guide](../runtime/pi-host-adapter/README.md#start-after-login-with-launchd)
or the Codex adapter setup instructions.

After restart, Bridge reconciles runs against Runtime Protocol adapter snapshots.
It does not replay prompts or adopt unowned Pi TUI or Codex Desktop/TUI sessions.
Provider credentials stay on the host and never enter the Bridge container.

## Operational container logs

The primary operational view is the container logs (Docker `json-file` logging
with `max-size: 10m` / `max-file: 3` rotation is already configured in
`compose.yaml`):

```sh
docker compose logs -f bridge
docker compose logs --tail=200 bridge
```

The bridge and the native runtime adapters emit one-line JSON records with stable
event names and bounded scalar fields only. The third process, the OpenAI
tunnel-client sidecar (`mcp-tunnel`), is configured for JSON logs too
(`LOG_FORMAT: json` in tracked `compose.yaml`). Representative Bridge SAFE fields
(placeholders, not real IDs/secrets):

```json
{"component":"bridge","event":"bridge_ready","level":"INFO","enabled_count":2,"adapters_configured":2,"workspace_count":3}
{"component":"bridge","event":"boundary_reject","level":"WARNING","reason":"untrusted-origin"}
{"component":"bridge","event":"request_error","level":"ERROR","code":"RuntimeError","source":"mcp","action":"read_file","workspace_id":"ws_…"}
```

`WB_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`, default `INFO`) controls the
bridge. An invalid value fails fast at bridge startup (`BridgeError`). The native
native adapters support the same four levels with the same INFO default via their
own environments (the Pi LaunchAgent template sets `WB_LOG_LEVEL=INFO`); Compose
does not configure host adapters — set `WB_LOG_LEVEL` for each deployment
environment. An invalid nonblank adapter value fails adapter startup safely. Bridge `serve`
controlled startup/config failures emit a sanitized ERROR `process_error` and
exit nonzero; adapter invalid `WB_LOG_LEVEL` emits a sanitized ERROR
`adapter_config_error` and exits nonzero; adapter fatal HTTP server/listen
errors emit a sanitized ERROR `process_error` and exit nonzero; arbitrary
OS/runtime crashes are not intercepted. Uvicorn access logs
stay disabled — routine lifecycle is covered by the records above, not by
request logs.

Three processes, three independent level controls:

| Process | Setting | Valid levels | Default |
|---|---|---|---|
| Docker Bridge | `WB_LOG_LEVEL` (Compose) | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |
| Native host adapters | `WB_LOG_LEVEL` (each process) | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |
| Tunnel sidecar | `WB_TUNNEL_LOG_LEVEL` → tunnel `LOG_LEVEL` (Compose) | `debug`/`info`/`warn` (tunnel vocabulary; no `ERROR` threshold) | `info` |

The tunnel vocabulary is `debug|info|warn` only — do not configure an `ERROR`
threshold that tunnel-client does not support. Compose cannot validate enum
values itself; an invalid `WB_TUNNEL_LOG_LEVEL` is left for tunnel-client to
reject with its own config error. Raw HTTP tunnel logging (`LOG_HTTP_RAW_UNSAFE`)
must remain disabled: it may expose sensitive headers/bodies.

Shared level semantics (Bridge and host adapters):

| Level | Meaning | Production guidance |
|---|---|---|
| `DEBUG` | High-frequency details for a current operation or adapter. | Temporary troubleshooting only. |
| `INFO` | Service readiness and expected lifecycle transitions. | Normal production level. |
| `WARNING` | Recoverable degradation or input rejection needing attention. | Alert candidates. |
| `ERROR` | Unexpected internal/runtime exception or unsafe startup condition. | Alert candidates. |

Retention: Docker bridge AND tunnel-sidecar logs are rotated 10m x3 by Compose
(`max-size: 10m` / `max-file: 3` on both services in tracked `compose.yaml`,
the canonical Compose contract). A local deployment-specific `compose-prod.yaml`
may mirror it — the current local copy has been validated separately — but it is
ignored and not portable because it contains host-specific paths. The native
launchd adapter writes plain host files
via `StandardOutPath`/`StandardErrorPath`; project-managed rotation is NOT currently
provided — rotation of those files is an explicit operator/next-milestone concern.

Logs never contain prompts, message/final-response text, file contents,
absolute paths, permission resources/patterns/metadata, tool arguments,
workspace roots/names, tokens, usernames/passwords, or raw error bodies — only
IDs, state/event names, counts, durations, sanitized codes and boolean health
flags.

Troubleshooting a stale `running` session (the agent says completed but the Bridge
still shows running): check the run's `permission_sync` outcome in
the API versus `permission_resync` log lines (`ok` with `matched` count means a
successful listing; `degraded` with a sanitized `code` means the listing
failed and stays retryable), the completion-probe lines for durable-evidence
decisions, and the startup `reconcile_start` /
`reconcile_result` lines for the reconcile outcome.

## Image layering

The bridge image builds the React manager in a Node build stage and copies its
compiled assets into the Python wheel. It builds third-party Python wheels in a dependency-only layer
(dependencies extracted from `pyproject.toml` with stdlib `tomllib`) before any
Workspace Bridge source is copied, so source/UI/test edits reuse the cached
dependency layer. The builder uses a BuildKit pip cache mount; the runtime stage
still installs offline (`--no-index`) from prebuilt wheels with no cache. The
runtime stage also installs Debian's `git` package without recommended packages
for images that may host the Node data-plane service; the Bridge control plane does
not inspect workspace Git state directly. Agent runtimes and their Git installations
remain separate and host-side. The image
uses explicit `COPY` allowlists, a non-root runtime, a read-only root filesystem,
and health checks.

Notifications are optional: set `WB_DISCORD_WEBHOOK_URL` in `.env` to configure the
current Discord channel adapter (kept out of the repo and never returned by any API).
The Bridge persists semantic events and per-channel delivery status; payloads contain
safe metadata only. `docker compose config` interpolates these values; review `.env`
permissions (0600) as you would other secrets.

## What is protected — and what is not

The runtime has a read-only container root filesystem, non-root UID/GID, dropped
Linux capabilities, no-new-privileges, PID/memory/CPU bounds and a bounded
noexec/nosuid/nodev `/tmp`. There is no Docker socket, host networking, privileged
mode, host-home mount or automatic credential mount. The build context is an
allowlist, so local `.env`, projects and state do not enter the image.

The Bridge container has no workspace data bind. The selected Node permits handoff
creation and later explicitly enabled workspace writes while enforcing
`none`/`handoff`/`workspace`, current hashes, enabled mappings and exclusions. A
compromised Node process remains able to access its configured allowed roots, so
use a dedicated Node identity and mount only intended projects. Network egress is
not disabled. Do not attach untrusted containers to this service's Compose network.

Text/image reading, metadata stripping, preview limits and image-secret caveats
are unchanged. Write/edit remain text-only. No snapshot-review engine is restored.

### Node-side filesystem hardening

Configure the Node service with host-local `allowed_roots` and a dedicated service identity. Bridge cannot edit that ceiling and never falls back to a local bind. Apply any host read-only or handoff-overlay policy on the Node host, then verify it with Node diagnostics.

## Operating commands

```sh
docker compose ps
docker compose logs --tail=100 bridge
docker compose exec bridge workspace-bridge --state /state doctor
docker compose stop
docker compose start
docker compose down
```

`down` removes containers/network but not these **host bind directories**. Do not
delete `WB_STATE_DIR` or handoff directories unless you intend to lose that data.
The health check only checks both listeners. It does not prove the bridge token
is configured, the tunnel is online, ChatGPT receives image pixels, or tests ran.
Docker marks unhealthy containers; health checks alone do not restart them.
`restart: unless-stopped` restarts processes that exit and resumes them with Docker,
but a manual stop remains a stop.

Back up stopped private state and workspace handoff folders together, preserving
permissions and all SQLite files. The Docker source archive includes no image
layers, tokens or user project data.

## Fresh state

The Docker state directory is separate from the native one. Starting with a fresh
database clears registered mappings, handoff records, run history and gateway
authorization. Existing handoff files remain on the host until removed separately.
Register intended mappings and configure the shared bridge token in the local
manager before connecting ChatGPT.

## Validation on your host

The v3 authority boundary requires a live Bridge plus an explicitly configured
Node, with the tunnel and any native adapter daemon checked separately. This
checkout did not run that Docker/Node integration smoke. The older
`scripts/test_docker.py` and `scripts/validate_container_transport.py` scripts
still construct local project fixtures for the pre-Node service and must be
migrated before being used as v3 validation. Use the Manager and Node diagnostics
to verify the selected Node root, adapter, route readiness and handoff path.

## Primary references

- https://docs.docker.com/reference/compose-file/services/
- https://docs.docker.com/engine/network/port-publishing/
- https://docs.docker.com/engine/storage/bind-mounts/
- https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- https://docs.docker.com/reference/compose-file/merge/
