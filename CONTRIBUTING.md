# Contributing

Human contributor guide for Workspace Bridge. Agents contributing to this
repository follow [AGENTS.md](AGENTS.md) plus the relevant sections below.
End-user installation is covered by [docs/SETUP.md](docs/SETUP.md), not
here.

## Repo structure

- `workspace_bridge/` — Python MCP service, Manager API, security policy,
  CLI (`cli.py`), Node service (`node_*.py`), adapter lifecycle
  (`adapter_*.py`), run coordination, diagnostics, notifications, release
  and support-bundle helpers.
- `web/` — React/TypeScript Manager. Vite builds into
  `workspace_bridge/static/dist/` for packaging.
- `runtime/pi-host-adapter/` — native Node.js Pi adapter (npm package
  `workspace-bridge-pi-host-adapter`).
- `tests/test_*.py` — Python suite. Adapter tests live in
  `runtime/pi-host-adapter/test/*.test.mjs`; browser tests in
  `web/tests/*.spec.ts`.
- `docs/` — public documentation, indexed by [docs/README.md](docs/README.md).
- `scripts/` — setup and smoke utilities (for example `configure_docker.py`,
  `smoke_mcp.py`, `build_release_manifest.py`).

## Setup

Treat `uv` as the canonical Python runner. Prefer `uv run python ...`,
`uv run pytest ...`, and `uv run workspace-bridge ...`.

```sh
uv sync --extra test
```

In `web/`, run `npm ci` for dependencies. In `runtime/pi-host-adapter/`,
run `npm ci` for dependencies.

## Test commands

Run the focused checks first, then the full suite once. Do not rerun
greps, builds, or suites unless a failure or source change requires it.

```sh
uv run pytest -q
```

```sh
cd runtime/pi-host-adapter && npm test
```

```sh
cd web && npm run lint && npm run format:check && npm run build
```

Playwright browser tests (`npm run test:e2e` from `web/`) need versioned
browser binaries. Before a long run, verify the browser expected by the
installed Playwright version can launch. If it reports a missing executable
or cache mismatch, stop that track and report it; do not install browsers
without explicit authorization. `npm run build` must be rerun after UI
changes so packaged assets stay current.

CLI smoke that does not mutate live services:

```sh
uv run workspace-bridge --help
uv run workspace-bridge doctor --offline
uv run workspace-bridge release --help
```

## Docs expectations

- Source, CLI parsers, package metadata, tests, and Compose files are the
  behavioral authority. Verify every setup command against current
  `--help` output and package `bin` entries before documenting it.
- Keep the public information architecture: README as landing page,
  `docs/SETUP.md` as the single human install guide,
  `docs/AGENT_SETUP.md` as the agent runbook, `docs/ARCHITECTURE.md` for
  authority and boundaries, `docs/RUNTIMES.md` for Pi/Codex/Claude Code specifics,
  `docs/OPERATIONS.md` for operator behavior, and the remaining files as
  focused references.
- No internal milestone labels, implementation-slice names, development
  diary, obsolete setup paths, personal paths or IPs, job IDs, private
  state examples, or secret values in tracked docs. Diagrams must render on
  GitHub Mermaid and contain no secrets.
- Cross-links are relative internal links. After any rename or removal,
  grep all Markdown for the old filename and fix every inbound link. Do not
  leave redirect stubs.
- Use generic safe paths (`$HOME`, `/path/to/...`) and valid shell syntax
  in code blocks.

## Pull requests and checks

Keep changes scoped. Explain the behavior change, affected boundaries, and
checks run; link the relevant issue when one exists and include screenshots
for Manager UI changes. Preserve unrelated worktree changes. Run the
acceptance-relevant checks as separate commands and preserve real failure
status: do not append unrelated successful commands that mask a failure
exit code.

## Security and reporting boundaries

Keep `.env`, tokens, private state, and tunnel profiles out of commits.
Start from `.env.example`; read [docs/SECURITY.md](docs/SECURITY.md) before
changing access, write scopes, or runtime policy. Keep the management
listener local and expose only the intended MCP endpoint through a tunnel.
Report vulnerabilities privately as described in the
[security policy](.github/SECURITY.md), never in public issues or PRs.

Do not commit, push, tag, publish, deploy, rotate credentials, restart live
services, or edit handoff documents as part of a docs or code change unless
the task explicitly authorizes it. If docs expose a real source
inconsistency, report it instead of silently changing architecture outside
the task scope.
