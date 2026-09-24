# Codex host adapter security

The manager offers two distinct Codex security sources for a workspace:

- **Use Codex config (config.toml)** stores the binding source `runtime-config`.
  It follows the current effective Codex security configuration for the exact
  workspace. Its observed revision may change without requiring reassignment.
- **Use Workspace Bridge profile** stores a profile ID and effective revision.
  A conversation created with that profile remains pinned to that immutable
  revision; changing the workspace assignment affects later conversations.

## Workspace Bridge profiles

Bridge stores a small wrapper around a native Codex permission-profile ID:

~~~json
{
  "permissions": ":workspace",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "user"
}
~~~

The ID can be a built-in selector such as `:read-only`, `:workspace`, or
`:danger-full-access`, or a named profile reported for the exact workspace.
Bridge's profile editor exposes only the wrapper. Codex owns permission
definitions, filesystem rules, network/domain and socket rules, inheritance,
project trust, managed requirements, and config layering.

The adapter checks native profiles against the validated workspace directory,
reads the effective config with `config/read(includeLayers=false)`, and reads
managed requirements. The effective security inputs contribute to an opaque
revision; raw rules, paths, domains, sockets, proxy details, and managed config
are not returned through Runtime Protocol or stored in a conversation summary.

Profile-bound `thread/start` and `thread/resume` use `permissions` and omit the
mutually exclusive legacy `sandbox` field. The adapter confirms the active
permission-profile ID when Codex reports it. An unavailable profile or changed
effective profile revision prevents an old profile-bound conversation from
being reused.

For compatibility, a stored legacy Bridge wrapper maps one-to-one:

| Stored sandbox value | Native permission selector |
| --- | --- |
| `read-only` | `:read-only` |
| `workspace-write` | `:workspace` |
| `danger-full-access` | `:danger-full-access` |

New profiles and saves use only the `permissions` form. Existing conversations
remain bound to the profile revision they were created with.

## Following current Codex config

For `runtime-config`, the adapter reads `permissionProfile/list(cwd)`, effective
`config/read(includeLayers=false)`, and `configRequirements/read`. Its opaque
security revision covers permission profile definitions and availability,
the selected default permission profile, approval and reviewer settings,
legacy sandbox inputs, relevant trust inputs, and managed constraints. Model,
display, and other unrelated preferences do not change this revision.

Initial `thread/start` omits `permissions`, `sandbox`, `approvalPolicy`, and
`approvalsReviewer`; Codex resolves its native layered configuration. Bridge
confirms the returned active profile, approval policy, reviewer, and compatible
provenance before recording the conversation's applied revision. The stored
summary contains only an active profile ID when known, approval category,
reviewer, and provenance (`named-profile`, `implicit/default`, or
`legacy-sandbox`).

Before every later turn on an existing runtime-config conversation, the
adapter checks that the thread is idle and re-reads the workspace's effective
security revision. An unchanged revision reuses the thread. For a changed
native permission profile, the adapter calls
`thread/settings/update(permissions=<current-id>)`, including when a named
profile keeps the same ID but its definition changed. Approval or reviewer
changes update those fields without selecting a new permission profile when
the profile state is unchanged. The adapter waits for
`thread/settings/updated` and confirms the active profile, approval policy, and
reviewer before saving the new applied revision. A running turn keeps the
settings it captured when it began.

Bridge starts a fresh native conversation for the next run when a change uses
legacy sandbox semantics that cannot be represented exactly, or when a native
update is unsupported, rejected, unconfirmed, blocked by managed requirements,
or reports a different effective state. The replacement is linked to the run
audit with a bounded reason. It does not require manually reassigning the
workspace binding. The fresh thread starts without security overrides so
Codex can resolve the current config natively.

The manager shows the current read-only resolved summary and status. It never
offers an editable mirror of Codex permission rules and never lists
`runtime-config` as a profile. Pi does not support this binding source.

Workspace Bridge does not write Codex config, requirements, credentials, or
model policy. Native config edits remain the administrator's responsibility.
