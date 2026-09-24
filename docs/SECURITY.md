# Security model

## Intended threat model

Restrict a remote ChatGPT tool connection to explicitly enabled project mappings, with no arbitrary command execution and handoff-only writes by default. Only local administrators may explicitly enable bounded source-text writes and, separately, opt-in Runtime Protocol agent execution. Defend against path traversal, accidental scope expansion, unsafe filesystem entries, cross-workspace path/job/conversation confusion, browser cross-origin access and accidental treatment of agent claims as verified results.

Not covered: a hostile local user/process with the service user's permissions; root/admin compromise; malicious browser extensions; compromised Python dependencies or tunnel-client; vulnerabilities in the host OS; comprehensive secret detection; prevention of every prompt-injection attempt; or ensuring that the local coding agent obeys a handoff. A prompt cannot substitute for the local agent's sandbox.

## Shared credential scope

One gateway credential authorizes every enabled mapping. Each project call requires `workspace_id`, and each path/job is checked inside that selected workspace. This prevents accidental identifier mixing but is **not per-chat or per-user authorization**: a chat with the shared connection may deliberately select another enabled project. Restrict tunnel/app sharing, disable unnecessary mappings, and use global pause or shared-token rotation when needed. No OAuth identity or per-client ACL is implemented.

## Implemented controls

- Two loopback listeners, no proxy-header trust, strict Host/Origin checks, separate admin/shared-bridge credentials. Manager operations never appear as MCP tools.
- Explicit project-parent allowlist; canonical configured-root boundary; overlapping mappings rejected even if disabled; current allowed-parent validation; default-disabled registration. The same configured path stays usable across reboot/remount even when device/inode identity changes; each request still re-validates the current root and enforces containment.
- Relative POSIX paths only. No absolute reads, `..`, ambiguous separators or paths outside the selected workspace. Current write scope further restricts writes. Each opened ancestor uses `O_NOFOLLOW`; special files, hardlinks and cross-device traversal are rejected.
- Local write scope is `none`, `handoff` (default), or `workspace`; it is reloaded for each serialized call, and no MCP argument can override it. `none` denies `prepare_handoff` too. Create-only by default; existing files require their current hash. No delete, rename, shell, or arbitrary command surface is exposed as an MCP tool. Two read-only Git evidence tools invoke only fixed status/diff operations with bounded time/output, no pager, external diff/textconv, hooks, network transports, terminal prompting, or repository mutation.
- Agent execution requires both the workspace agent switch and an explicit grant for the selected runtime, each defaulting to OFF. It is not implied by workspace enablement or `write_scope`; MCP has no tool to change it. `start_agent_run` is handoff-bound, accepts no free-form prompt or path, resolves an opaque model selector against that runtime's admin-enabled allowlist plus default, and fails closed without a policy. There is no arbitrary command endpoint. Runtime adapters are private host processes.
- Runtime Protocol v1 binds each conversation to one workspace, native runtime and immutable security profile revision. Pi enforces its trusted permission extension in an isolated profile; Codex enforces its native sandbox and approval policy in a dedicated app-server process. These mechanisms make different claims. The Codex read-only profile denies native approval escalation automatically. Interactions require a live exact request, not merely a persisted ID.
- Local administrators can create, edit, assign, and delete custom runtime security profiles. Pi profiles expose the validated file-tool, external path, protected path, shell, and session-grant controls; Codex profiles expose native sandbox, approval policy, and reviewer choices. The adapter owns and validates each definition. An edit changes its revision and requires a new conversation; an assigned profile cannot be deleted. Pi shell Allow and Codex full access can run with broad host authority, so the manager labels those choices explicitly.
- Per-runtime model policy (enabled selectors, one mandatory default, and optional per-model reasoning defaults) and the Bridge-owned run list (`/api/runs`, newest first, bounded) are local-admin-only; no MCP tool can enable models, change these defaults, bypass policy, or act on arbitrary runtime conversations.
- All writes reject secret-like/binary/control content and administrator exclusions. Staging uses private files and complete-content publication, not in-place truncation. Rechecks catch ordinary target/parent changes; they are not a sandbox or a portable atomic compare-and-swap against hostile external writers. Parent folders may remain after a later write failure; read back after an ambiguous I/O failure.
- Built-in sensitive-name and build-directory exclusions. Additional administrator globs are conservative and case-insensitive. Repository ignore files cannot grant access or relax policy.
- Git evidence requires a direct real `.git` directory and rejects linked worktrees, bare repositories, symlinked/hardlinked metadata, shared object stores and external config includes. Changed paths are filtered through workspace exclusions before any patch command; denied paths are represented by aggregate counts only. Patch text uses the normal secret redactor. Status hashes bind pagination/diffs to the filtered current state, but do not attribute edits to an agent or user.
- Request type/size bounds, bounded reads/search, output budget, concurrency gate and quotas. File changes during reads and stale context hashes fail rather than silently succeeding.
- Generated planning document hashes expose local modification when a handoff is read. No runtime test execution, automatic approval gate, stored verdict, or claim of independent verification is provided.
- No third-party scripts, escaped text rendering, a restrictive CSP, no browser persistent token storage, and no contents/keys in operational events. The UI intentionally does not render project Markdown as HTML.

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
runtime and run ids, timestamps, and allowlisted request kind/action metadata. It
does not persist prompts, results, source, commands, external paths, arbitrary
provider payloads, webhook endpoints, or tokens in notification tables. The
`WB_DISCORD_WEBHOOK_URL` value is read from local process configuration and held
only by the Discord adapter. Discord payloads suppress mentions and contain the same
metadata-only event envelope. Channel result codes/details are bounded and
redacted; raw HTTP bodies are discarded. Delivery failures cannot change run state,
and status/read APIs expose channel ids and delivery summaries without secrets.

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
| HTTP request body | MCP: 1 MiB; local management: 96,000 bytes; 10-second body timeout |
| Handoff text mutation | 256 KiB final/previous file; JSON escaping also counts against the HTTP budget |
| Source / artifact page | Up to 400 / 200 lines respectively |
| Handoffs | 100 per workspace |
| Private-state quota gate | 512 MiB estimated SQLite allocation |
| Operational events | Most recent 10,000 |
| Concurrent accepted tool work | Up to 4; service mutations/reads serialized |

The SQLite quota is a preventive allocation gate, not an OS disk quota; WAL/journal overhead and races with non-service processes are not a strict byte cap. The process should also run with OS-level resource limits for an untrusted multi-user deployment. Very large workspaces need deliberate exclusions or narrower browsing scope.

## Deployment rule

Never tunnel the admin listener. Never use an admin token as a bridge token. For stronger isolation use a dedicated service identity that can read only selected projects and write only private state plus their handoff directories. Configure and test those permissions yourself before treating them as enforced. Run only one bridge process per state directory.

The admin listener is loopback-only by default. Setting `WB_ADMIN_ALLOWED_HOSTS`
(admin only; MCP is unaffected) is the sole opt-in remote-admin path: a
comma-separated list of bare hostnames/IPs (no ports, schemes or wildcards)
that widens only the admin listener to `0.0.0.0` and allows those `Host` values
(`http` + `https` origins). Empty (default) keeps loopback-only and fails
closed on invalid values. Prefer SSH port-forwarding or VPN; plain HTTP bears
the admin token in clear, so use TLS termination and firewall rules when remote
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

Agent adapters are private host processes configured through
`WB_RUNTIME_ADAPTERS` and protected by `WB_RUNTIME_TOKEN`. They bind to host
loopback, have no published container port, and do not receive provider
credentials from Bridge. Without the token, operational requests fail closed;
`/health` exposes only readiness and lock state.

Each conversation is bound to one enabled workspace, runtime, and immutable
security profile revision. Bridge rechecks the workspace grant, prepared handoff,
profile binding, and model policy before a run. Responses are limited to live,
adapter-provided interactions and are revalidated by the adapter. Pi and Codex
profiles use different native security mechanisms. Neither a runtime profile nor
a workspace binding should be described as an OS sandbox unless the specific
runtime provides that guarantee. Pi runs with the host user's authority; review
its file, external-path, protected-path, and shell controls before allowing writes.

## Diagnostics and Doctor

The local-admin diagnostics API requires the admin bearer/session authentication.
It returns stable check codes and bounded workspace/runtime metadata, but never
adapter URLs, credentials, raw environment values, prompts, tool arguments,
provider payloads, or full external paths. Runtime failure text is reduced to
fixed safe summaries. External tunnel and ChatGPT connectivity is unobserved and
must not be inferred from an enabled local gateway.

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

## Image decoding — v0.6

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

## Docker deployment — v0.8

The container uses a non-root host UID/GID, read-only root filesystem, a bounded
noexec/nosuid/nodev temporary filesystem, dropped capabilities and no-new-privileges.
No privileged mode, Docker socket, host network, cloud API keys or tunnel credential
is included. Both bridge ports are explicitly host-loopback only; the native Pi
host adapter has **no** Compose service and publishes **no** port. Use a current
Docker Engine (28+ avoids the documented old localhost-publication L2 exposure).
Never attach untrusted services to its Compose network or add a public reverse proxy.
The bridge reaches private host adapters through the `WB_RUNTIME_ADAPTERS` URL map
(`host.docker.internal` on Docker Desktop/OrbStack) or explicit reachable host
URLs (Linux Engine). Provider credentials stay on the host only and are never
logged or returned.

The project-parent bind is writable so handoffs and later authorized source writes
can reach the host. All mounted files, including disabled mappings, are visible to
the container process; only the MCP layer enforces enabled mappings, exclusions and
write scope. A process compromise is not constrained to the handoff directory.
Mount only intended projects under a dedicated parent; no home/root binds. The
optional read-only source recipe in DOCKER.md is a separate hardening choice and
must be tested on the target filesystem; cross-device traversal stays denied.
Outbound network access is not disabled by this Compose file. Image pixel secrets
are still not redacted. The same configured path remains usable after a
reboot/remount or native-to-container device/inode change; each request
re-validates the current root and still denies symlink, cross-device and
exclusion escapes. No security guarantee is inferred merely from containerization.
