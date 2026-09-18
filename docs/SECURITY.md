# Security model

## Intended threat model

Restrict a remote ChatGPT tool connection to explicitly enabled project mappings, with no command execution and handoff-only writes by default. Only local administrators may explicitly enable bounded source-text writes. Defend against path traversal, accidental scope expansion, unsafe filesystem entries, cross-workspace path/job confusion, browser cross-origin access and accidental treatment of agent claims as verified results.

Not covered: a hostile local user/process with the service user's permissions; root/admin compromise; malicious browser extensions; compromised Python dependencies or tunnel-client; vulnerabilities in the host OS; comprehensive secret detection; prevention of every prompt-injection attempt; or ensuring that the local coding agent obeys a handoff. A prompt cannot substitute for the local agent's sandbox.

## Shared credential scope

One gateway credential authorizes every enabled mapping. Each project call requires `workspace_id`, and each path/job is checked inside that selected workspace. This prevents accidental identifier mixing but is **not per-chat or per-user authorization**: a chat with the shared connection may deliberately select another enabled project. Restrict tunnel/app sharing, disable unnecessary mappings, and use global pause or shared-token rotation when needed. No OAuth identity or per-client ACL is implemented.

The first v0.1 migration disables all existing mappings once and starts the shared gateway unconfigured. The administrator must review and re-enable intended projects. Old project routes/tokens are rejected; retained database hashes no longer authenticate requests.

## Implemented controls

- Two loopback listeners, no proxy-header trust, strict Host/Origin checks, separate admin/shared-bridge credentials. Manager operations never appear as MCP tools.
- Explicit project-parent allowlist; immutable root mappings; overlapping mappings rejected even if disabled; device/inode pinning; current allowed-parent validation; default-disabled registration.
- Relative POSIX paths only. No absolute reads, `..`, ambiguous separators or paths outside the selected workspace. Current write scope further restricts writes. Each opened ancestor uses `O_NOFOLLOW`; special files, hardlinks and cross-device traversal are rejected.
- General reads are available in all modes. Local write scope is `none`, `handoff` (default), or `workspace`; it is reloaded for each serialized call, and no MCP argument can override it. `none` denies `prepare_handoff` too. Create-only by default; existing files require their current hash. No delete, rename, shell, subprocess, test or Git invocation in the server package.
- All writes reject secret-like/binary/control content and administrator exclusions. Staging uses private files and complete-content publication, not in-place truncation. Rechecks catch ordinary target/parent changes; they are not a sandbox or a portable atomic compare-and-swap against hostile external writers. Parent folders may remain after a later write failure; read back after an ambiguous I/O failure.
- Built-in sensitive-name and build-directory exclusions. Additional administrator globs are conservative and case-insensitive. Repository ignore files cannot grant access or relax policy.
- Request type/size bounds, bounded reads/search, output budget, concurrency gate and quotas. File changes during reads and stale context hashes fail rather than silently succeeding.
- Generated planning document hashes expose local modification when a handoff is read. No runtime test execution, automatic approval gate, stored verdict, or claim of independent verification is provided.
- No third-party scripts, escaped text rendering, a restrictive CSP, no browser persistent token storage, and no contents/keys in operational events. The UI intentionally does not render project Markdown as HTML.

## What is excluded by default

VCS internals, common package/build/cache directories, credential directories, `.env*`, private-key/certificate bundle formats, common token/credential filenames, SQLite/databases and log files. Exact controls are in `security.py`.

These exclusions are intentionally conservative: even `.env.example`, some public-key-looking names, and generated files may be unavailable. Do not weaken access protection simply to satisfy an agent. For a needed safe example, manually create a sanitized document with a neutral filename and explicitly review its contents before exposure.

An ignored file can still be readable if it is not excluded by bridge policy. Never assume `.gitignore` protects secrets. Denied directories are not traversed or counted as audited content. Registered mappings cannot point into a denied subtree to bypass the policy.

## Search-specific bounds

Globs filter a safe inventory and cannot resolve arbitrary files. Search cursors are HMAC signed and bound to workspace, root identity, policy, query and observed file metadata. A stale cursor requires restarting; a valid cursor is not a long-lived content snapshot. Queries run over redacted text and regex matching has a timeout. Compilation, filesystem latency and other in-process operations are not a hardened resource sandbox; stronger multi-user isolation requires OS-level limits and a different authorization model. `.gitignore`/`.ignore` never control bridge access.

## Redaction is not a DLP guarantee

The heuristic recognizes several common token/password/private-key patterns. It can miss unusual secrets and falsely redact legitimate code. Deny policies and choosing appropriate projects are the primary safeguards. Do not put credentials into plans or reports. Redacted source is incomplete evidence; report that limitation during review.

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

Never tunnel or reverse-proxy the admin listener. Never use an admin token as a bridge token. For stronger isolation use a dedicated service identity that can read only selected projects and write only private state plus their handoff directories. Configure and test those permissions yourself before treating them as enforced. Run only one bridge process per state directory.

A disabled mapping retains its history. Token rotation revokes the old token for subsequent calls. Neither action erases copies already returned to ChatGPT, cancels completed reads, or necessarily interrupts work already holding the service lock.

## Review boundary

The user supplies the agent reply; ChatGPT reads current source and reports its
assessment in the conversation. There is no retained before-state, complete change
inventory, tamper-proof review receipt or automated test verification. Exclusions,
binary files, redaction and unreadable content limit what can be assessed.

Older state may still contain source snapshots and historical evidence retained
for a non-destructive upgrade. v0.3+ does not load or expose those through the removed
tools and does not create new ones. Backups remain sensitive.

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

## Local policy control

Upgrades add write_scope=handoff without broadening any mapping. The manager requires
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

## Docker deployment — v0.7

The container uses a non-root host UID/GID, read-only root filesystem, a bounded
noexec/nosuid/nodev temporary filesystem, dropped capabilities and no-new-privileges.
No privileged mode, Docker socket, host network, cloud API keys or tunnel credential
is included. Both published ports are explicitly host-loopback only; use a current
Docker Engine (28+ avoids the documented old localhost-publication L2 exposure).
Never attach untrusted services to its Compose network or add a public reverse proxy.

The project-parent bind is writable so handoffs and later authorized source writes
can reach the host. All mounted files, including disabled mappings, are visible to
the container process; only the MCP layer enforces enabled mappings, exclusions and
write scope. A process compromise is not constrained to the handoff directory.
Mount only intended projects under a dedicated parent; no home/root binds. The
optional read-only source recipe in DOCKER.md is a separate hardening choice and
must be tested on the target filesystem; cross-device traversal stays denied.
Outbound network access is not disabled by this Compose file. Image pixel secrets
are still not redacted. Native-to-container device/inode identity changes are not
automatically trusted; preserve the old state and use a reviewed migration or fresh
Docker mappings. No security guarantee is inferred merely from containerization.
