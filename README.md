# Workspace Bridge — Phase 1, v0.7.0

**One private tunnel. Protected workspaces. Plain handoffs. Review in ChatGPT.**

ChatGPT reads your project, resolves the technical approach, and writes a handoff.
You paste its instructions and path into OpenCode. When OpenCode finishes, **paste
its reply back into ChatGPT**. ChatGPT inspects current source with the normal
browsing tools and gives findings in the conversation.

There are no source snapshots, automatic changed-file lists, frozen diffs, review
IDs, server-verified audit verdicts, or required result files. The server never starts,
contacts or polls OpenCode, or executes user commands or tests. Source is read-only by
default; a local administrator can explicitly enable text writes per workspace.

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
workspace IDs and handoff prompts, sets each workspace's write permission, shows the three planning documents and access
events, creates/rotates the shared credential, pauses MCP access, and generates a
single tunnel profile. It does not track agent progress, test reports or verdicts.

## Twelve tools

| Purpose | Tools |
|---|---|
| Project-lead guidance | `read_project_lead_skill` |
| Workspace selection | `list_workspaces`, `workspace_info` |
| General inspection and audit | `list_dir`, `glob`, `grep_files`, `read_file` |
| Manual handoff | `prepare_handoff`, `list_handoffs`, `read_handoff` |
| Policy-controlled writing | `write_file`, `edit_file` |

The embedded skill tells ChatGPT to own decisions, give the less-capable implementer
explicit, bounded tasks, and verify its claims against current code. It is loaded
on demand, not repeated in every response. Skill compliance is advisory, not
server-enforced. The API is general; permissions are enforced by the server.
`read_file` reads allowed source, handoff documents, and raster images. `write_file` and `edit_file`
use the same workspace-relative paths, with a separately configured write scope.
`read_handoff` remains an optional convenience, not a restriction on general reads.
See [tool reference](docs/MCP_TOOLS.md) and [file access](docs/FILE_ACCESS.md).

| Local write permission | API value | Effect |
|---|---|---|
| Read-only | `none` | Denies all file writes, including `prepare_handoff` |
| Handoff only (default) | `handoff` | Writes under `.workspace-handoff/` only |
| Workspace-wide | `workspace` | Writes to permitted text files inside that mapping |

New mappings and migrations from pre-v0.5 default to handoff-only; existing
explicitly configured policies survive subsequent upgrades. Nothing auto-enables source
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
setup, persistent state, migration limits and security details.

```sh
# From this package directory, OUTSIDE the projects being mapped:
python3 scripts/configure_docker.py --projects-dir "$HOME/Projects" --mcp-port 8875
docker compose config --quiet
docker compose up -d --build
docker compose exec bridge workspace-bridge --state /state show-admin-token
```

Open `http://127.0.0.1:8766/`, add your actual host project paths, create a bridge
token and enable intended mappings. The same absolute project paths are mounted
inside the container, so OpenCode can read copied handoff paths on the host.
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

The image is built locally, not pulled as a published Workspace Bridge image.
Docker build/runtime validation has not been executed in this delivery environment;
`scripts/test_docker.py` and the added CI job exercise the real container on a host
with Docker. See [validation](VALIDATION.md) for evidence and outstanding checks.

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
OpenCode, project files, or handoffs. Only `/mcp` is tunnelled; the management page
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

**Build:** manually paste the instruction/path into OpenCode. It implements the
plan, runs checks locally, and returns a summary in its own conversation. No
`RESULT.md`, `TESTS.json`, callback, or completion signal is required.

**Review:** paste that reply into ChatGPT, for example:

> Here is OpenCode's return for this handoff: [paste reply]. Read the relevant
> current source, callers and tests using Workspace Bridge. Check the acceptance
> criteria, identify regressions, and give findings or a corrective handoff.

ChatGPT uses `read_handoff` for task context and the same general browsing tools
for code inspection. Findings stay in chat, with optional ordinary notes saved under
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
or handoff path in every write mode. The user still pastes OpenCode's reply into
ChatGPT for general-tool review; workspace permission alone does not authorize
ChatGPT to take over implementation. See [file access guide](docs/FILE_ACCESS.md).

## Upgrade from v0.5.x and earlier

Stop the bridge, back up its private state and handoff folders, reinstall this
release, and restart with the **same state directory**. Do not reinitialize.
Refresh ChatGPT's tool discovery (twelve tools) and reload the skill (1.4.0).
No new tunnel or credential is needed. Existing mappings and handoffs are retained.
No new state migration is added in v0.6; existing explicit write policies persist.
From releases earlier than v0.5, the existing write-scope migration still defaults
to `handoff`. Install normally to include the new Pillow dependency; do not use
`--no-deps` unless it is already installed.

Old baseline/review records and on-disk artifacts are preserved for history, but
are not used or exposed by the retired tools. They are not automatically erased
or reclaimed. New handoffs store no project-source snapshots. The old database
`baseline` column remains inert for compatibility. Start a fresh handoff for new
work; older copies may still contain superseded workflow instructions.

See [v0.6 upgrade](docs/MIGRATION_0.6.md) and [v0.5 upgrade](docs/MIGRATION_0.5.md); the [v0.3 migration](docs/MIGRATION_0.3.md) explains retired review features. For a v0.1 installation also follow the
[earlier gateway migration](docs/MIGRATION_0.2.md).

## Boundaries

Pinned explicit roots, traversal/symlink/hardlink/special-file rejection, exclusions,
bounded reads, heuristic secret redaction, separate local-admin credentials, and
shared-token revocation remain. Repository ignore files are not access policy.
This is not an OS sandbox, complete secret detection, or per-chat authorization.
Source read by ChatGPT leaves your computer through the connection. No shell,
Git execution or direct agent integration was added. Source writes stay disabled
unless the local administrator explicitly selects workspace-wide permission.

See [security](docs/SECURITY.md), [operations](docs/OPERATIONS.md),
[architecture](docs/ARCHITECTURE.md) and [validation](VALIDATION.md).
