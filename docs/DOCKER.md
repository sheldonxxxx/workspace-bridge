# Docker Compose — v0.8.4

Runs **Workspace Bridge** with an internal tunnel sidecar and a private, client-only
OpenCode SDK adapter on the shared Compose network. The images are built locally from
this source; no public Workspace Bridge image has been published. The OpenCode server
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

**Validation boundary:** `docker compose config` validated, both the bridge
(`workspace-bridge:0.8.4`) and adapter (`workspace-bridge-opencode-adapter:0.1.4`)
images built, and a disposable bridge+adapter container smoke passed here: the bridge
served MCP (401 unauthenticated) and the loopback manager, the adapter reported
`server_configured=false`/locked with **no published port**, settings/health resolved,
and a new workspace defaulted to disabled/handoff/agent-disabled. The full Compose
stack with the tunnel sidecar and a live host OpenCode server was **not** run in this
environment. `scripts/test_docker.py` performs the real Compose build/start/health
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
container**, so copied handoff paths also work in host OpenCode. Register a project
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
OpenCode user so that mode-0600 handoff files are readable on the host. On Docker
Desktop, verify bind ownership and read/write behavior with a disposable project;
file-sharing implementations can differ. Do not work around failures with root,
privileged mode or world-writable state.

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

## OpenCode runtime (optional)

Agent execution is off until you enable it per workspace. The private adapter stays
**locked** until `WB_RUNTIME_TOKEN` is set: with an empty token every operational
endpoint (`/models`, `/sessions`, prompt, permission reply, abort, `/events`) returns
401 and the bridge fails closed. `/health` remains readable and reports
`locked: true` / `token_configured: false` without revealing any secret. To connect
the host OpenCode server:

1. Start the server natively on the host with Basic Auth, e.g.
   `OPENCODE_SERVER_PASSWORD=<secret> opencode serve --hostname 127.0.0.1 --port 4096`.
   Keep it bound as narrowly as your setup allows.
2. In `.env` set `WB_OPENCODE_SERVER_URL` and the matching username/password, plus a
   random `WB_RUNTIME_TOKEN` shared between the bridge and its adapter. The token is
   **required** to unlock agent operations; an empty value never means allow.
   - Docker Desktop: `WB_OPENCODE_SERVER_URL=http://host.docker.internal:4096`.
   - Linux Docker Engine: set an explicit reachable host URL. The adapter declares
     `host.docker.internal:host-gateway`; if that does not resolve your server, use
     the host's bridge address or a reachable DNS name. Do **not** assume `localhost`
     inside a container means the host.
3. `docker compose up -d` (recreates the adapter and bridge). The manager's
   **OpenCode runtime** panel shows health/version and the locked state;
   `docker compose logs opencode-adapter` shows `server_configured`/`locked` without
   printing credentials.

Docker Compose starts the bridge before the adapter. That ordering is safe: a
transient adapter-unavailable result during startup reconciliation never orphans an
active run. Reconciliation retries until the runtime is reachable and only orphans a
run after a positive missing-session result.

The adapter has **no published port** and is reachable only on the private Compose
network at `http://opencode-adapter:8770`. The bridge never receives provider
credentials or the OpenCode URL. Never add a `ports:` entry to the adapter and never
put the OpenCode server in this Compose file.

## Image layering

The bridge image builds third-party Python wheels in a dependency-only layer
(dependencies extracted from `pyproject.toml` with stdlib `tomllib`) before any
Workspace Bridge source is copied, so source/static/test edits reuse the cached
dependency layer. The builder uses a BuildKit pip cache mount; the runtime stage
still installs offline (`--no-index`) from prebuilt wheels with no cache. The
adapter installs from its lockfile with a BuildKit npm cache mount and keeps
`node_modules` (no dev dependencies) inside the image. Both images stay minimal:
explicit `COPY` allowlists, non-root runtime, read-only rootfs, and health checks.

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

For upgrades, replace the source package outside projects while preserving your
`.env`, optional override and private state. Run `docker compose up -d --build`.
Do not rerun initialization. Back up stopped private state and workspace handoff
folders together, preserving permissions and all SQLite files. The Docker source
archive includes no image layers, tokens or user project data.

## Existing native installation

The default Docker state directory is **separate** from the native one. Do not run
native and Docker services against the same state concurrently. A host-to-container
mount may change device/inode identities even at the same path; the bridge must
not silently re-pin trusted roots. The simplest switch is to preserve the native
state as an archive, start fresh Docker state, register intended mappings, and
update the host tunnel's shared credential. Old handoff files remain on the host,
but fresh state does not import their job records.

A state-preserving native-to-container migration requires reviewing paths,
internal-port configuration and identity checks on the actual host. No automatic
migration/import or SQLite identity bypass is supplied. For an already-working
Compose deployment, ordinary recreate/upgrade uses the existing Docker state;
if root identities change, stop and investigate instead of weakening the checks.

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
OpenCode execution or tunnel authentication; those remain separate live checks.

## Primary references

- https://docs.docker.com/reference/compose-file/services/
- https://docs.docker.com/engine/network/port-publishing/
- https://docs.docker.com/engine/storage/bind-mounts/
- https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- https://docs.docker.com/reference/compose-file/merge/
