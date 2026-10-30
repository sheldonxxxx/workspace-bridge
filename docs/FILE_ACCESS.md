# General file tools and write policy

## Stable interface, independent permissions

```text
read_file(workspace_id, path, offset=1, limit=200, expected_sha256=null,
          representation="auto", max_image_dimension=null)
apply_patch(workspace_id, changes, message)
undo_edit(workspace_id, edit_id)
upload_image(workspace_id, data_base64, mime_type)
```

All paths are relative to the selected workspace, for example
`path="src/main.py"`. Handoffs are kept in Bridge state, not in the workspace;
read them with `list_handoffs`. Image uploads are kept in Node state, not in
the workspace.

The same edit schemas are always exposed. `workspace_info` and
`list_workspaces` return `write_scope` and `source_access`. `workspace_info` also
returns `writable_path_prefix` (`null` for no writes, or the empty string for
workspace-wide). Paths still cannot escape that workspace or bypass exclusions.
`read_scope` remains `workspace` in all modes.

| `write_scope` | Allowed file mutation |
|---|---|
| `none` | None: read-only, the default. Image uploads, handoffs and runs still work |
| `workspace` | Edit: allowed text files through `apply_patch`, and the configured checks |

Each new mapping starts disabled and read-only. Workspaces set to the earlier
handoff-only scope became read-only. Only the local manager, authenticated with
the admin account, can change permission. There is no MCP `write_scope`,
`force`, permission-changing tool or bypass parameter.

## Enable edits later

In the local manager, open the project's **Settings**, choose **ChatGPT file
access → Edit permitted files and run checks**, then save and confirm. No new
tunnel, client schema or restart is needed for a subsequent policy change.
Select Read only to reduce access again.
A change affects subsequent calls, including patches queued behind it for the same
workspace; it cannot undo a completed patch or stop one already in progress.

Workspace-wide grants source-edit capability to every authorized chat using the
shared connection. It is not a request to edit arbitrary source. The project-lead
skill continues manual agent delegation unless the user asks for direct edits.
OS filesystem permissions must also permit the operation; the setting does not
grant extra OS privileges. Keep the server installation/state and tunnel profile
outside mapped projects. Stop agents/watchers that could conflict with edits.

## Create and edit

```python
# Illustrative MCP arguments; use the actual workspace ID.
apply_patch(workspace_id=workspace_id,
            message="Add the plan notes",
            changes=[{"op": "create", "path": "notes/plan.md",
                      "content": "# Plan\nInspect the parser tests first.\n"}])

# Read the existing file and use its returned SHA-256.
read_file(workspace_id=workspace_id,
          path="notes/plan.md")
apply_patch(workspace_id=workspace_id,
            message="Name the agreed fix",
            changes=[{"op": "edit", "path": "notes/plan.md",
                      "sha256": current_hash,
                      "old": "Inspect the parser tests first.",
                      "new": "Inspect the parser tests, then implement the agreed fix."}])
```

A `create` is exclusive and creates missing parents; an existing target is a
conflict. `edit`, `move` and `delete` require the file's current hash. An
`edit` is case-sensitive and requires one unique match (including overlapping
occurrences); no regex, fuzzy patch interpretation or shell. An empty `new`
removes the matched text, not the file. Newlines and unrelated text are
preserved. A single-file change is a patch with one change; there is no
separate whole-file write tool. Use `apply_patch` only when the workspace
allows edits and the user has requested the edit. Stale/conflicting responses
require re-reading and reconciliation. A lost response is not proof that
nothing was written.

## Atomic patches and undo

`apply_patch` edits, creates, moves and deletes up to 50 files in one step:
all changes apply or none do. The Node journals each touched file's earlier
content, keyed by the returned `edit_id`, and `undo_edit` restores them while
the files still hold what the patch wrote. Results include each file's
previous and new hash. Tool annotations mark both tools destructive; those
hints are not the access-control mechanism. See
[apply_patch and undo_edit](MCP_TOOLS.md#apply_patch-and-undo_edit).

## Secret-like content

A write is refused when it adds secret-like text (private keys, cloud and
GitHub tokens, `password = ...` style assignments) that the file did not
already hold the same number of times. A file that already contains such a
match stays editable, and moving or deleting it is allowed. Reads still redact
matches, so edit around them rather than through them. The detector is a
pattern list, not a complete secret scanner.

## Exclusions and showing hidden folders

Two built-in lists apply in every mode:

- Always denied: version-control and credential names (`.git`, `.ssh`, `.aws`,
  `.config`, `.npmrc`, `.netrc`, `.claude`, `.mcp.json`, `.env*`, `*.pem`,
  `*.key`, `*credentials*`, `secrets`, `*.sqlite*`, `*.db` and similar) and the
  `.workspace-handoff` folder 0.3 kept handoffs in.
- Hidden by default: generated and dependency folders (`node_modules`, `.venv`,
  `venv`, `__pycache__`, tool caches, `dist`, `build`, `target`, `.next`,
  `.nuxt`, `coverage`, `vendor`, `.DS_Store`, `*.log`).

An administrator exclusion list adds case-insensitive globs. An entry `!name`
shows one hidden-by-default name exactly as listed, for example `!build` or
`!vendor`; anything else after `!`, including the always-denied names, is
rejected. A path denied by any other rule stays denied, so `!vendor` does not
expose `vendor/.env`. `workspace_info` reports `shown_default_hidden` and
`extra_exclusions` separately.

## Reading images

`read_file` now returns native MCP image previews for supported raster images,
under the same read permissions. See [image support](IMAGE_SUPPORT.md). Text reads
still have a 512 KiB cap. `apply_patch` remains UTF-8 text-only.
`upload_image` accepts validated raster uploads into the Node's state, in any
write scope. Original bytes are preserved, including embedded metadata;
previews remain transformed. See [image upload](MCP_TOOLS.md#image-upload)
for its 20 MiB/40MP bounds and matching MIME checks.

## Bounds and limits

Text changes accept UTF-8 only, at most 256 KiB per created or edited file and
1 MiB of text per patch; request JSON up to 1 MiB.
Built-in and administrator exclusions, new secret-like and binary/control rejection,
no-follow paths, single-link regular-file requirements, root pinning and device
checks apply in every mode. Internal `.wb-write-*` staging files are never exposed.
New files use 0600; newly created directories use 0700. Source replacements preserve
ordinary permissions (0777 mask, including execute bits), but discard special bits.
ACLs, extended attributes and ownership are not preserved by inode replacement;
do not use `apply_patch` for files requiring those.

Complete content is staged and published without truncating the original inode.
Identity/hash rechecks and per-workspace write serialization (on the Bridge and
on the Node) detect tested ordinary races,
not arbitrary hostile local writers. This is not an OS sandbox, portable atomic
compare-and-swap, filesystem transaction, complete secret detector or per-chat ACL.
`apply_patch` rolls back in the application, so a crash midway can leave a
partial patch; its journal entry is then marked `interrupted` and `undo_edit`
restores the files it reached. Errors/termination can leave empty parent
directories or hidden staging files.

## Handoff and review stay simple

`save_handoff` stores the normal three documents in Bridge state and needs
no write scope; a published handoff cannot be edited afterwards. Never move
acceptance criteria after implementation. The user pastes the agent's normal
reply into ChatGPT, which audits current source with general tools and, where
the user configured them, [checks](MCP_TOOLS.md#run_checks). Automated runs
add Node-held [run checkpoints](MCP_TOOLS.md#run-checkpoints) for a run-window
diff. No review IDs, required result files or server-verified verdicts are
restored.
