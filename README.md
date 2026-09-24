# Workspace Bridge — v0.8.4

**One private tunnel. Protected workspaces. Plain handoffs. Optional agent runs. Review in ChatGPT.**

ChatGPT reads your project, resolves the technical approach, and writes a handoff.
By default you paste its instructions and path into the local agent manually. When a local
administrator enables agent execution for a workspace, ChatGPT can instead start one
bounded agent run on an explicitly configured runtime
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
private host adapters connect to the locally managed runtimes.

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

The manager adds mappings, enables/disables access, edits exclusions, copies
workspace IDs and handoff prompts, sets each workspace's write permission **and its
separate agent-execution policy** (default off), shows the three planning documents
and access events, lists linked agent runs with their conversation IDs, model, state
and notification status, shows live Runtime Protocol interactions and recorded
activities, lets the admin answer supported choices or stop an active run, enforces
per-runtime model policies, creates/rotates the shared
credential, pauses MCP access, and generates a single tunnel profile.

The local manager uses React, TypeScript, Vite, Tailwind CSS, and owned
shadcn/ui components. Starlette serves the API and the compiled UI from the
same loopback listener. Frontend source lives in `web/`; run `npm ci` and
`npm run build` there after UI changes. The compiled assets are included in
the Python package, so installing or running the server does not require Node.

## Twenty-five tools

| Purpose | Tools |
|---|---|
| Project-lead guidance | `read_project_lead_skill` |
| Workspace selection | `list_workspaces`, `workspace_info` |
| General inspection and audit | `list_dir`, `glob`, `grep_files`, `read_file` |
| Read-only Git evidence | `git_status`, `git_diff` |
| Handoff and optional agent dispatch | `prepare_handoff`, `list_handoffs`, `read_handoff`, `list_agent_models`, `start_agent_run`, `list_agent_runs`, `read_agent_run`, `cancel_agent_run`, `list_agent_executions`, `read_agent_execution`, `read_agent_interaction`, `respond_agent_interaction`, `list_agent_activities`, `read_agent_activity` |
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
python3 scripts/configure_docker.py --projects-dir "$HOME/Projects" --mcp-port 8875
docker compose config --quiet
docker compose up -d --build
docker compose exec bridge workspace-bridge --state /state show-admin-token
```

Open `http://127.0.0.1:8766/`, add your actual host project paths, create a bridge
token and enable intended mappings. The same absolute project paths are mounted
inside the container, so the Pi agent can read copied handoff paths on the host.
Point the host tunnel to `http://127.0.0.1:8875/mcp` (or your configured host port).
Both published ports bind to host loopback only. Internal ports remain 8765/8766.

The optional setup script needs host Python 3; manual `.env.example` setup does
not. Runtime Python/Pillow dependencies are installed in the image. Persistent
state defaults to a **separate** `~/.local/state/workspace-bridge-docker` directory;
the existing native installation is not overwritten. No workspace is auto-enabled.
The image uses a non-root user, read-only container root, capability dropping,
private state and a bounded temporary filesystem. The **project bind is writable**
for handoffs; the default source-write prohibition is an application policy,
not an OS read-only mount. Review the Docker guide before enabling source writes.

The image is built locally, not pulled as a published Workspace Bridge image. In this
delivery environment `docker compose config` validated, both the bridge and adapter
images built successfully, and a disposable bridge+adapter container smoke passed
(loopback-only published ports, adapter with no published port, new workspace
disabled/handoff/agent-disabled). The full Compose stack with the tunnel sidecar and
a live host Pi agent was not run here; `scripts/test_docker.py` exercises the
real container on a Docker-equipped host. See [Docker guide](docs/DOCKER.md).

## Install

Python 3.11+; macOS, Linux or WSL2. Native Windows is not supported. This release was
validated in Linux; your host and actual tunnel/client still need validation.
Keep this package, virtual environment, private state and tunnel profile outside
mapped projects.

```sh
cd workspace-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q

mkdir -p "$HOME/Projects"
workspace-bridge init --allow-parent "$HOME/Projects"
workspace-bridge serve
```

Use your actual dedicated project parent. A permitted parent does not expose every
child automatically. In another terminal in the same environment:

```sh
workspace-bridge show-admin-token
```

Open `http://127.0.0.1:8766/`, enter the admin token, add individual mappings, create
one bridge token, and enable only intended projects. Generate the tunnel profile
and follow [tunnel setup](docs/TUNNEL_SETUP.md). Neither token belongs in ChatGPT,
the Pi agent, project files, or handoffs. Only `/mcp` is tunnelled; the management page
stays local. The endpoint, token format and tunnel profile are unchanged from v0.2.

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

## Runtime Protocol v1 adapters (opt-in)

Bridge can coordinate Pi and Codex through the same private [Runtime Protocol
v1](docs/RUNTIME_PROTOCOL.md). Set `WB_RUNTIME_ADAPTERS` to a JSON object that
maps each runtime ID to its private host URL, for example
`{"pi":"http://host.docker.internal:8780","codex":"http://host.docker.internal:8772"}`.
The existing Pi host adapter serves `/v1/*` on its current port. To start the
dedicated Codex host adapter, install this checkout in a host Python environment
and run `workspace-bridge-codex-adapter` with `WB_CODEX_ADAPTER_STATE`,
`WB_CODEX_PROJECTS_ROOT`, and `WB_RUNTIME_TOKEN` set. It binds to host loopback
on port 8772 by default and starts its own Codex app-server process; it does not
attach to a Desktop or TUI thread. Keep adapter state outside project roots.

For each workspace, enable the agent switch, grant each intended runtime, select
an adapter security profile, and save that runtime's enabled model list and
default in the local manager. Every new runtime grant starts disabled. A security
profile applies to new conversations; changing it does not widen an existing
conversation. Open **Profiles** in the manager to create, edit, and delete
custom profiles using the controls supported by Pi or Codex. Assign a saved
profile from the workspace's **Change profile** dialog. Pi's external access controls govern
file tools outside the workspace; shell commands and any enabled host extensions
have separate authority. Assign another profile in every
workspace before deleting one. Native Pi and Codex security mechanisms differ;
review each profile's claims before enabling write access. All configured runtimes
use the same run, interaction, activity, and execution APIs and Bridge database schema.

## Notifications

The Bridge records canonical lifecycle events and per-channel delivery state in a
durable SQLite outbox. Runtime coordinators publish semantic events; a persistent
daemon worker performs channel delivery after startup and wake signals, outside
request and run-state paths. The current adapter is Discord, configured locally with
`WB_DISCORD_WEBHOOK_URL`; future channels can register an adapter without changing
run orchestration. Pending rows for removed channels are disabled so they cannot
block configured channels.

Events contain bounded workspace and handoff labels, Bridge run/runtime identifiers,
timestamps, and optional request kind/action metadata. They never contain prompts,
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
