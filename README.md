# Workspace Bridge — v0.1.0

**One private tunnel. Protected workspaces. Plain handoffs. Optional agent runs. Review in ChatGPT.**

ChatGPT reads your project, resolves the technical approach, and writes a handoff.
By default you paste its instructions and path into the local agent manually. When a local
administrator enables agent execution for a workspace, ChatGPT can instead start one
bounded agent run on an explicitly configured adapter instance
for that prepared handoff through Workspace Bridge and read
the final result back itself. Runtime questions and choices are resumable through
the Runtime Protocol interaction flow. When the run finishes, Workspace Bridge records a
terminal state and publishes a durable notification event to each configured channel.
Discord is the current channel adapter. ChatGPT then audits
current source with the normal browsing tools and gives findings in the conversation.

There are no source snapshots, automatic changed-file lists, frozen diffs, review
IDs, server-verified audit verdicts, or required result files. Read-only Git status
and bounded diffs are available as live review evidence. Source is read-only by
default; a local administrator can explicitly enable text writes per workspace and
separately enable agent execution (default off). The bridge never runs shell commands,
never exposes an arbitrary command tool, and never starts or hosts an agent server:
private adapter daemons provide native runtime behavior.

## Connection and management

```text
ChatGPT — one connection
    │
Private MCP tunnel — one channel
    │
127.0.0.1:8765/mcp — one X-Bridge-Token
    ├── workspace_id A → project A
    ├── workspace_id B → project B
    └── disabled mappings remain inaccessible

127.0.0.1:8766 — local management page; NEVER tunnel it
```

Every project tool requires an explicit `workspace_id`. One credential authorizes
**all enabled mappings**, not separate per-chat permissions. Disable projects that
should not be available. Workspace paths and handoff IDs remain scoped; there is
no mutable shared active-workspace setting.

## Diagnostics and runnable routes

`workspace-bridge doctor` reports local configuration, workspace prerequisites,
adapter-instance health, current profile/model freshness, runnable workspace/adapter routes,
and read-only Git Evidence. Use `workspace-bridge doctor --json` for canonical
DiagnosticReport JSON or add `--offline` to skip all runtime/network calls. The
authenticated local-admin `GET /api/diagnostics` endpoint returns the same report;
`?offline=1` selects offline mode.

Diagnostic statuses are `pass`, `warning`, `action_required`, `failed`, and
`unknown`. Overall severity is deterministic: failed, action required, unknown,
warning, then pass. Doctor exits nonzero for failed/action-required reports and
zero for pass, warning, or unknown-only reports. A runnable route is one exact
workspace/adapter pair whose mapping, shared
MCP gateway, exact WorkspaceRoute, current security profile, and — when a model
policy is configured — its current adapter-scoped default model satisfy
run-start prerequisites. Write scope governs new handoff publication, not run
admission. Listener
health, gateway enablement, and adapter health do not establish that such a
route exists.
Git Evidence reports review capability and never blocks an execution route.

The local Manager reads this canonical report. Only `runnable_routes[].ready`
for the selected exact workspace/adapter pair enables a prepared-handoff start;
overall diagnostic health remains separate.
If diagnostics cannot refresh, the Manager marks route readiness unavailable
and disables starts while leaving other Manager data visible. Its authenticated
`POST /api/workspaces/{workspace}/jobs/{job_id}/runs` action accepts only an
exact `adapter_id` and idempotency request ID for the path-owned prepared job, then
delegates to the existing `service.start_agent_run` policy path. It accepts no
arbitrary prompt or model override.

The native CLI reads `~/.local/state/workspace-bridge` by default; pass the
global `--state` option before `doctor` to select another location. Native checks:

```sh
workspace-bridge doctor
workspace-bridge doctor --json
workspace-bridge doctor --offline
```

For Docker deployments, run Doctor inside the Bridge container so it reads the
same `/state` and container network context as the
live Bridge process:

```sh
docker exec workspace-bridge workspace-bridge --state /state doctor
docker exec workspace-bridge workspace-bridge --state /state doctor --json
docker exec workspace-bridge workspace-bridge --state /state doctor --offline
```

Offline Doctor uses SQLite read-only, starts no runtime or notification workers,
does not recover notification deliveries, and does not take the serve process
lock. Runtime/profile/model freshness is unknown offline; such a route is never
reported ready. The Manager consumes these server-authoritative route identities
directly.

The manager adds mappings, enables/disables access, edits exclusions, copies
workspace IDs and handoff prompts, sets each workspace's write permission **and its
separate agent-execution policy** (default off), shows the three planning documents
and access events, lists linked agent runs with their conversation IDs, model, state
and notification status, shows live Runtime Protocol interactions and recorded
activities, lets the admin answer supported choices or stop an active run, enforces
per-adapter model policies, creates/rotates the shared
credential, pauses MCP access, and generates a single tunnel profile.

The local manager uses React, TypeScript, Vite, Tailwind CSS, and owned
shadcn/ui components. Starlette serves the API and the compiled UI from the
same loopback listener. Frontend source lives in `web/`; run `npm ci` and
`npm run build` there after UI changes. The compiled assets are included in
the Python package, so installing or running the server does not require Node.

## Twenty-six tools

| Purpose | Tools |
|---|---|
| Project-lead guidance | `read_project_lead_skill` |
| Workspace selection | `list_workspaces`, `workspace_info` |
| General inspection and audit | `list_dir`, `glob`, `grep_files`, `read_file` |
| Read-only Git evidence | `git_status`, `git_diff` |
| Handoff and optional agent dispatch | `prepare_handoff`, `list_handoffs`, `read_handoff`, `list_agent_adapters`, `list_agent_models`, `start_agent_run`, `list_agent_runs`, `read_agent_run`, `cancel_agent_run`, `list_agent_executions`, `read_agent_execution`, `read_agent_interaction`, `respond_agent_interaction`, `list_agent_activities`, `read_agent_activity` |
| Policy-controlled writing | `write_file`, `edit_file` |

The embedded skill tells ChatGPT to own decisions, give the less-capable implementer
explicit, bounded tasks, and verify its claims against current code. It is loaded
on demand, not repeated in every response. Skill compliance is advisory, not
server-enforced. The API is general; permissions are enforced by the server.
`read_file` reads allowed source, handoff documents, and raster images. `write_file` and `edit_file`
use the same workspace-relative paths, with a separately configured write scope.
`read_handoff` remains an optional convenience, not a restriction on general reads.
Git evidence accepts no arbitrary commands or refs and never mutates the repository;
excluded changes contribute counts only. A dirty tree may contain edits from before
the current agent run, and Git status/diffs do not establish authorship.
See [tool reference](docs/MCP_TOOLS.md) and [file access](docs/FILE_ACCESS.md).

| Local write permission | API value | Effect |
|---|---|---|
| Read-only | `none` | Denies all file writes, including `prepare_handoff` |
| Handoff only (default) | `handoff` | Writes under `.workspace-handoff/` only |
| Workspace-wide | `workspace` | Writes to permitted text files inside that mapping |

New mappings default to handoff-only. Nothing auto-enables source
writes. To broaden a project later, open its **Exclusions & policy → Write
permission** in the local manager, select **Workspace-wide**, and explicitly save
and confirm. Only local administrator credentials can change this setting; MCP
has no policy-changing tool or override argument. The same tool names, schemas,
tunnel and token work before and after that change. Subsequent calls see the
current policy without a server restart. All exclusions still apply. This grants
capability to every chat using the shared connection, not just one conversation.

## Docker Compose

Docker support is now included. Run the bridge in Compose and keep your existing
secure tunnel client on the **host**. See [Docker guide](docs/DOCKER.md) for manual
setup, persistent state and security details.

```sh
# From this package directory, OUTSIDE the projects being mapped:
python3 scripts/configure_docker.py --state-dir "$HOME/.local/state/workspace-bridge-docker" --mcp-port 8875
docker compose config --quiet
docker compose up -d --build
docker compose exec bridge workspace-bridge --state /state show-admin-token
docker exec workspace-bridge workspace-bridge --state /state doctor
```

Open `http://127.0.0.1:8766/`, add an authoritative Node endpoint and token,
then add workspace mappings using roots on that Node. The Bridge container is the
control plane and does not inspect a local project bind; the Node service owns
those files, Git data, handoffs, and runtime adapters. Point the host tunnel to
`http://127.0.0.1:8875/mcp` (or your configured host port). Both published ports
bind to host loopback only. Internal ports remain 8765/8766.

The optional setup script needs host Python 3; manual `.env.example` setup does
not. Runtime Python/Pillow dependencies are installed in the image. Persistent
state defaults to a **separate** `~/.local/state/workspace-bridge-docker` directory;
the existing native installation is not overwritten. No workspace is auto-enabled.
The image uses a non-root user, read-only container root, capability dropping,
private Bridge state and a bounded temporary filesystem. It has no workspace data
bind: the authoritative Node host owns project files, Git, handoffs and source
writes under its own `allowed_roots` policy.

The image is built locally, not pulled as a published Workspace Bridge image. The
v3 Node split has not been validated with a live Docker Compose plus Node service
smoke in this checkout. The older `scripts/test_docker.py` and
`scripts/validate_container_transport.py` fixtures still describe the pre-Node
local-bind flow and are not v3 evidence; validate the Bridge and its authoritative
Node together on a Docker-equipped host before relying on a Compose deployment.
See [Docker guide](docs/DOCKER.md).

## Install

Python 3.11+; macOS, Linux or WSL2. Native Windows is not supported. This release was
validated in Linux; your host and actual tunnel/client still need validation.
Keep private state and tunnel profile outside mapped projects.

The supported installation is a persistent `uv tool` environment:

```sh
uv tool install workspace-bridge
workspace-bridge --version

workspace-bridge init
# Host-only Node (safe loopback default):
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" init \
  --allow-root "$HOME/Projects"
# For Docker Desktop, run the init command instead with --host 0.0.0.0.
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge serve
```

Contributors working from a checkout can use a development venv instead:

```sh
cd workspace-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

## Manual updates

There is no automatic or remote updater. Update the installed tool locally
on each host, then explicitly restart the affected persistent services:

```sh
uv tool upgrade workspace-bridge
workspace-bridge --version
# Restart the Node service if it is persistently installed, e.g.:
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
```

A package upgrade never restarts anything by itself. The Pi adapter
(`workspace-bridge-pi-host-adapter`) is a separate local npm-managed
component updated with npm on its host. The Manager System / Versions view
shows component versions and compatibility as information only.

On macOS, `service install` creates the per-user
`~/Library/LaunchAgents/com.workspace-bridge.node.plist` (launchd) and keeps
the native Node running outside Compose. On Linux, the same plain nested
command installs the system unit
`/etc/systemd/system/workspace-bridge-node.service` and keeps the native Node
running outside Compose; it starts at boot and survives logout automatically
while `User=`/`Group=` keep the Node itself non-root with the same workspace
permissions as the installing user. Run the command normally without leading
sudo; the backend requests sudo only for the narrow systemd administration
steps. An intentional advanced root mode exists (root-owned state managed as
root, `User=root`/`Group=root`, root-controlled executable required); a root
invocation against a user-owned state is rejected, never converted. Neither
backend prints or stores the Node token in the unit. `service
status` is read-only and reports the managed unit, enabled/running state,
configured listen address, and a bounded authenticated `/v1/status` result.
`service uninstall` removes only that managed unit; Node state, tokens,
adapters, allowed roots, and private logs remain. On Linux, check the journal
manually with `journalctl -u workspace-bridge-node.service`; the program never
scrapes journal contents. Manual package updates remain
`uv tool upgrade workspace-bridge`, then
`workspace-bridge node ... service restart`.

When the Bridge runs in Docker Desktop, a Node initialized with `--host
0.0.0.0` is registered in Manager as `http://host.docker.internal:8770` (use
the configured port). A Node bound only to `127.0.0.1` is intentionally not
assumed to be reachable from the Bridge container. Non-loopback binding exposes
the authenticated Node on host interfaces, so use a host firewall/private
network and never put it behind the MCP tunnel. Compose still contains only
Bridge and the MCP tunnel; native Node and Pi/Codex adapter daemons stay on
their host with identical absolute workspace paths.

Add the Node URL and one-time Node token in the Manager, then register only
canonical child roots on that Node. The Node `allowed_roots` ceiling is configured
on its host and is not editable by Bridge. In another terminal in the same environment:

```sh
workspace-bridge show-admin-token
```

Open `http://127.0.0.1:8766/`, enter the admin token, add individual mappings, create
one bridge token, and enable only intended projects. Generate the tunnel profile
and follow [tunnel setup](docs/TUNNEL_SETUP.md). Neither token belongs in ChatGPT,
the Pi agent, project files, or handoffs. Only `/mcp` is tunnelled; the management page
stays local. The MCP endpoint remains `/mcp`; refresh any cached connector registration after MCP schema changes.

## Daily loop

**Plan:** ask ChatGPT to inspect the project and prepare a small, concrete handoff.
It returns a copyable instruction and an absolute path. Each new handoff contains:

```text
.workspace-handoff/jobs/job_<id>/
├── TASK.md
├── CONTEXT.md
└── ACCEPTANCE.md
```

Optional `context_hashes` only check specifically referenced files before publishing;
they do not capture a baseline. No whole-project snapshot scan is performed.

**Build:** manually paste the instruction/path into the local agent (Pi by default). It implements the
plan, runs checks locally, and returns a summary in its own conversation. No
`RESULT.md`, `TESTS.json`, callback, or completion signal is required.

**Review:** paste that reply into ChatGPT, for example:

> Here is the agent's return for this handoff: [paste reply]. Read the relevant
> current source, callers and tests using Workspace Bridge. Check the acceptance
> criteria, identify regressions, and give findings or a corrective handoff.

ChatGPT uses `read_handoff` for task context, runtime activities, read-only Git
status/diffs, and the same general browsing tools for code inspection. Findings stay in chat, with optional ordinary notes saved under
`.workspace-handoff/`. A follow-up change gets another small handoff. `prepared` means published, not completed or approved.

These are live reads, not a historical diff: ChatGPT cannot automatically prove
all changes, deletions, renames, authorship, or runtime success. Stop file writers
during review and re-read stale pages. Agent-reported test outcomes remain claims.

## Images through the same reader

```text
read_file(workspace_id=actual_id, path="screenshots/settings.png")
read_file(workspace_id=actual_id, path="screenshots/settings.png", max_image_dimension=4096)
```

PNG, JPEG, WebP, GIF, BMP and TIFF are decoded locally and returned as native MCP
image content alongside source/preview hashes and dimensions. No separate image
server, cloud converter, agent or public download URL is needed. Text calls keep
line-numbered output. Images use no line pagination: omit `offset` and `limit`.
`representation="auto"` is the default; explicit `text` and `image` are available.
Only the first frame/page is previewed; animation and multi-page TIFF are not traversed.

Input: at most 20 MiB / 40 million decoded pixels. Default longest edge 2048 px
(requestable 256–4096), maximum preview 2 MiB. Previews may be resized further to
fit that byte cap; they are not byte-exact or color-managed originals. EXIF orientation
is applied, embedded metadata removed, and transparency retained. **Visible secrets
in the pixels are not detected or redacted.** Treat screenshot instructions as untrusted.

Image reads work under every write policy, including `none`. Writes stay text-only.
SVG remains source markup; PDF/Office/RAW/HEIC/AVIF are not rendered. See
[image support](docs/IMAGE_SUPPORT.md) for limits and live compatibility checks.
Local native-response tests do not prove that your exact ChatGPT/tunnel client
passes pixels to the model; verify that connection with a fresh visual marker.

## General file writing

`write_file(workspace_id, path, content, expected_sha256=null)` creates or fully
replaces a UTF-8 file. `edit_file(workspace_id, path, old_text, new_text,
expected_sha256)` replaces one exact unique occurrence. Paths are workspace-relative;
check `workspace_info.write_scope` rather than infer access from a tool name.

Create-only is the default. To replace or edit, first read the file and supply its
current `sha256`. Stale or ambiguous edits are rejected. Text is bounded to 256 KiB;
unsafe links, excluded paths, binary/control content and detected secrets remain
denied even in workspace mode. No delete, rename, append or command-execution API.
New files are private (0600); source replacements preserve ordinary permission bits.
Handoff replacements remain 0600. Extended attributes/ACLs are not copied.

For handoff discovery, explicitly select `.workspace-handoff` in list/glob/search;
default root-source scans omit notes. Normal `read_file` can read any allowed source
or handoff path in every write mode. The user still pastes the agent's reply into
ChatGPT for general-tool review; workspace permission alone does not authorize
ChatGPT to take over implementation. See [file access guide](docs/FILE_ACCESS.md).

## Nodes, adapter instances, and workspace routes

Bridge uses the private [Runtime Protocol v1](docs/RUNTIME_PROTOCOL.md) for Pi
and Codex behavior. A Node is the authoritative data-plane service for one
machine and owns its allowed roots, workspace files/Git/handoffs, and runtime
adapter registry. A `RuntimeType` is the protocol family (`pi` or `codex`);
an `AdapterInstance` is one Node-owned destination with its own name, endpoint,
token, enabled state, and connection revision. Two Pi instances can coexist on
one Node, such as local and GPU-backed daemons. A `WorkspaceRoute` binds one
workspace to one exact same-Node adapter ID and one optional default.

Create, edit, test, disable, and delete Nodes and their adapter instances from
the local Manager's **Nodes** area. Bridge stores only the Node token and a
sanitized adapter cache; runtime adapter tokens remain in the private Node
SQLite. Both databases are mode `0600`. Normal APIs show only whether a token
exists, never its value. Blank token fields preserve the stored token. Endpoint
and token edits take effect on the next request without restarting Bridge.

Enable exact targets from each workspace's **Execution targets** controls. The
surface is present even when no routes exist and discovers only adapters from
the workspace's authoritative Node. Model policy and native profile discovery
belong to each adapter instance. Codex `runtime-config` security follows the
selected Codex adapter and exact workspace route. Diagnostics and Handoff start
controls use the exact `(workspace_id, adapter_id)` pair and show Node, model,
readiness, and effective security. MCP callers first use
`list_agent_adapters`; `runtime_type` is descriptive, while `adapter_id` selects
the destination. There is no implicit cross-Node or runtime-type fallback.

Bridge-side endpoints and tokens live in SQLite, not `WB_RUNTIME_ADAPTERS` or
a Bridge-wide `WB_RUNTIME_TOKEN`. Native Pi/Codex daemon ports, tokens, state,
and launch services remain configured on their host machines for now. The
Manager owns how Bridge connects to a daemon; daemon lifecycle automation is a
separate future milestone. Existing non-v3 Bridge or Node databases are rejected with
`state_schema_incompatible`; use fresh state paths because this development
cutover does not migrate older state.

Pi's external access controls govern file tools outside the workspace; shell
commands and enabled host extensions have separate authority. Native Pi and
Codex security mechanisms differ; review each profile's claims before enabling
write access. All adapters share the Runtime Protocol, but routes, model policy,
profiles, and execution history remain scoped to each adapter ID.

Codex profiles select a native permission-profile ID with an approval policy and
reviewer. The separate **Use Codex config (config.toml)** source follows the
current effective native configuration for that workspace. Codex owns filesystem,
network, domain, and socket rules in its config layers; Bridge shows only a bounded
summary and does not duplicate those controls. Discovery and thread creation use
the exact workspace directory. Profile-bound starts send the permissions selector;
config-bound starts omit security overrides so Codex resolves its own configuration.
See [Codex adapter security profiles](docs/CODEX_ADAPTER.md).

## Notifications

The Bridge records canonical lifecycle events and per-channel delivery state in a
durable SQLite outbox. Runtime coordinators publish semantic events; a persistent
daemon worker performs channel delivery after startup and wake signals, outside
request and run-state paths. The current adapter is Discord, configured locally with
`WB_DISCORD_WEBHOOK_URL`; future channels can register an adapter without changing
run orchestration. Pending rows for removed channels are disabled so they cannot
block configured channels.

Events contain bounded workspace and handoff labels, Bridge run and adapter IDs,
adapter names, runtime types, timestamps, and optional request kind/action metadata.
They never contain prompts,
results, source, commands, external paths, webhook URLs, tokens, or raw provider
payloads. Delivery failures are bounded, isolated per channel, and never change run
state. A crash after remote acceptance but before the sent result is persisted can
cause one duplicate after restart; delivery is at least once across that window.
`read_agent_run` exposes a safe multi-channel delivery summary; `/api/status` reports
configured channel ids and readiness without endpoints or credentials.

## Boundaries

Pinned explicit roots, traversal/symlink/hardlink/special-file rejection, exclusions,
bounded reads, heuristic secret redaction, separate local-admin credentials, and
shared-token revocation remain. Repository ignore files are not access policy.
This is not an OS sandbox, complete secret detection, or per-chat authorization.
Source read by ChatGPT leaves your computer through the connection. No general shell
or Git execution tool was added. Runtime execution occurs only through bounded
Runtime Protocol tools and private host adapters, after a local administrator enables
agent execution for the workspace. Source writes stay disabled unless the local
administrator explicitly selects workspace-wide permission. Runtime profiles are
not necessarily OS sandboxes; review each adapter's enforcement claims.

See [security](docs/SECURITY.md), [operations](docs/OPERATIONS.md),
[architecture](docs/ARCHITECTURE.md) and [validation](VALIDATION.md).
