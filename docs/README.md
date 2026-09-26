# Documentation index

Start here and follow only the guide that matches your role. Source, CLI
parsers, package metadata, tests, and Compose files are the behavioral
authority; prose never overrides them.

## Installers and operators

- [Setup](SETUP.md) — the single canonical human installation and
  configuration guide: evaluation and persistent paths, Bridge, Node,
  adapters, Manager routes, tunnel/client connection, verification, updates,
  uninstall. macOS and Linux notes are inline.
- [Docker](DOCKER.md) — container Bridge control-plane detail. Read after
  Setup, not instead of it.
- [Operations](OPERATIONS.md) — state and backups, service management,
  manual upgrades, release identity, logging, support bundles, notifications,
  recovery, troubleshooting, and validation status.

## Agents asked to install or configure

- [Agent setup](AGENT_SETUP.md) — deterministic runbook an AI/coding agent
  follows to prepare Workspace Bridge for a user. Starts with host discovery,
  uses only supported commands, keeps secrets out of chat, separates
  agent-runnable commands from required user/manual UI actions, and states
  exact stop/ask points with checkpoints and rollback rules.

## Contributors (human and agent)

- [Contributing](../CONTRIBUTING.md) — contributor setup, repo structure,
  test commands, docs expectations, PR and check guidance.
- [AGENT_SETUP](AGENT_SETUP.md) is the install runbook; this repository's
  contributor instructions for agents live in [AGENTS.md](../AGENTS.md).
  Do not treat end-user setup as contributor setup.

## Architecture and security

- [Architecture](ARCHITECTURE.md) — component responsibilities, state
  ownership, authority and data paths, trust boundaries, run sequence,
  native-vs-container deployment, lifecycle, logging, extension points, and
  intentional non-goals, with Mermaid diagrams.
- [Security](SECURITY.md) — threat model, credential scope, implemented
  controls, limits, and deployment rules.
- [Runtimes](RUNTIMES.md) — consolidated Pi and Codex runtime notes:
  installation surface, service lifecycle, security-profile differences,
  continuation behavior, model policy, and troubleshooting.

## Protocol and tool reference

- [MCP tools](MCP_TOOLS.md) — exact tool names, arguments, and behavior.
- [Runtime Protocol](RUNTIME_PROTOCOL.md) — Runtime Protocol v1 contract
  between Bridge, Node, and adapters.
- [File access](FILE_ACCESS.md) — read/write interface and write scopes.
- [Image support](IMAGE_SUPPORT.md) — image reads, limits, privacy, and
  client validation.
- [Handoff protocol](HANDOFF_PROTOCOL.md) — planning and review workflow
  contract (three documents, manual and automated loops).
- [References](REFERENCES.md) — external interfaces consulted.
