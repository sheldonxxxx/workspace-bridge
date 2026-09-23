# Docker Compose — v0.8.4

Runs **Workspace Bridge** with an internal tunnel sidecar on the shared Compose
network. The Pi agent runs natively on the host and is reached through a
private client-only Pi host adapter URL. The image is built locally from
this source; no public Workspace Bridge image has been published. The Pi agent
itself is **not** part of this stack: it runs natively on the host and is managed by you.

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

**Validation boundary:** `docker compose config` validated, the bridge
(`workspace-bridge:0.8.4`) image built, and a disposable bridge container smoke
passed here: the bridge served MCP (401 unauthenticated) and the loopback
manager, settings/health resolved, and a new workspace defaulted to
disabled/handoff/agent-disabled. The full Compose stack with the tunnel sidecar
and a live host Pi agent was **not** run in this environment.
`scripts/test_docker.py` performs the real Compose build/start/health
checks on a Docker-equipped host; do not confuse a successful image build or YAML
parse with a successful end-to-end run.

## Quick start

Extract the package **outside** the projects you will map. Use your normal host
user, not `sudo`; the helper detects UID/GID and the runtime refuses root.
The projects directory must already exist and should contain only intended project
folders, for example `$HOME/Projects/project-a` and `$HOME/Projects/project-b`.

```sh
cd /path/to/workspace-bridge
python3 scripts/configure_docker.py \
  --projects-dir "$HOME/Projects" \
  --mcp-port 8875 \
  --admin-port 8766

docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec bridge workspace-bridge --state /state show-admin-token
```

The helper creates `.env` (0600) and a private state directory (0700), defaulting
to `$HOME/.local/state/workspace-bridge-docker`. Set `--state-dir /absolute/path`
for a different **separate** state location. It does not overwrite an existing
`.env`, change project files, run Docker or enable mappings. Later, edit `.env`
deliberately; rerunning the helper is not required.

Open **http://127.0.0.1:8766/** and enter the admin token locally. Register each
actual host project path, create one bridge token, then enable only intended
mappings. The project parent is mounted at the **same absolute path inside the
container**, so copied handoff paths also work in the host Pi agent. Register a project
child, not the parent itself. Do not use `/state` or invented `/workspace` aliases.

The first startup initializes **only fresh** Docker state. It refuses incomplete
nonempty state, mismatched parents/internal ports or unsafe ownership instead of
resetting credentials. Container recreation retains state, mappings, tokens,
handoffs and selected write scopes. A new mapping is still disabled and handoff-only.

### Without host Python

```sh
cp .env.example .env
id -u
id -g
```

Edit `.env`: replace `WB_UID`, `WB_GID`, `WB_PROJECTS_DIR`, `WB_STATE_DIR` with your
actual non-root numeric identity and canonical absolute POSIX paths. Paths can be
single-quoted in `.env`. Do not put tokens/API keys in this file. The state directory
must be outside the project parent and owned by the configured UID.

```sh
mkdir -p "$HOME/.local/state/workspace-bridge-docker"
chmod 700 "$HOME/.local/state/workspace-bridge-docker"
chmod 600 .env
# WB_STATE_DIR must match the directory just prepared.
docker compose config --quiet
docker compose up -d --build
```

The bind sources must exist; Compose does not silently create them as root-owned
folders (`create_host_path: false`). On Linux, use the same UID/GID as the normal
Pi agent user so that mode-0600 handoff files are readable on the host. On Docker
Desktop, verify bind ownership and read/write behavior with a disposable project;
file-sharing implementations can differ. Do not work
around failures with root, privileged mode or world-writable state.

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

## Pi runtime (optional)

Agent execution is off until you enable it per workspace. The private Pi host
adapter stays **locked** until `WB_RUNTIME_TOKEN` is set: with an empty token every
operational endpoint returns 401 and the bridge fails closed. `/health` remains
readable and reports `locked: true` without revealing any secret. To connect the
host Pi agent:

1. Start the Pi host adapter natively on the host. On macOS, the
   [LaunchAgent setup](../runtime/pi-host-adapter/README.md#start-after-login-with-launchd)
   starts it after user login and restarts it if it exits. Keep it bound to
   loopback.
2. In `.env` set `WB_PI_RUNTIME_URL` plus a random `WB_RUNTIME_TOKEN` shared
   between the bridge and the adapter. The token is **required** to unlock agent
   operations; an empty value never means allow.
   - Docker Desktop or OrbStack: `WB_PI_RUNTIME_URL=http://host.docker.internal:<port>`.
   - Linux Docker Engine: set an explicit reachable host URL. Do **not** assume
     `localhost` inside a container means the host.
3. `docker compose up -d` (recreates the bridge). The manager's **Pi runtime**
   panel shows health/version and the locked state; `docker compose logs bridge`
   shows adapter reachability without printing credentials.

A transient adapter-unavailable result during startup reconciliation never orphans
an active run. Reconciliation retries until the runtime is reachable and only
orphans a run after a positive missing-session result.

The Pi adapter runs natively on the host with **no published port** and no Compose
service. The bridge never receives provider credentials. Never put the Pi agent
in this Compose file.

## Operational container logs

The primary operational view is the container logs (Docker `json-file` logging
with `max-size: 10m` / `max-file: 3` rotation is already configured in
`compose.yaml`):

```sh
docker compose logs -f bridge
docker compose logs --tail=200 bridge
```

The bridge and the native Pi adapter emit one-line JSON records with stable
event names and bounded scalar fields only. The third process, the OpenAI
tunnel-client sidecar (`mcp-tunnel`), is configured for JSON logs too
(`LOG_FORMAT: json` in tracked `compose.yaml`). Representative Bridge SAFE fields
(placeholders, not real IDs/secrets):

```json
{"component":"bridge","event":"bridge_ready","level":"INFO","enabled_count":2,"runtime_configured":true,"workspace_count":3}
{"component":"bridge","event":"run_created","level":"INFO","job_id":"job_…","model":"provider/model","run_id":"run_…","session_id":"ses_…","workspace_id":"ws_…"}
{"component":"bridge","event":"permission_asked","level":"INFO","action":"external_directory","request_id":"per_…","run_id":"run_…","source":"event","session_id":"ses_…"}
{"component":"bridge","event":"run_state","level":"INFO","run_id":"run_…","state":"completed","reason":"read_reconcile"}
```

`WB_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`, default `INFO`) controls the
bridge. An invalid value fails fast at bridge startup (`BridgeError`). The native
Pi adapter supports the same four levels with the same INFO default via its own
environment (the LaunchAgent template sets `WB_LOG_LEVEL=INFO`); Compose config does
not automatically configure the LaunchAgent — set `WB_LOG_LEVEL` for each deployment
environment. An invalid nonblank adapter value fails adapter startup safely. Bridge `serve`
controlled startup/config failures emit a sanitized ERROR `process_error` and
exit nonzero; Pi adapter invalid `WB_LOG_LEVEL` emits a sanitized ERROR
`adapter_config_error` and exits nonzero; Pi adapter fatal HTTP server/listen
errors emit a sanitized ERROR `process_error` and exit nonzero; arbitrary
OS/runtime crashes are not intercepted. Uvicorn access logs
stay disabled — routine lifecycle is covered by the records above, not by
request logs.

Three processes, three independent level controls:

| Process | Setting | Valid levels | Default |
|---|---|---|---|
| Docker Bridge | `WB_LOG_LEVEL` (Compose) | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |
| Native Pi adapter | `WB_LOG_LEVEL` (LaunchAgent) | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |
| Tunnel sidecar | `WB_TUNNEL_LOG_LEVEL` → tunnel `LOG_LEVEL` (Compose) | `debug`/`info`/`warn` (tunnel vocabulary; no `ERROR` threshold) | `info` |

The tunnel vocabulary is `debug|info|warn` only — do not configure an `ERROR`
threshold that tunnel-client does not support. Compose cannot validate enum
values itself; an invalid `WB_TUNNEL_LOG_LEVEL` is left for tunnel-client to
reject with its own config error. Raw HTTP tunnel logging (`LOG_HTTP_RAW_UNSAFE`)
must remain disabled: it may expose sensitive headers/bodies.

Shared level semantics (Bridge and Pi adapter):

| Level | Meaning | Production guidance |
|---|---|---|
| `DEBUG` | High-frequency internals, polls/resyncs/probes, normal SDK tool-event tracing. | Temporary troubleshooting only. |
| `INFO` | Healthy/expected lifecycle transitions (ready, run/session created, dispatch started, permission transitions, successful completion/recovery). | Normal production level. |
| `WARNING` | Recoverable degradation, policy/input rejection, stream/runtime unavailability after grace, dispatch refusal, orphaning, SDK dispatch stall/journal anomaly. | Alert candidates. |
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

The bridge image builds third-party Python wheels in a dependency-only layer
(dependencies extracted from `pyproject.toml` with stdlib `tomllib`) before any
Workspace Bridge source is copied, so source/static/test edits reuse the cached
dependency layer. The builder uses a BuildKit pip cache mount; the runtime stage
still installs offline (`--no-index`) from prebuilt wheels with no cache. The image
stays minimal: explicit `COPY` allowlists, non-root runtime, read-only rootfs, and
health checks.

Discord notifications are optional: set `WB_DISCORD_WEBHOOK_URL` in `.env` (kept out
of the repo and never returned by any API). Waiting and completion states are
notified with safe metadata only. `docker compose config` interpolates these values;
review `.env` permissions (0600) as you would other secrets.

## What is protected — and what is not

The runtime has a read-only container root filesystem, non-root UID/GID, dropped
Linux capabilities, no-new-privileges, PID/memory/CPU bounds and a bounded
noexec/nosuid/nodev `/tmp`. There is no Docker socket, host networking, privileged
mode, host-home mount or automatic credential mount. The build context is an
allowlist, so local `.env`, projects and state do not enter the image.

**The project-parent bind itself is writable.** This permits handoff creation and
later explicitly enabled workspace writes without remounting. The server still
enforces `none`/`handoff`/`workspace`, current hashes, enabled mappings and exclusions.
A read-only container root does not make its writable bind mounts read-only.
A compromised service/decoder process could access everything mounted under that
parent, including disabled mappings; API policy is not an OS sandbox. Mount only
intended projects, not your home or entire disk. Network egress is not disabled.
Do not attach untrusted containers to this service's Compose network.

Text/image reading, metadata stripping, preview limits and image-secret caveats
are unchanged. Write/edit remain text-only. No snapshot-review engine is restored.

### Optional OS-level read-only source mount

For stronger protection, make the parent bind read-only and overlay each selected
project's handoff folder read-write. First precreate the real handoff directory
as your user. Example for the actual child `project-a`:

```sh
mkdir -p "$HOME/Projects/project-a/.workspace-handoff"
chmod 700 "$HOME/Projects/project-a/.workspace-handoff"
```

Place this in local, untracked `compose.override.yaml` (replace `project-a`):

```yaml
services:
  bridge:
    volumes:
      - type: bind
        source: ${WB_PROJECTS_DIR}
        target: ${WB_PROJECTS_DIR}
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ${WB_PROJECTS_DIR}/project-a/.workspace-handoff
        target: ${WB_PROJECTS_DIR}/project-a/.workspace-handoff
        read_only: false
        bind:
          create_host_path: false
```

Compose merges mounts by target, preserving the private state bind. Add one explicit
handoff overlay per project, then inspect `docker compose config` and recreate.
Validate on a disposable project: the bridge intentionally refuses cross-device
traversal. Separate bind mounts must present the same filesystem device to the
bridge; some Desktop/filesystem configurations may not, and are not validated here.
Do not disable the device check to make this work. In that case retain application
policy plus a suitably constrained OS identity, or use a tested mount arrangement.
Source writes stay OS-denied with this overlay even when the UI says `workspace`;
remounting for broader writes requires a separate deliberate local action.

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

```sh
# Uses newly created temporary projects/state, not your .env or real mappings:
python3 scripts/test_docker.py
```

This builds/starts the image, checks non-root/read-only-root settings, loopback
published ports, health, two workspaces, native PNG content, actual host handoff
paths, protected writes, stale hashes, hostile Origin rejection and retained
credentials/mappings after recreation. It tears down only its own temporary Compose
project. It requires Docker and fails rather than claiming success when absent.
The CI workflow runs the same script. Neither checks actual ChatGPT pixel recognition,
Pi execution or tunnel authentication; those remain separate live checks.

## Primary references

- https://docs.docker.com/reference/compose-file/services/
- https://docs.docker.com/engine/network/port-publishing/
- https://docs.docker.com/engine/storage/bind-mounts/
- https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- https://docs.docker.com/reference/compose-file/merge/
