# Repository Guidelines

## Project Structure & Module Organization

`workspace_bridge/` contains the Python MCP service, management API, security policy, CLI, and runtime coordination. `web/` is the React/TypeScript manager; its Vite build goes to `workspace_bridge/static/dist/` for packaging. `runtime/pi-host-adapter/` contains the native Node.js Pi adapter. Python tests live in `tests/test_*.py`, adapter tests in `runtime/pi-host-adapter/test/*.test.mjs`, and browser tests in `web/tests/*.spec.ts`. See `docs/` for architecture, operations, and security details; `scripts/` holds setup and smoke utilities.

## Build, Test, and Development Commands

- `uv sync --extra test` creates the project environment and installs the service with Python test dependencies from `uv.lock`.
- `uv run pytest -q` runs the Python suite. `uv run workspace-bridge serve` starts the local service after `uv run workspace-bridge init --allow-parent <projects-dir>`.
- In `web/`, run `npm ci`, `npm run dev` for the Vite manager, and `npm run build` to refresh packaged assets. Run `npm run lint` and `npm run format:check` for frontend checks.
- In `runtime/pi-host-adapter/`, run `npm ci` and `npm test` for the Node test suite. Run `npm run test:e2e` from `web/` for Playwright browser tests; installed Chrome is required.

## Coding Style & Naming Conventions

Use four spaces and `snake_case` for Python functions and modules. Follow existing TypeScript/React patterns: two-space indentation, `PascalCase` components, and `camelCase` functions. Node adapter files use ES modules (`.mjs`). Keep API inputs explicitly validated and security decisions in service or adapter code. Format frontend files with Prettier and check them with Oxlint using the scripts above; follow nearby Python and adapter formatting where no formatter is configured.

## Testing Guidelines

Use pytest and `pytest-asyncio` for Python, `node:test` for the adapter, and Playwright for manager flows. Name new tests `test_*.py`, `*.test.mjs`, or `*.spec.ts` in their respective directories. Add focused coverage for changed behavior, especially path boundaries, credentials, runtime permissions, and API responses. No coverage percentage is configured.

## Commits & Pull Requests

Recent commits favor concise subjects such as `feat(pi): ...`, `refactor: ...`, and `feat!: ...` for breaking changes. Keep commits scoped. In pull requests, explain the behavior change, affected boundaries, and checks run; link the relevant issue when one exists and include screenshots for manager UI changes. Preserve unrelated worktree changes.

## Security & Configuration

Keep `.env`, tokens, private state, and tunnel profiles out of commits. Start from `.env.example`; read `docs/SECURITY.md` before changing access, write scopes, or runtime policy. Keep the management listener local and expose only the intended MCP endpoint through a tunnel.
