# AGENTS.md

Instructions for AI/coding agents contributing to this repository. For
installing or configuring Workspace Bridge on behalf of a user, follow
[docs/AGENT_SETUP.md](docs/AGENT_SETUP.md). For human contributor detail,
see [CONTRIBUTING.md](CONTRIBUTING.md).

## Source map

- `workspace_bridge/` — MCP service, Manager API, CLI, Node service,
  adapter lifecycle, run coordination, diagnostics, notifications.
- `web/` — Manager UI (build output packaged under
  `workspace_bridge/static/dist/`).
- `runtime/pi-host-adapter/` — Pi adapter (npm package).
- `tests/test_*.py`, `runtime/pi-host-adapter/test/*.test.mjs`,
  `web/tests/*.spec.ts` — test suites.
- `docs/` — public docs indexed by [docs/README.md](docs/README.md).
- `.workspace-handoff/` is internal ignored audit state: never rewrite,
  delete, or scan it in tests. Never touch vendored `.agents/skills`
  content.

## Mandatory architecture invariants

- The Bridge never opens workspace roots directly and never falls back to a
  local checkout; the authoritative Node owns files, Git, handoffs, and
  adapter secrets under its host `allowed_roots` ceiling.
- A runtime type never selects a destination; only an exact same-Node
  `(workspace_id, adapter_id)` route with its security binding admits a run.
- Write scope governs publication and mutation; route enablement governs
  runs. One never implies the other.
- Bridge never replays prompts or approvals. Recovery rebinds only what the
  adapter positively identifies; everything else becomes interrupted or
  orphaned with stale interactions.
- Tokens stay out of APIs, diagnostics, logs, errors, events, docs, and
  chat. One-time token output stays local.
- No shell, Git mutation, per-chat ACL, automatic updater, or automatic
  support upload. The Manager stays loopback-only and is never tunnelled.

## Inspect before edits

Read the relevant source, callers, and tests with the normal file tools
before changing behavior. Check current `--help` output and package `bin`
entries before documenting a command. Cross-check claimed tool names,
counts, scopes, route admission, continuation rules, service paths, logging
bounds, and image limits against `workspace_bridge/` and tests. If docs
expose a real source inconsistency, report it instead of changing
architecture outside the task scope.

## Test commands

Prefer focused checks first, then the full suite once. Preserve real failure
status; do not mask exits. Do not install dependencies or browsers
implicitly; report environment-blocked checks instead.

```sh
uv run pytest -q
```

```sh
cd runtime/pi-host-adapter && npm test
```

```sh
cd web && npm run lint && npm run format:check && npm run build
```

CLI smoke that mutates nothing:

```sh
uv run workspace-bridge --help
uv run workspace-bridge doctor --offline
```

## Documentation consistency

Keep the public information architecture (see [docs/README.md](docs/README.md)
and [CONTRIBUTING.md](CONTRIBUTING.md)): no internal milestone labels,
diary prose, obsolete paths, personal data, or secrets in tracked docs.
After renames or removals, grep Markdown for old filenames and fix every
inbound link. Use generic paths and valid shell in code blocks.
