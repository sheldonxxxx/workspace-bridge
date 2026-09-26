# Operations

## State, secrets, backups

Default state: `~/.local/state/workspace-bridge`, mode 0700. Configuration,
SQLite database and the admin-token file are private; `bridge.sqlite3` is mode
0600. In this development phase, Node connection tokens are intentionally stored
as plaintext in Bridge SQLite and runtime adapter tokens in the private Node
SQLite. Normal Manager APIs return only `has_token`, and tokens are excluded
from diagnostics, logs, errors and events. Treat both state databases and project
handoff folders as sensitive; Bridge holds sanitized adapter references while the
Node holds adapter secrets and data-plane state.

Fresh state creates schema v4. This architecture cutover does not migrate older
databases or retain v1 API aliases. If Bridge reports `state_schema_incompatible`,
use a fresh state path; the existing database is left untouched.

Stop the service before a plain filesystem copy; preserve any SQLite WAL/SHM files with the database. Alternatively use an explicitly managed SQLite online backup procedure. Do not copy only a live database file and assume it is consistent. No automatic backup or pruning is configured.

Keep the package and tunnel profiles outside mapped projects. Bridge state cannot
overlap a project. `--state` is a global CLI option, before the subcommand.
Configure each Node's approved `allowed_roots` on that Node host; Bridge cannot
edit them. A workspace remains usable only through its selected Node and fails
closed when that Node is unavailable.

## Revocation

Disable a mapping to reject subsequent calls. Rotate the shared bridge token when it may have leaked; save the new secret in the local tunnel environment and restart tunnel-client. Old credentials no longer authorize new operations across any mapping. Pausing the shared bridge rejects all MCP requests without changing individual mapping states. Re-enabling the bridge restores access to those mappings. A current operation can finish before a serialized management change takes effect.

Shared-token rotation is available in the local manager while serving, or via `workspace-bridge rotate-bridge-token` while stopped. It generates a new token, enables the gateway and does not enable disabled mappings. Update the one tunnel environment and restart its process.

The bridge has no admin-token rotation button. For a compromised admin secret, stop the daemon and perform a reviewed local credential rotation or initialize fresh private state; do not continue exposing projects with a compromised local administrator account. Fresh state does not import old mappings or handoffs automatically.

## Root replacement / moving projects

Mappings authorize a canonical path. The same configured path stays usable
across reboot/remount even when device/inode identity changes; each request
re-validates the current root and still enforces containment, excludes and
scopes. A genuinely missing root fails as unavailable, and a symlink/file
replacement fails closed. Existing mappings, including disabled mappings,
cannot be overlapped. There is no destructive delete/remap operation and no
automatic fallback search for moved repositories; use fresh state with a
preserved archive when a mapping must be recreated.

## Limits and incomplete coverage

The initial implementation targets ordinary code workspaces after dependency/build/large-asset exclusions. A large RapidRAW checkout containing photo libraries, model weights or huge fixtures needs explicit exclusions. Directory-wide omissions are intentional scope, not reviewed content. A scan that exceeds its bounds reports the limit; do not claim complete coverage.

Unreadable, excluded, binary, redacted or oversized content limits manual code
review. No approval gate exists. Changing exclusions affects future browsing, not
a handoff's publication state. Source reads are live; pause external writers and
re-read changed files. No automatic retention cleanup is added: old snapshot blobs
may still consume space after upgrading, even though new handoffs create none.

## Readiness checks

`workspace-bridge doctor` prints concise Overall, Core, Workspaces, Adapters,
Runnable routes, and Git evidence sections. Use
`workspace-bridge doctor --json` for the canonical JSON report and
`workspace-bridge doctor --offline` to guarantee zero adapter/network calls.
The authenticated local-admin `GET /api/diagnostics` endpoint returns the same
schema; `GET /api/diagnostics?offline=1` selects offline mode. Both Doctor and
the API use one server-side evaluator.

Checks use `pass`, `warning`, `action_required`, `failed`, or `unknown`. Overall
severity is failed, action required, unknown, warning, then pass. Doctor exits
1 when overall is failed/action-required; it exits 0 for pass, warning, and
unknown-only reports. Unknown adapter/profile/model freshness is not success.
A route is ready only when the exact workspace/adapter mapping, accessible root,
shared MCP gateway, enabled WorkspaceRoute and
adapter, and the adapter's current security profile all pass. A configured
model policy is validated, but an unconfigured policy is optional governance:
models are then unrestricted by Bridge policy and the runtime chooses its own
default. Write scope is not a run prerequisite — it governs only new handoff
publication and is reported as its own diagnostic — so a read-only workspace
may still have a runnable prepared handoff.
Git Evidence is review-only and never blocks a route.

Listener health, gateway enabled, runtime healthy, and runnable route are distinct.
The Manager reads this canonical report and treats only
`runnable_routes[].ready` for an exact `(workspace_id, adapter_id)` pair as route readiness;
overall health does not gate that pair. If diagnostics are unavailable, the
Manager disables starts and marks route readiness unavailable while retaining
other page data. The Handoff `Start run` action is an authenticated local-admin
wrapper for an existing prepared handoff and delegates to
`service.start_agent_run`; it accepts no free-form prompt or model override.
Offline Doctor opens SQLite read-only, creates no service workers, does not
recover notification `sending` rows, and runs while `serve` holds the process
lock.

## Release identity and compatibility (M4.1)

Every deployed component exposes a bounded content-addressed identity
(contract 1): `{ contract: 1, product: "workspace-bridge",
product_version, component, component_version, build_id: "sha256:<64 hex>" }`.
`build_id` is deterministic over production inputs only; it never uses live
Git state, paths, timestamps, hostnames, tokens, mutable instance IDs, or
environment-only values, and no paths or file inventories are exposed.

Product vs component versions stay distinct: the product version is the
Workspace Bridge release (`0.1.0`); the component version is the component's
own version (Bridge/Node `0.1.0`, Codex `0.1.0`, Pi package `0.1.0`, Manager
`0.1.0`). Adapter/native semantic versions (`adapterVersion`,
`nativeVersion`, protocol/features, Node/adapter revisions) remain separate
fields and are never overloaded by release identity.

Bridge `/api/status` returns both `release` (Bridge core) and
`manager_release` (validated `static/dist/release.json`, or `null` when
missing/invalid rather than a fabricated match). Node `/v1/status` returns
`release` with component `node`; Codex and Pi Runtime Protocol descriptors
carry an additive optional `release`. Descriptors without it stay
protocol-usable at the generic protocol layer (first-party release support
still begins at 0.1.0). A present malformed or unsupported `release` object is
observed as degraded update metadata (`invalid`/`unsupported`) while the
descriptor stays usable for normal runtime operations; it never makes an
otherwise valid Runtime Protocol v1 descriptor unavailable. Protocol-major,
core-feature, and runtime-identity validation stay strict.

The Manager compares its full compile-time Manager identity with
`/api/status.manager_release` (`contract`, `product`, `product_version`,
`component`, `component_version`, `build_id`): any difference shows a
non-destructive
`Manager build mismatch / refresh or rebuild required` warning (an old cached
JS bundle talking to a newer Bridge, including a version-only skew with an
equal build ID); absent/invalid metadata on either side shows
`Manager identity unavailable`, never a false mismatch. Bridge core and
Manager build IDs/versions appear in the Overview status surface (short
`sha256:` prefix, full ID in the title/details).

Canonical diagnostics adds a `release` section. It always emits a Bridge
identity check; live Node checks cover identity present/valid, exact
product-version match, and exact Python-core build-ID match (Bridge+Node use
the same Python core); live adapter checks cover identity present/valid and
product-version match, plus Python-core build-ID match for Codex only (Pi uses
an independent artifact build ID and is never compared to the Python core).
Missing/invalid/unsupported first-party identity from an otherwise
protocol-compatible Node/adapter is `warning` (unsupported development
build; 0.1.0 is the first supported baseline); product/build skew
is `warning`; offline/unobserved remote identity is `unknown`. Release warnings never enter `RunnableRoute.blockers`;
Runtime Protocol compatibility, security, model, and workspace readiness
remain the execution authority.

This is source/package identity, not a container image digest or
code-signing provenance; image/artifact provenance belongs to later
release work.

## Release target manifest and 0.1.0 reset

M4.2A resets every active Workspace Bridge-owned release/component/package
version to exactly `0.1.0` (Python product/package, Bridge/Node components,
Codex component/descriptor, Pi package/`ADAPTER_VERSION`/
`workspaceBridgeRelease` plus lockfiles, Manager/web package plus lockfiles,
Docker image tags/install pin, README/docs display versions, served
`release.json`). Compatibility/schema/protocol counters and upstream
dependencies are unchanged: Runtime Protocol major/minor, release contract 1,
DB/config/state schemas, API versions, Pi native 0.87.0, Codex native version,
tunnel-client/dependency versions. Old `0.x` CHANGELOG headings are pre-reset
development history and are preserved as-is; the new top `v0.1.0` entry
explains the reset.

A deterministic release target manifest pins exact M4.1 release identities
for all five deployed components (`bridge`, `manager`, `node`,
`codex-host-adapter`, `pi-host-adapter`) with `schema_version: 1`, product
`workspace-bridge`, `product_version`, and a content-addressed
`manifest_id: sha256:<64 hex>` over canonical content excluding the ID
itself. Every target `product_version` must equal the manifest
`product_version`; Bridge/Node/Codex Python-core build IDs must match each
other (Pi/Manager use independent artifact build IDs). Generate it with:

```sh
uv run python scripts/build_release_manifest.py [--output manifest.json]
```

The helper uses fixed, bounded local `node` subprocesses to call the existing
Manager (`web/manager-release.mjs`) and Pi (`runtime/pi-host-adapter/release.mjs`)
release helpers; it accepts no arbitrary commands and emits no filesystem
paths, hosts, tokens, or timestamps. It fails clearly when Node or release
metadata is unavailable or invalid.

## Deterministic release bundle (M4.2C1)

`workspace-bridge release build --output <new-dir> [--json]` is the
deterministic release-artifact boundary used by release CI.
It builds a content-addressed bundle from the current checkout, proves every
artifact's embedded release identity matches a freshly generated target manifest,
and exposes it through a typed local CLI. It never installs, restarts, deploys,
contacts live Nodes/adapters, requires Bridge state or admin credentials, creates
Docker images, touches launchctl, or mutates service state. Release verification
consumes a validated artifact bundle rather than the live checkout.

Why a bundle, not the live checkout: the checkout is mutable (edits, untracked
files, timestamps, host paths) while release verification must be reproducible. The bundle pins exact deployable bytes (wheel SHA-256/size, Pi
archive SHA-256/size) to exact M4.1 release identities in a fresh target
manifest, with a content-addressed `bundle_id`/`receipt_id` over canonical safe
metadata. `validate_release_bundle(path)` recomputes hashes, `bundle_id`, strict
manifest validity, component coverage, and embedded identities; corrupt/tampered
bundles fail before any future mutation. This closes the "built from what?"
gap: the bundle proves exactly which bytes a release contains.

Manual updates vs release evidence: routine component updates do NOT transfer
C1 bundles or Pi runtime archives. Host owners install immutable published
versions locally — Node and Codex from PyPI package
`workspace-bridge==<product_version>` via `uv`, Pi from npm package
`workspace-bridge-pi-host-adapter@<product_version>` via `npm`. C1/C1.1
artifacts remain release verification/audit and optional
offline/disaster-recovery material, not update transport.

Source/package identity vs deployable-byte identity: the current Python release
`build_id` is source/package identity — deterministic over production Python and
embedded-skill inputs only (it excludes Manager dist, bytecode, and runtime
noise) and identical for Bridge, Node, and Codex on the same source. The bundle
artifact `sha256` is deployable-byte identity — deterministic over the exact
normalized wheel/archive bytes (sorted entries, fixed modes/timestamps,
repacked wheel, `tar.gz` with `mtime=0`). A source-only change moves `build_id`
without changing Manager bytes; a build-metadata normalization moves artifact
bytes without changing `build_id`. Both are recorded: `bundle.json` lists
component `build_id` values (validated against the target) alongside artifact
`sha256`/`size` values (validated against bytes on disk).

Layout is fixed and bundle-relative: `<output>/target-manifest.json`,
`<output>/artifacts/python-wheel.whl`, `<output>/artifacts/pi-host-adapter.tar.gz`,
`<output>/bundle.json`. `bundle.json` (schema v1) carries `product`,
`product_version`, exact `manifest_id`, deterministic artifact entries (logical
name, `sha256`, `size`, sorted covered components, validated release identities),
and content-addressed `bundle_id`/`receipt_id` (SHA-256 over canonical metadata
excluding itself). No timestamps, absolute paths, hostnames, tokens, environment
values, Git state, or command strings appear in public metadata. The output
directory must be new/empty (non-symlink); existing complete bundles are never
overwritten; temporary work is private and cleaned on failure.

Build order is fixed: Manager `npm run build` from `web/` first (so packaged
`workspace_bridge/static/dist/release.json` is current), then the target manifest
is regenerated AFTER the build to bind final source/generated state, then the
Python wheel (`uv build --wheel`, deterministically repacked with sorted entries
and fixed ZIP metadata) and the Pi archive (exactly the `release.mjs` production
inventory, deterministic `tar.gz` order/modes/`mtime=0`) are built and
independently validated. Wheel validation unpacks to an isolated temp dir and
recomputes the Python-core `build_id` with the package's own enumeration logic
plus the embedded Manager `release.json`, requiring exact equality to target
bridge, node, codex-host-adapter, AND manager identities. Pi validation extracts
to temp and runs the trusted `release.mjs` helper against the extracted data
(never importing untrusted code), requiring exact equality to target
pi-host-adapter identity and exact inventory match. All subprocesses use fixed
typed argv (`uv`, `npm`, `node`) with no `shell=True` and no caller-supplied
command text; archive extraction rejects traversal/symlinks. `deploy
validate --bundle <dir> [--json]` re-runs the same pure validation.

Diagnostics do not prove account authorization, model tool behavior, test
execution, or ChatGPT's handling of a response. `scripts/smoke_mcp.py --url
http://127.0.0.1:8765/mcp` exercises read-only discovery, workspace discovery,
info and directory listing over real local HTTP. It prompts for the shared
bridge token or reads WORKSPACE_BRIDGE_TOKEN; the token is never a command-line
argument. Run `tunnel-client doctor` for the tunnel itself, then validate
discovery and source-write denial and allowed handoff-write behavior in a real
ChatGPT conversation with a nonsensitive sample project.

## Trusted distribution: CI and GitHub releases (M4.2C1.1)

Registry publication is distribution only; accepted bundle/runtime artifact
hashes remain the deployment truth. CI never deploys to live Bridge/Node/
adapters. The private repository is `sheldonxxxx/workspace-bridge`.

Operator flow: update versions (`pyproject.toml`, `workspace_bridge/__init__.py`,
Pi `package.json` `version` + `workspaceBridgeRelease`, web `package.json` as
applicable) so all equal `X.Y.Z`, merge to `main`, then create and publish a
GitHub Release with tag exactly `vX.Y.Z`. Publishing happens only on
`release: published`; branch pushes and pull requests never publish.
`release.yml` is the ONLY registry publishing workflow. It first validates the
tag is exactly `v<project-version>` and that pyproject, Pi package version, and
Pi `workspaceBridgeRelease` all match before any publish job runs.

CI baseline (`.github/workflows/ci.yml`, `contents: read`, no OIDC/write):
Python `3.11`/`3.13` on Ubuntu x64, `3.13` on Ubuntu arm64 (`ubuntu-24.04-arm`,
supported for private repos) and macOS arm64 (`macos-14`), locked installs
(`uv sync --locked --extra test`, `npm ci`); Manager build/unit/lint/format on
Linux x64; Pi `npm ci --omit=dev` + `npm test` on Linux x64/arm64 and macOS
arm64; Docker Bridge natively on `linux/amd64` (`ubuntu-24.04`) + `linux/arm64`
(`ubuntu-24.04-arm`) with image-architecture inspection and bounded container
release-identity smoke. No Playwright browsers in baseline. Current majors:
`checkout@v7`, `setup-node@v7`, `setup-uv@v10`, `upload-artifact@v7`,
`download-artifact@v8`.

Python distribution: `release.yml` runs the accepted C1 build on Linux x64
(`release build` + `release validate`), uploads the exact validated source
bundle as an Actions artifact, then `prepare-pypi-dist` (no OIDC) revalidates
the bundle, verifies wheel SHA-256/size against `bundle.json`, stages the exact
wheel bytes under the standard distribution filename (bytes unchanged), and
uploads only `pypi-dist`. The minimal `pypi` environment job (`id-token: write`,
`contents: read`, needs `release-ready` + `prepare-pypi-dist`) contains only an
artifact download plus `pypa/gh-action-pypi-publish` (no checkout, no `uv sync`,
no project code). No `PYPI_TOKEN`, no rebuild. A `release-ready` barrier
(`contents: read`, no OIDC/write) needs validate + source + all Pi runtimes +
npm-pack + both Docker arches + prepared PyPI dist; no publish or assets start
before it succeeds.

Pi npm package (`runtime/pi-host-adapter`): publishable (no `private:true`),
`repository` exactly `https://github.com/sheldonxxxx/workspace-bridge` with
`directory: runtime/pi-host-adapter`, `publishConfig.access: public`
(`provenance: false` by default because private repos cannot satisfy
provenance), and a strict `files` allowlist of the 16 top-level production
`.mjs` files (plus auto-included `package.json`; `npm pack` proves no `test/`./
`launchd/`/`node_modules`/secrets). No `bin` entry. Release CI always
builds/tests/packs (`npm ci`, `npm test`, `npm pack` → single exact `.tgz`
uploaded as `npm-package` with no OIDC), but the OIDC publish job (environment
`npm`, `contents: read` + `id-token: write`, needs `release-ready` + `npm-pack`)
has no checkout/build/test/pack, disables package-manager caching, keeps
`setup-node` `registry-url` for Trusted Publishing, verifies `npm >=11.5.1`
with a bounded version check (fails instead of installing/upgrading npm), and
publishes the exact `.tgz` with no rebuild: staged by default
(`npm stage publish <tgz> --access public`, deferring 2FA), direct only when
`NPM_PUBLISH_MODE == 'direct'` (`npm publish <tgz> --access public
--provenance=false`). Gated by `NPM_TRUSTED_PUBLISHING_ENABLED == 'true'`.
Never requires `NPM_TOKEN`.

Pi runtime artifacts: C1 `pi-host-adapter.tar.gz` stays the platform-independent
source artifact. Every `build-pi-runtime` leg downloads the source bundle,
validates it with `release validate` BEFORE `tar -xzf`, and only then
extracts/materializes. Release CI derives self-contained per-platform archives
(`linux-x64`, `linux-arm64`, `darwin-arm64`): copy/extract the accepted Pi
production source into a private staging dir, run `npm ci --omit=dev` from the
committed lockfile, run a minimal `piRelease` smoke, then pack production
source + materialized `node_modules` deterministically (sorted walk, `mtime=0`,
`uid/gid=0`, dirs `0755`, files `0755` iff executable else `0644`, relative
in-root symlinks only). Each sidecar binds C1 source Pi SHA-256, Pi release
identity, `package-lock` SHA-256, platform/arch, Node major, runtime SHA/size,
and a content-addressed runtime-artifact ID. Hashes differ across platforms by
design (e.g. `esbuild` binaries). Validation re-hashes, safely inspects/
extracts, checks top-level allowlist (`*.mjs`, `package.json`,
`package-lock.json`, `node_modules`), rejects absolute/out-of-root symlinks,
devices/FIFOs/sockets/traversal, and verifies extracted Pi identity + lock SHA.

Release Docker: `build-docker` natively on `linux/amd64` (`ubuntu-24.04`) +
`linux/arm64` (`ubuntu-24.04-arm`), needs validate + source bundle, validates
the exact source bundle before build, builds the same Dockerfile, requires
Docker-reported architecture to equal the matrix arch, and runs the built
container requiring embedded Bridge + Manager identities to equal the C1 target
manifest. No GHCR push; build/identity validation only, but a required gate.

Release assets: after `release-ready` plus all producers validate, the
`release-assets` job (the ONLY job with `contents: write`, no OIDC) uploads
persistent versioned assets to the ALREADY-published GitHub Release via
`gh release upload <tag> --clobber` with short-lived `GITHUB_TOKEN`: source
`bundle.json`, target manifest, Python wheel, Pi source artifact, plus three
runtime `.tar.gz` + `.json` sidecars (names include version + platform/arch).
Docker success is a gate; no image tarballs are uploaded and no GHCR push
occurs. It never creates a second release or moves tags.

## Release/version compatibility (read-only)

Bridge-first upgrades are an explicit supported state. Bridge and Manager
may update first; compatible Nodes and adapters may lag indefinitely until
the operator chooses a maintenance window. Release skew is informational
update state, not an execution gate.

- Runtime release metadata is decoupled from Runtime Protocol
  compatibility. Protocol-major, core-feature, and runtime-identity checks
  stay strict, but a present malformed or unsupported `release` object
  never makes an otherwise valid descriptor unavailable. It is observed as
  degraded update metadata while normal operations continue.
- Node Protocol compatibility is explicit: Bridge treats a Node as healthy
  only for a bounded `{status: "ok", protocol: 1}` object. Product/build/
  release skew never affects reachability. A Node protocol mismatch
  surfaces as `node_protocol_error`/incompatible and makes affected routes
  unavailable; malformed optional release metadata does not.
- Optional capabilities gate only the capability that needs them. Absence
  of `securityRebind`, steering, events, imageInput, or future optional
  features never disables otherwise supported runs.
- The running Bridge is the active target: target product version is the
  Bridge product version, target Python-core build for Node/Codex is the
  Bridge build ID, Manager target is the Bridge-served Manager identity, Pi
  target is the Bridge product version with product-version-only precision
  (no fabricated Pi build ID).
- `GET /api/system/versions` is read-only and schema-versioned
  (`schema_version: 1`). It performs bounded live Node/adapter observations
  only and never refreshes catalogs, mutates DB/state, creates backups,
  downloads artifacts, or touches locks. Failures are
  per-component; topology enumeration failure fails the whole response
  safely. States are `current`, `update_available`,
  `unsupported_build`, `target_mismatch`, `incompatible`,
  `unavailable`, each with `execution_compatible`, current/target product
  versions, current/target build IDs where known, target precision,
  runtime type/instance ID where relevant, and a bounded reason code.
  Strict numeric `X.Y.Z` lower-than-target is `update_available`, higher
  is `target_mismatch` (never an implicit downgrade); non-comparable
  differences are `target_mismatch`. Same-version Node/Codex build skew is
  `update_available`. Pi on the same product version is `current` when no
  exact target build is known. A reachable Node or successfully parsed
  adapter descriptor with missing/invalid/unsupported first-party release
  identity is `unsupported_build`: 0.1.0 is the first supported
  baseline, while ordinary
  protocol-supported execution remains compatible when protocol proof was
  obtained. Descriptor-fetch failures are classified only from structured
  RuntimeUnsupported/RuntimeUnavailable semantics, never from
  exception-message text. True protocol/core incompatibility is
  `incompatible` (execution false); unreachable/disabled is `unavailable`.
- Manager System/Versions shows the target Bridge version prominently and
  per-component state as routine staged rollout (for example
  `Update available · Compatible`), never a generic system-error banner.
  `unsupported_build` explains the component is operational only where
  protocol-compatible but must be installed onto 0.1.0+ manually;
  it is not presented as supported update-available rollout.
  `target_mismatch` offers no downgrade. Only affected
  routes/features are described as impacted for `incompatible`/`unavailable`.
  No update/apply action exists; the view is read-only and
  existing operations stay usable while version states display.

## Service persistence

On macOS, the native Node is persistent through the per-user LaunchAgent
`com.workspace-bridge.node`; on Linux, through the system unit
`workspace-bridge-node.service`. Neither is a Compose service. The managed
macOS plist is `~/Library/LaunchAgents/com.workspace-bridge.node.plist`, Node
state defaults to `~/.local/state/workspace-bridge-node`, and macOS
stdout/stderr stay in its private `logs/` directory. The managed Linux unit is
`/etc/systemd/system/workspace-bridge-node.service` with output in the system
journal; `User=`/`Group=` keep the Node process non-root as the installing
user. The Node and native Pi/Codex adapters therefore run as the same user
and see the same absolute host workspace paths. This is simpler than a
per-user unit: the system unit starts at boot and survives logout
automatically with no extra boot step.

Use the host-admin CLI without leading sudo; Manager does not control launchd
or systemd:

```sh
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service install
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service status
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service start
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service stop
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service restart
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" service uninstall
```

`install` requires initialized state with mode 0700 and private config/token
files, refuses an unmanaged or modified unit, and is idempotent for exact
managed content, including a retry-safe partial path when only the
state-local manifest exists. On macOS it uses launchd's user-domain
`bootstrap`, `print`, and `kickstart`/`bootout` operations; on Linux the plain
`service install` command runs unprivileged and the backend uses only fixed
`sudo` operations for privileged mutation (private temp plus
`sudo install -o root -g root -m 0644`, `sudo systemctl daemon-reload`, and
`sudo systemctl enable --now` for install; `sudo systemctl start/stop/restart`
and `sudo systemctl disable --now` for lifecycle) with no shell and no
arbitrary sudo argv. Run the command as the Node owner and authorize the
narrow sudo prompts. `status` is
read-only and unprivileged (fixed `systemctl show ... --no-pager`, no sudo)
and distinguishes the managed unit, enabled/running/failed state, configured
host/port, bounded authenticated Node health, and the availability of each
configured root by a bounded label/count summary. A reachable Node with an
unavailable root is transport-healthy but root-degraded; verify root
availability before testing a Manager or Bridge workspace. Linux `status`
never exposes the Node token, unit ExecStart path, sudo command, HOME paths,
raw environment, journal text, or raw systemctl output. Inspect the journal
manually with `journalctl -u workspace-bridge-node.service` when needed.
Linux without systemd/systemctl/sudo privilege fails service management
clearly (`node_service_systemd_unavailable` or
`node_service_privilege_unavailable`) while foreground
`workspace-bridge node --state <state> serve` remains available.

Advanced root mode (intentional only): running the Node as root is supported
when the operator deliberately chooses it. Initialize and manage a separate
root-owned Node state as root; the unit then carries `User=root`/`Group=root`
and the backend executes the same fixed helper argv directly without sudo.
The executable (including any shim and its target) plus both parent chains
must be root-owned and non-writable, otherwise install fails closed. Root
mode grants the Node and its agents full root filesystem authority. Ownership
is strictly isolated with no takeover: `sudo workspace-bridge ...` (or any
root invocation) against an existing user-owned Node state is rejected rather
than converted, a non-root invocation against root-owned state is rejected,
and switching an installed service between root and non-root requires
`service uninstall` first (or a fresh state). There are no `--user`/`--group`
flags and `SUDO_USER` is never consulted.

On macOS, privacy controls can allow the LaunchAgent to start while still
blocking access to a workspace under locations such as `/Volumes/data2`.
Files & Folders, external/removable-storage, or other TCC prompts may apply to
the executable/interpreter and the selected workspace. Grant only the required
access to the actual executable/interpreter when prompted; Full Disk Access is
not mandatory when a narrower permission is sufficient. `status` root
availability followed by a Manager/Bridge workspace check is the verification
path; do not automate or assume the prompt.

`uninstall` removes only the exact managed unit (macOS plist or Linux system
unit plus its Node-state manifest) and preserves Node state, adapters,
workspace bindings, allowed roots, tokens, and logs. On Linux it runs a fixed
`sudo systemctl disable --now`, removes only
`/etc/systemd/system/workspace-bridge-node.service` via the fixed sudo
remove helper, runs daemon-reload, then removes the state-local manifest.
Unsupported operating systems fail explicitly with
`node_service_unsupported_platform`; a Node container is not part of this
milestone.

The safe default Node listen host remains `127.0.0.1` for host-only use. If the
Bridge is in Docker Desktop, initialize the Node with an explicit non-loopback
host such as `--host 0.0.0.0`, then register
`http://host.docker.internal:<node-port>` in Manager. A loopback-only Node is
not assumed reachable from the container. Non-loopback binding requires a host
firewall/private-network review, while Node token authentication remains
mandatory; the Node is never exposed through the MCP tunnel. v0.8 still uses
Docker Compose for Bridge + MCP tunnel only, with restart policy, health check,
non-root UID/GID, and private persistent Bridge state. See DOCKER.md.

Setting `WB_ADMIN_ALLOWED_HOSTS` widens only the admin listener to 0.0.0.0 with
those `Host` values allowed (MCP unaffected); invalid values fail closed.
The tunnel client stays on the host; no Docker socket is mounted into the bridge.

The optional `scripts/run_tunnel.py` helper only launches the official client when manually invoked. It is not imported or callable by the MCP server. Runtime execution is available only through bounded, opt-in Runtime Protocol tools and separate host adapters; the server never exposes a shell or arbitrary command tool.

## Runtime Protocol runs and restart recovery

Agent execution requires a local administrator to enable the
workspace, enable an exact same-Node WorkspaceRoute, and assign its
adapter-specific security binding; a model policy is optional governance per
adapter. Run and
conversation views are local-admin-only; MCP cannot change routes, profiles, or
model policy. The Manager shows the owning Node and adapter name and ID, runtime
type, Bridge run and conversation IDs, handoff, model, immutable security-used
snapshot, state, timestamps, and notification delivery. Run details show current interactions and bounded activity
and execution records. Only live, adapter-provided choices can be submitted.

On startup and during active runs, Bridge reconciles its durable records with the
Runtime Protocol adapter snapshots. It rebinds only operations the adapter
positively identifies as owned. It never replays a prompt or approval. If the
adapter cannot confirm an operation, Bridge records an interrupted or orphaned
outcome and marks any pending interactions stale. Transient adapter failures are
reported as availability errors and do not cause a prompt retry.

Pi and Codex adapters are Node-owned private processes using the same Runtime
Protocol v1 contract. Native daemon listen ports, bootstrap tokens, state paths,
and LaunchAgent/app-server lifecycle remain configured on their hosts. Bridge
Node endpoints/tokens are managed in the local Manager and stored in Bridge
SQLite; Node adapter endpoints/tokens stay in Node SQLite. The Bridge does not
use `WB_RUNTIME_ADAPTERS` or a global `WB_RUNTIME_TOKEN`. Changes apply to the
next request without a Bridge restart. Native process logs are separate from
Bridge logs. Set `WB_LOG_LEVEL`
independently for Bridge and each adapter, and
`WB_TUNNEL_LOG_LEVEL` for the tunnel sidecar. Never enable raw HTTP tunnel logging
(`LOG_HTTP_RAW_UNSAFE`): it may expose sensitive headers or bodies.

The `/health` endpoint is a minimal lock and readiness check; the authenticated
`/v1/descriptor` reports protocol and adapter capabilities. Unsupported features
fail closed. Live `workspace-bridge doctor` checks configured AdapterInstances
with bounded Runtime Protocol requests; `doctor --offline` reads local adapter
configuration and model policy without contacting the host. Runtime Protocol run
notifications use Bridge-owned event and delivery tables; channel failures do not
change run state. The `read_agent_run` `notifications` object shows bounded event
summaries and per-channel delivery status.

## Pi runtime profile

Pi runs natively with the host user's authority, so its security profile is
pre-tool policy rather than an OS sandbox. Profiles control supported file tools,
external paths, protected paths, shell behavior, and session grants through the
Pi host adapter's trusted permission extension. A profile revision is immutable
for each conversation; changes take effect in new conversations. Review each
profile's scope in the local Manager before assigning it. Each adapter instance
has its own profile discovery and model policy even when two instances share the
Pi runtime type. Codex uses a separate
native permission profile with an approval policy and reviewer; the two runtimes'
profile claims are not equivalent. Codex profile discovery and binding are checked
with the exact workspace ID and validated directory. Bridge selects the native
profile ID; Codex owns the effective project/user/managed config layers and the
filesystem/network policy definitions. See
[Codex adapter security profiles](CODEX_ADAPTER.md).

Codex workspaces may instead follow the effective native `config.toml` security
settings. Bridge observes a security-only revision and bounded summary, then
attempts to update an idle conversation before its next turn. A running turn keeps
its captured settings. When Codex cannot represent or confirm an update, Bridge
starts a fresh conversation that resolves the current native configuration. This
mode is distinct from assigning a Bridge profile and is unsupported for Pi.

## Writable handoff notes

Use the general write/edit tools with the default handoff-only policy for small UTF-8 documents. Current hashes
are required for replacements/edits; no forced overwrite, delete or rename is
available. Read back after connection loss or ambiguous disk errors. Writes may
create parent directories, and partial failures can leave empty directories or an
internal staging file after process termination; internal staging files are hidden
from tools. Do not remove such files while a write is active. No automated cleanup
or total handoff-storage quota is provided.

For least privilege, precreate each project's `.workspace-handoff/` with ownership
and write permission for the dedicated service identity; give that identity only
read access to the rest of the project. This release does not configure those OS
permissions for you. Stop the local coding agent before revising its active plan.

## Per-workspace write permissions (v0.5)

The general tools are read_file, write_file and edit_file. Write scope is set only
in the local manager: Exclusions & policy → Write permission → Save and confirm.
The default handoff mode allows notes but not source. none denies every file
mutation including prepare_handoff; workspace allows bounded permitted source
text. Both old and new mappings default to handoff. Scope changes take effect on
subsequent calls with no tunnel/schema change or restart. OS permissions remain
an additional requirement; enabling scope does not grant filesystem privileges.
See FILE_ACCESS.md for mode/ACL/ownership limits.
