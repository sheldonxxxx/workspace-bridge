# Security policy

Workspace Bridge connects a remote model to files and agents on your machine,
so we treat security reports as the highest priority.

## Supported versions

Only the latest release receives security fixes. Upgrade with
`uv tool upgrade workspace-bridge` (and the Pi adapter with
`npm install -g workspace-bridge-pi-host-adapter`), then restart affected
services.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability.

Report it privately through
[GitHub private vulnerability reporting](https://github.com/sheldonxxxx/workspace-bridge/security/advisories/new).
Include the affected version, your deployment shape (native or Docker; which
runtimes), reproduction steps, and the impact you observed.

Never include real tokens, passwords, private source, or unreviewed support
bundles in a report. Use synthetic values; we will ask for anything else we
need.

This is a small project; we will acknowledge reports as soon as we can and
agree a disclosure date with you once a fix is available.

## Scope

In scope: escaping a workspace or Node `allowed_roots` boundary, bypassing
write scope or route admission, running an agent without an enabled route,
bypassing a runtime security profile, leaking tokens into APIs, logs,
diagnostics, or events, and reaching the Manager from outside loopback.

The intended threat model and its known exclusions (for example, a hostile
local user with the service user's permissions, or prompt injection that a
local agent obeys) are described in [docs/SECURITY.md](../docs/SECURITY.md).
