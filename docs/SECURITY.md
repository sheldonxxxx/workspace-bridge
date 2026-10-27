# Security model

## Intended threat model

Restrict a remote ChatGPT tool connection to explicitly enabled project mappings, with no arbitrary command execution and handoff-only writes by default. Only local administrators may explicitly enable bounded source-text writes and, separately, opt-in Runtime Protocol agent execution. Defend against path traversal, accidental scope expansion, unsafe filesystem entries, cross-workspace path/job/conversation confusion, browser cross-origin access and accidental treatment of agent claims as verified results.

Not covered: a hostile local user/process with the service user's permissions; root/admin compromise; malicious browser extensions; compromised Python dependencies or tunnel-client; vulnerabilities in the host OS; comprehensive secret detection; prevention of every prompt-injection attempt; or ensuring that the local coding agent obeys a handoff. A prompt cannot substitute for the local agent's sandbox.

## Paired Node gateway

Paired Nodes dial the Bridge's Node gateway over TLS 1.2+ and pin its
self-signed certificate by SHA-256 fingerprint (no CA, no hostname check).
Each connection answers a fresh challenge with an Ed25519 signature that also
binds the certificate fingerprint; only the Node's public key is stored on
the Bridge, and its private key stays in the Node state (`0600`). Pairing
codes are 128-bit, single use, expire after 10 minutes, and are stored only
as hashes. Requests carrying a browser `Origin` are refused, and repeated
authentication failures are throttled. The gateway binds loopback unless
`WB_NODE_GATEWAY_HOST` is set; expose it only on a private network or VPN.
A paired Node trusts its Bridge fully, exactly as an HTTP Node trusts the
holder of its token.

## Shared credential scope

One gateway credential authorizes every enabled mapping. Each project call requires `workspace_id`, and each path/job is checked inside that selected workspace. This prevents accidental identifier mixing but is **not per-chat or per-user authorization**: a chat with the shared connection may deliberately select another enabled project. Restrict tunnel/app sharing, disable unnecessary mappings, and use global pause or shared-token rotation when needed. No OAuth identity or per-client ACL is implemented.

## Implemented controls

- Two loopback listeners, no proxy-header trust, strict Host/Origin checks, separate admin/shared-bridge credentials. Manager operations never appear as MCP tools.
- Each Workspace is bound to one authoritative Node. The Node owns the host `allowed_roots` ceiling and validates the canonical node-local root on registration and every data-plane request; Bridge cannot edit that ceiling or inspect a local fallback checkout. Overlapping mappings on one Node are rejected even when disabled, and registrations start disabled.
- Relative POSIX paths only. No absolute reads, `..`, ambiguous separators or paths outside the selected workspace. Current write scope further restricts writes. Each opened ancestor uses `O_NOFOLLOW`; special files, hardlinks and cross-device traversal are rejected.
- Local write scope is `none`, `handoff` (default), or `workspace`; it is reloaded for each call (a queued write reloads it when its turn comes), and no MCP argument can override it. `none` denies `prepare_handoff` too. Create-only by default; existing files require their current hash. `apply_patch` can also move and delete files under the same scope, exclusions and hash checks, journaling earlier content on the Node for `undo_edit`. No shell or arbitrary command surface is exposed as an MCP tool. Two read-only Git evidence tools invoke only fixed status/diff operations with bounded time/output, no pager, external diff/textconv, filter drivers (repository-defined clean/smudge/process filters are overridden to empty), hooks, network transports, terminal prompting, or repository mutation.
- `RuntimeType` (`pi`, `codex`, or `claude`) describes protocol behavior. An `AdapterInstance` is one exact execution destination owned by a Node, identified by `adapter_id`; multiple instances can share a runtime type. A `WorkspaceRoute` binds one workspace to one same-Node adapter ID and at most one enabled default. Execution requires exactly that route and its adapter to be enabled on the same authoritative Node; a legacy workspace-wide execution switch may remain in storage and API responses for compatibility but is inert and is never a run gate. It is not implied by `write_scope`; MCP has no tool to change route or policy settings. `start_agent_run(adapter_id, ...)` is handoff-bound or takes a bounded direct instruction (published as a minimal auditable handoff through the normal Node write policy, deterministically derived from the adapter_id + run request_id idempotency domain; never both), accepts no free-form prompt path, and resolves a model against the adapter's configured allowlist when one exists; with no Bridge model policy the models are unrestricted by Bridge governance and the runtime chooses its own default. There is no arbitrary command endpoint.
- Runtime Protocol v1 conversations persist workspace, adapter ID, descriptive runtime type, connection revision, and security source. Every run carries the rules of its route's [security profile](#security-profiles), and the plugin echoes the profile ID and revision it enforces; a mismatch fails the run before any prompt. Each run records its rules in an immutable `effective_security` snapshot. Interactions require a live exact request, not merely a persisted ID.
- Claude Code loads the host's Claude settings, `CLAUDE.md`, skills, plugins, sub-agents, and MCP servers as the CLI does, so hooks and MCP servers configured in a loaded layer run with the host user's authority. The Node protects `.claude` and `.mcp.json` from Bridge reads and writes, the plugin never edits them, and the plugin's `setting_sources` setting can drop the project and local layers.
- Per-adapter model policy (enabled selectors, one mandatory default, and optional per-model reasoning defaults) and the Bridge-owned run list (`/api/runs`, newest first, bounded) are local-admin-only; no MCP tool can enable models, change these defaults, bypass a configured policy, or act on arbitrary adapter conversations. An unconfigured policy is a governance choice, not a failure: models are then unrestricted by Bridge policy and the runtime selects its own default.
- HTTP Node tokens are plaintext in private Bridge SQLite; agent plugins have no tokens. Both state databases are mode `0600` and their containing directories are private. Normal list/detail/status APIs return `has_token` only; add/edit forms do not read tokens back, and blank update fields preserve them. Node and plugin responses are scrubbed before they reach models, profiles, run data, diagnostics, events, logs, or errors. Backups remain sensitive.
- Fresh writable state creates schema v4 directly (including per-run native token usage). Existing v4 state receives narrow in-place upgrades only when a release needs them (for example, `0.2.0` widens the runtime-type constraint to accept `claude`), inside one transaction that preserves rows and indexes. Pre-v4 state is rejected as `state_schema_incompatible` and is never altered; use a fresh state path for it.
- All writes reject binary/control content, administrator exclusions, and secret-like text the file did not already hold (existing matches may stay, so such files remain editable). Staging uses private files and complete-content publication, not in-place truncation. Rechecks catch ordinary target/parent changes; they are not a sandbox or a portable atomic compare-and-swap against hostile external writers. Parent folders may remain after a later write failure; read back after an ambiguous I/O failure.
- Built-in sensitive-name and build-directory exclusions. Credential and version-control names are always denied; an administrator `!name` entry can show only a hidden-by-default generated or dependency name such as `build` or `vendor`. Additional administrator globs are conservative and case-insensitive. Repository ignore files cannot grant access or relax policy.
- Git evidence requires a direct real `.git` directory and rejects linked worktrees, bare repositories, symlinked/hardlinked metadata, shared object stores and external config includes. Changed paths are filtered through workspace exclusions before any patch command; denied paths are represented by aggregate counts only. Patch text uses the normal secret redactor. Status hashes bind pagination/diffs to the filtered current state, but do not attribute edits to an agent or user.
- Request type/size bounds, bounded reads/search, output budget, concurrency gate and quotas. File changes during reads and stale context hashes fail rather than silently succeeding.
- Generated planning document hashes expose local modification when a handoff is read. No runtime test execution, automatic approval gate, stored verdict, or claim of independent verification is provided.
- No third-party scripts, escaped text rendering, a restrictive CSP, no browser persistent token storage, and no contents/keys in operational events. The UI intentionally does not render project Markdown as HTML.

## Security profiles

Security profiles live in the Bridge and are edited only by a local admin in
the Manager; no MCP tool reads or changes them. A profile sets `deny`, `ask`,
or `allow` for reading and writing inside and outside the workspace, the
shell, web access, and extensions (MCP servers and plugins). It can also list
`protected` paths, `external_roots` outside the workspace with their own read
and write modes, and `commands` rules that decide shell commands by prefix
before the shell mode applies. A command that is only a shell running an
inline script, such as `zsh -lc 'git push'` (the form Codex runs every
command in), is judged by its script; a script nested further inside a line
asks at least when the profile has ask or deny command rules. Paths under `.git`, `.env` files (templates such
as `.env.example` excepted), and the root `checks.toml` are always protected.

| Profile | Reads | Writes | Shell, web, extensions |
| --- | --- | --- | --- |
| `read-only` | Workspace only | None | None |
| `reviewed` | Workspace; ask outside | Workspace only | Ask |
| `trusted` | Anywhere | Workspace; ask outside | Allow |

`ask` pauses the run until ChatGPT answers the interaction, on its own under
the project-lead skill's approval rules or by asking you in the chat. Nothing
answers on a timeout, and the Manager can only cancel the run. Each answer is
recorded with `decided_by` (`chatgpt` or `owner`, as ChatGPT reports it) and a
reason. The skill is guidance, not enforcement, so put anything that must
never be approved without you under `deny`, not `ask`, and review the
approval log filtered to `chatgpt` now and then. A route names one
profile, or `native` for a plugin that can follow the agent's own security
(Codex and Claude Code). A route is refused when its plugin cannot enforce every restrictive
rule of the profile: Codex, for example, keeps reads, web access, and
extensions inside its read-only sandbox ungated, so it accepts `trusted` and
`native` but not `read-only` or `reviewed`. A run keeps the rules it started
with, and an edit reaches the next run, including a continuation. Before
saving an edit that loosens a profile, or that a routed plugin cannot enforce,
the Manager lists the affected rules and routes and asks for confirmation. A
profile in use cannot be deleted.

Under `native`, the agent's own permission settings decide, and every
permission prompt the agent raises becomes a Bridge interaction. Codex follows
its effective configuration (sandbox, approval policy, reviewer). Claude Code
runs with its own tools and loaded settings in the mode set by the plugin's
`permission_mode` setting (`default`, `acceptEdits`, `plan`, `auto`, or
`dontAsk`) or else by the settings' `permissions.defaultMode`. Claude Code
ignores a `defaultMode` of `auto` when driven through its SDK, so auto mode
needs `--set permission_mode=auto`. Bypass mode stays disabled. In auto mode
Claude Code's classifier, not the Bridge, decides what runs unasked.

Each plugin enforces the rules itself, in its own way; see
[Security on every run](RUNTIME_PROTOCOL.md#security-on-every-run). None of
them is an OS sandbox. Allowing the shell, writes outside the workspace, or
extensions gives the agent broad host authority.

## What is excluded by default

VCS internals, common package/build/cache directories, credential directories, `.env*`, private-key/certificate bundle formats, common token/credential filenames, SQLite/databases and log files. Exact controls are in `security.py`.

These exclusions are intentionally conservative: even `.env.example`, some public-key-looking names, and generated files may be unavailable. Do not weaken access protection simply to satisfy an agent. For a needed safe example, manually create a sanitized document with a neutral filename and explicitly review its contents before exposure.

An ignored file can still be readable if it is not excluded by bridge policy. Never assume `.gitignore` protects secrets. Denied directories are not traversed or counted as audited content. Registered mappings cannot point into a denied subtree to bypass the policy.

## Search-specific bounds

Globs filter a safe inventory and cannot resolve arbitrary files. Search cursors are HMAC signed and bound to workspace, policy, query and observed file metadata. A stale cursor requires restarting; a valid cursor is not a long-lived content snapshot. Queries run over redacted text and regex matching has a timeout. Compilation, filesystem latency and other in-process operations are not a hardened resource sandbox; stronger multi-user isolation requires OS-level limits and a different authorization model. `.gitignore`/`.ignore` never control bridge access.

## Redaction is not a DLP guarantee

The heuristic recognizes several common token/password/private-key patterns. It can miss unusual secrets and falsely redact legitimate code. Deny policies and choosing appropriate projects are the primary safeguards. Do not put credentials into plans or reports. Redacted source is incomplete evidence; report that limitation during review.

## Lifecycle notifications

The Bridge persists only canonical event identity, bounded workspace/handoff labels,
Node and adapter ID/name, runtime type and run IDs, timestamps, and allowlisted request kind/action metadata. It
does not persist prompts, results, source, commands, external paths, arbitrary
provider payloads, webhook endpoints, or tokens in notification tables. The
`WB_DISCORD_WEBHOOK_URL` value is read from local process configuration and held
only by the Discord adapter. Discord payloads suppress mentions and contain the same
metadata-only event envelope. Channel result codes/details are bounded and
redacted; raw HTTP bodies are discarded. Delivery failures cannot change run state,
and status/read APIs expose channel ids and delivery summaries without secrets.

The [MCP event broker](EVENTS.md) projects only Bridge-issued
identifiers, allowlisted state/request kind and timestamps from that same journal.
Subscriptions use the shared Bridge credential, explicit enabled-workspace
filters and expiry; they introduce no per-chat ACL. Credential rotation and
disabled mappings stop delivery. Callback URLs and Standard Webhooks signing keys
stay in private Bridge state and never appear in status APIs or events. A signed
challenge verifies each callback before activation; HTTPS connections validate
public DNS answers and pin the connected address while preserving TLS hostname
verification. No redirect, proxy, private/local destination, event-triggered run
or approval response is allowed. Verification and delivery hold no Service DB lock.

Source returned through the tunnel reaches ChatGPT. The tunnel removes a public inbound endpoint; it does not make model processing local or mean code never leaves the machine. Check organizational AI rules and your account's data controls.

## Limits

| Item | Initial bound |
|---|---:|
| Source/artifact read | 512 KiB per file |
| Filesystem enumeration | 10,000 entries, depth 40, approximately 5 seconds |
| Search batch | Up to 100 files, 8 MiB, approximately 5 seconds |
| Search hits | Up to 100 matching lines per page; continuation within files |
| Regex matching | 20 ms timeout per line; overall search budget |
| Tool response | 24,000 serialized result characters before MCP envelope; browse pages budget UTF-8 bytes conservatively |
| HTTP request body | MCP: 1 MiB except `write_image` (28,027,564 bytes); local management: 96,000 bytes; 10-second body timeout |
| Handoff text mutation | 256 KiB final/previous file; JSON escaping also counts against the HTTP budget |
| Handoff image upload | 20 MiB original raster bytes, 40MP; matching MIME/suffix, first-frame decoder validation; original metadata retained, pixels not redacted |
| Handoff image references | Up to 20 unique paths; context and attachments share a 64 MiB publication-time read budget |
| Source / artifact page | Up to 400 / 200 lines respectively |
| Handoffs | 100 per workspace |
| Private-state quota gate | 512 MiB estimated SQLite allocation |
| Operational events | Most recent 10,000 |
| Concurrent accepted tool work | Up to 4 calls; writes serialize per workspace, run starts per route, run input per run; reads, cancels and other workspaces proceed in parallel |

The SQLite quota is a preventive allocation gate, not an OS disk quota; WAL/journal overhead and races with non-service processes are not a strict byte cap. The process should also run with OS-level resource limits for an untrusted multi-user deployment. Very large workspaces need deliberate exclusions or narrower browsing scope.

## Deployment rule

Never tunnel the admin listener. Admin passwords and browser sessions never authorize MCP requests. For stronger isolation use a dedicated service identity that can read only selected projects and write only private state plus their handoff directories. Configure and test those permissions yourself before treating them as enforced. Run only one bridge process per state directory.

The admin listener is loopback-only by default. Setting `WB_ADMIN_ALLOWED_HOSTS`
(admin only; MCP is unaffected) is the sole opt-in remote-admin path: a
comma-separated list of bare hostnames/IPs (no ports, schemes or wildcards)
that widens only the admin listener to `0.0.0.0` and allows those `Host` values
(`http` + `https` origins). Empty (default) keeps loopback-only and fails
closed on invalid values. Prefer SSH port-forwarding or VPN; plain HTTP bears
admin credentials in clear, so use TLS termination and firewall rules when remote
is unavoidable. Docker additionally requires republishing the admin host port
beyond `127.0.0.1` to reach LAN.

A disabled mapping retains its history. Token rotation revokes the old token for subsequent calls. Neither action erases copies already returned to ChatGPT, cancels completed reads, or necessarily interrupts work already holding the service lock.

## Review boundary

The user supplies the agent reply; ChatGPT reads current source and reports its
assessment in the conversation. There is no retained before-state, complete change
inventory, tamper-proof review receipt or automated test verification. Exclusions,
binary files, redaction and unreadable content limit what can be assessed.

Backups remain sensitive.

## Policy-scoped writes and readable notes

Access requires the existing shared credential and an enabled, explicitly selected
workspace. `read_file` and explicit handoff scans obey the same filename/administrator
exclusions. Default source-root scans still skip handoffs. Internal staging names
beginning `.wb-write-` are never caller-addressable and are hidden from tools.

The writer never follows existing symlinks, truncates a hard-linked inode, or
adds executable permissions to newly created files. New files and replaced handoff
notes use 0600; source replacements preserve ordinary permissions including execute
bits, stripping special bits. Parent directories
created by the bridge use 0700. Existing ACLs/extended attributes are not preserved
by replacing an inode. Host case-sensitivity and filesystem permissions still apply.

No aggregate handoff disk quota, version history, automatic pruning or general
file deletion is added. The 256 KiB bound is per file, not a total disk limit.
Do not use this as arbitrary storage; configure OS quotas for stricter limits.
Normal notes and user-pasted returns remain untrusted data, not proof that tests
ran or that an independent audit occurred.

## Runtime Protocol agent execution

Bridge-to-Node endpoints and Node tokens are managed in the local Manager and
stored in private Bridge SQLite. Agents run as plugins: child processes of
their Node with no port, no credential, and the Node's environment minus
every `WB_*` variable. They do not receive provider credentials from
Bridge.

Each conversation is bound to one enabled workspace and exact same-Node
AdapterInstance, and to either Bridge security profiles or the agent's native
security. Bridge rechecks the exact WorkspaceRoute, prepared handoff, profile,
and adapter connection before a run, and sends the profile's current rules with
it. Responses are limited to live, adapter-provided interactions and are
revalidated by the adapter. Neither a profile nor a workspace binding should be
described as an OS sandbox. Agents run with the host user's authority; review a
profile's shell, external-path, and write rules before routing it.

## Run checkpoints

Run checkpoints are the only stored copy of workspace source. The owning Node
keeps them in `checkpoints/` under its private state directory, which can never
overlap a workspace. Content is limited to what `read_file` may return: the same
built-in deny list, workspace exclusions and 512 KiB per-file limit, read through
the same no-symlink descriptor checks. Git writes only that store; the
workspace and its `.git` are never written, and Git runs with fixed arguments,
no hooks and no user or system configuration.

A file deleted or rewritten in the workspace stays readable through run diffs
until retention removes its checkpoints (14 days, or sooner once a workspace
store exceeds 1 GiB). Exclusions added later hide paths immediately; clear the
workspace's checkpoints in the Manager to delete them from disk. Diffs are
redacted like `git_diff`, which is not a DLP guarantee. The Bridge stores only
checkpoint IDs, statuses, error codes and changed path names. Support bundles
never include checkpoints.

## Diagnostics and Doctor

The local-admin diagnostics API requires the admin bearer/session authentication.
It returns stable check codes and bounded workspace/adapter metadata, but never
adapter URLs, credentials, raw environment values, prompts, tool arguments,
provider payloads, or full external paths. Runtime failure text is reduced to
fixed safe summaries.

Doctor uses a SQLite `mode=ro` connection, starts no run-reconciliation or
notification worker, does not recover `sending` deliveries, and does not take or
create the serve process lock. It reads database facts under the shared service
lock, releases that lock before bounded private runtime calls, and performs no
state mutation. Offline Doctor does not call runtime adapters; remote-dependent
checks become unknown, which prevents a route from being reported ready.

## Local policy control

New mappings default to write_scope=handoff. The manager requires
a separate admin credential and explicit workspace-write confirmation. Its routes
are absent from the MCP listener; no remote permission-changing tool is offered.
Workspace mode is available to every chat using the shared connection, not per-chat
authorization. A source edit can affect later application/agent behavior even though
the bridge never executes code. Keep its own installation and private state outside
projects, stop concurrent agents/watchers and grant OS permissions separately.

## Image decoding

Image input passes the same root, exclusions, no-follow/single-link and device checks.
Only six validated raster formats receive the 20 MiB read budget; text retains 512 KiB.
Decoding occurs in a fixed package-owned `python -I -B` worker with authorized bytes,
not a workspace path, URL, shell string or passed environment credentials. Input
size, 40MP pixels, 2 MiB preview, 3 MiB response, metadata allocation and decode time
are bounded. Linux applies a 1 GiB address-space ceiling; macOS memory isolation is
not claimed. Native decoder failures are sanitized. Keep Pillow updated.

This is resource containment, not an OS/network sandbox. The worker still has the
service's OS privileges; use a dedicated account/container for hostile files. Never
put the bridge installation or virtualenv inside writable mapped projects. Image
previews exist only in process memory and the response; events contain no image
bytes, metadata, filenames or hashes. Existing tunnel/client logs are outside that
guarantee. EXIF/GPS/XMP/comments/profile metadata is dropped, but visible pixel
secrets and embedded image instructions are NOT detected/redacted. Only authorize
images permitted to leave the host; use administrator exclusions for sensitive paths.

Supported animation/multi-page formats return only the first frame/page. No full
animation review, OCR, external fetch, raw-binary export or exact color/byte fidelity
is implied. Client-side handling still requires live validation.

## Docker deployment

The container uses a non-root host UID/GID, read-only root filesystem, a bounded
noexec/nosuid/nodev temporary filesystem, dropped capabilities and no-new-privileges.
No privileged mode, Docker socket, host network, cloud API keys or tunnel credential
is included. Both bridge ports are explicitly host-loopback only; the native Pi
host adapter has **no** Compose service and publishes **no** port. Use a current
Docker Engine (28+ avoids the documented old localhost-publication L2 exposure).
Never attach untrusted services to its Compose network or add a public reverse proxy.
The Manager stores Bridge-to-Node endpoints and Node tokens in private Bridge
SQLite. Node-owned adapter endpoints and per-instance tokens stay in the private
Node SQLite. Docker Desktop/OrbStack commonly use `host.docker.internal`; Linux
Engine needs a reachable Node URL. Native daemon process tokens and provider
credentials remain host-side and are never returned by Bridge APIs.

The Bridge container has no workspace data bind. The selected Node controls
handoffs and later authorized source writes, and its host identity can access every
configured `allowed_roots` entry. A Node compromise is not constrained to one
handoff directory; mount only intended projects under a dedicated host root and do
not use a home/root bind.
Outbound network access is not disabled by this Compose file. Image pixel secrets
are still not redacted. The same configured path remains usable after a
reboot/remount or native-to-container device/inode change; each request
re-validates the current root and still denies symlink, cross-device and
exclusion escapes. No security guarantee is inferred merely from containerization.
