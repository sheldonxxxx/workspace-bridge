# General file tools and write policy

## Stable interface, independent permissions

```text
read_file(workspace_id, path, offset=1, limit=200, expected_sha256=null,
          representation="auto", max_image_dimension=null)
write_file(workspace_id, path, content, expected_sha256=null)
edit_file(workspace_id, path, old_text, new_text, expected_sha256)
```

All paths are relative to the selected workspace, never the handoff folder itself.
Read source with `path="src/main.py"`, or a note with
`path=".workspace-handoff/notes/plan.md"`. `read_file` is general-purpose already;
`read_handoff` is only an optional helper for standard planning documents.

The same generic write/edit schemas are always exposed. `workspace_info` and
`list_workspaces` return `write_scope` and `source_access`. `workspace_info` also
returns `writable_path_prefix` (`null` for no writes, `.workspace-handoff/` for
handoff-only, or the empty string for workspace-wide). Paths still cannot escape
that workspace or bypass exclusions. `read_scope` remains `workspace` in all modes.

| `write_scope` | Allowed file mutation |
|---|---|
| `none` | None, including new handoffs or handoff edits |
| `handoff` | Only allowed paths inside `.workspace-handoff/`; the default |
| `workspace` | Allowed source and handoff text files inside the selected mapping |

Each new mapping starts disabled and handoff-only. Only the
local manager, authenticated with the separate admin token, can change permission.
There is no MCP `write_scope`, `force`, permission-changing tool or bypass parameter.

## Enable workspace writes later

In the local manager, expand the project's **Exclusions & policy**, choose
**Write permission → Workspace-wide — allowed text files**, then **Save write
permission** and confirm. No new tunnel, client schema or restart is needed for a
subsequent policy change. Select Handoff only or Read-only to reduce access again.
A change affects subsequent calls, including queued calls before they begin their
serialized operation; it cannot undo a completed write.

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
           path=".workspace-handoff/notes/plan.md",
           content="# Plan\nInspect the parser tests first.\n")

# Read the existing file and use its returned SHA-256.
read_file(workspace_id=workspace_id,
          path=".workspace-handoff/notes/plan.md")
edit_file(workspace_id=workspace_id,
          path=".workspace-handoff/notes/plan.md",
          old_text="Inspect the parser tests first.",
          new_text="Inspect the parser tests, then implement the agreed fix.",
          expected_sha256=current_hash)
```

Without a hash, writes are create-only and create missing parents. Existing targets
cause a conflict. Complete replacements and exact edits require the current hash.
Exact editing is case-sensitive and requires one unique match (including overlapping
occurrences); no regex, fuzzy patch interpretation or shell. Empty replacement text
removes the matched text, not the file. Newlines and unrelated text are preserved.
Use these same tools with source paths only after workspace permission is enabled
and the user has requested the edit. Stale/conflicting responses require re-reading
and reconciliation. A lost response is not proof that nothing was written.

Results include actual path, new/previous hash, size, creation flag and write scope;
exact edits also return `replacements: 1`. Important changes should be read back.
Tool annotations classify mutations as potentially destructive and non-idempotent;
those hints are not the access-control mechanism.

## Reading images

`read_file` now returns native MCP image previews for supported raster images,
under the same read permissions. See [image support](IMAGE_SUPPORT.md). Text reads
still have a 512 KiB cap. The following limits describe **writing**, which remains
UTF-8 text-only; image previews cannot be written back with these APIs.

## Bounds and limits

UTF-8 text only, at most 256 KiB before and after writing; request JSON up to 1 MiB.
Built-in and administrator exclusions, secret-like/binary/control rejection,
no-follow paths, single-link regular-file requirements, root pinning and device
checks apply in every mode. Internal `.wb-write-*` staging files are never exposed.
New files use 0600; newly created directories use 0700. Source replacements preserve
ordinary permissions (0777 mask, including execute bits), but discard special bits.
Handoff replacements use 0600. ACLs, extended attributes and ownership are not
preserved by inode replacement; do not use this tool for files requiring those.

Complete content is staged and published without truncating the original inode.
Identity/hash rechecks and serialized bridge calls detect tested ordinary races,
not arbitrary hostile local writers. This is not an OS sandbox, portable atomic
compare-and-swap, multi-file transaction, complete secret detector or per-chat ACL.
No aggregate storage quota, history, automatic cleanup, delete or rename is added.
Errors/termination can leave empty parent directories or hidden staging files.

## Handoff and review stay simple

`prepare_handoff` publishes the normal three documents. Notes use no new database
or job record. Edited generated plans retain original publication hashes;
`matches_published=false` can be an intentional revision, not an audit failure.
Replaying a handoff does not restore old documents. Never rewrite an active plan
or move acceptance criteria after implementation. The user pastes the agent's normal
reply into ChatGPT, which audits current source with general tools. No snapshots,
review IDs, required result files or server-verified verdicts are restored.
