# General file tools and write policy

## Stable interface, independent permissions

```text
read_file(workspace_id, path, offset=1, limit=200, expected_sha256=null,
          representation="auto", max_image_dimension=null)
write_file(workspace_id, path, content, expected_sha256=null)
edit_file(workspace_id, path, old_text, new_text, expected_sha256)
write_image(workspace_id, path, data_base64, mime_type, expected_sha256=null)
apply_patch(workspace_id, changes, message)
undo_edit(workspace_id, edit_id)
```

All paths are relative to the selected workspace, for example
`path="src/main.py"`. Handoffs are kept in Bridge state, not in the workspace;
read them with `read_handoff`.

The same generic write/edit schemas are always exposed. `workspace_info` and
`list_workspaces` return `write_scope` and `source_access`. `workspace_info` also
returns `writable_path_prefix` (`null` for no writes, or the empty string for
workspace-wide). Paths still cannot escape that workspace or bypass exclusions.
`read_scope` remains `workspace` in all modes.

| `write_scope` | Allowed file mutation |
|---|---|
| `none` | None: read-only, the default. Handoffs and runs still work |
| `workspace` | Edit: allowed text files, image uploads inside `.workspace-handoff/attachments/`, and the configured checks |

Each new mapping starts disabled and read-only. Workspaces set to the earlier
handoff-only scope became read-only. Only the local manager, authenticated with
the admin account, can change permission. There is no MCP `write_scope`,
`force`, permission-changing tool or bypass parameter.

## Enable edits later

In the local manager, open the project's **Settings**, choose **ChatGPT file
access → Edit permitted files and run checks**, then save and confirm. No new
tunnel, client schema or restart is needed for a subsequent policy change.
Select Read only to reduce access again.
A change affects subsequent calls, including writes queued behind it for the same
workspace; it cannot undo a completed write or stop one already in progress.

Workspace-wide grants source-edit capability to every authorized chat using the
shared connection. It is not a request to edit arbitrary source. The project-lead
skill continues manual agent delegation unless the user asks for direct edits.
OS filesystem permissions must also permit the operation; the setting does not
grant extra OS privileges. Keep the server installation/state and tunnel profile
outside mapped projects. Stop agents/watchers that could conflict with edits.

## Create and edit

```python
# Illustrative MCP arguments; use the actual workspace ID.
write_file(workspace_id=workspace_id,
           path="notes/plan.md",
           content="# Plan\nInspect the parser tests first.\n")

# Read the existing file and use its returned SHA-256.
read_file(workspace_id=workspace_id,
          path="notes/plan.md")
edit_file(workspace_id=workspace_id,
          path="notes/plan.md",
          old_text="Inspect the parser tests first.",
          new_text="Inspect the parser tests, then implement the agreed fix.",
          expected_sha256=current_hash)
```

Without a hash, writes are create-only and create missing parents. Existing targets
cause a conflict. Complete replacements and exact edits require the current hash.
Exact editing is case-sensitive and requires one unique match (including overlapping
occurrences); no regex, fuzzy patch interpretation or shell. Empty replacement text
removes the matched text, not the file. Newlines and unrelated text are preserved.
Use these tools only when the workspace allows edits and the user has requested
the edit. Stale/conflicting responses require re-reading
and reconciliation. A lost response is not proof that nothing was written.

Results include actual path, new/previous hash, size, creation flag and write scope;
exact edits also return `replacements: 1`. Important changes should be read back.
Tool annotations classify mutations as potentially destructive and non-idempotent;
those hints are not the access-control mechanism.

## Atomic patches and undo

For source changes, `apply_patch` edits, creates, moves and deletes up to 50
files in one step: all changes apply or none do. The Node journals each
touched file's earlier content, keyed by the returned `edit_id`, and
`undo_edit` restores them while the files still hold what the patch wrote.
See [apply_patch and undo_edit](MCP_TOOLS.md#apply_patch-and-undo_edit).
`write_file` and `edit_file` are not journaled.

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
  `.workspace-handoff` folder outside its image-attachment and earlier-handoff
  paths.
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
still have a 512 KiB cap. `write_file` and `edit_file` remain UTF-8 text-only.
`write_image` accepts validated raster uploads under `.workspace-handoff/attachments/`
when the workspace allows edits, subject to exclusions. Original bytes are preserved, including
embedded metadata; previews remain transformed. See [image upload](MCP_TOOLS.md#image-upload)
for its 20 MiB/40MP bounds, matching MIME/suffix checks and hash-protected replacement.

## Bounds and limits

Text writes accept UTF-8 only, at most 256 KiB before and after writing; request JSON up to 1 MiB.
Built-in and administrator exclusions, new secret-like and binary/control rejection,
no-follow paths, single-link regular-file requirements, root pinning and device
checks apply in every mode. Internal `.wb-write-*` staging files are never exposed.
New files use 0600; newly created directories use 0700. Source replacements preserve
ordinary permissions (0777 mask, including execute bits), but discard special bits.
Handoff replacements use 0600. ACLs, extended attributes and ownership are not
preserved by inode replacement; do not use this tool for files requiring those.

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

`prepare_handoff` stores the normal three documents in Bridge state and needs
no write scope; a published handoff cannot be edited afterwards. Never move
acceptance criteria after implementation. The user pastes the agent's normal
reply into ChatGPT, which audits current source with general tools and, where
the user configured them, [checks](MCP_TOOLS.md#run_checks). Automated runs
add Node-held [run checkpoints](MCP_TOOLS.md#run-checkpoints) for a run-window
diff. No review IDs, required result files or server-verified verdicts are
restored.
