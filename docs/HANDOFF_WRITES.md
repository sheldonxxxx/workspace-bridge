# Handoff writes — superseded API names

v0.5 replaces `write_handoff_file` and `edit_handoff_file` with general `write_file`
and `edit_file`. The old names are rejected, not aliases. The default write scope
is still `.workspace-handoff/`; broader access requires explicit local approval.

See [General file tools and write policy](FILE_ACCESS.md) for the current contract
and [v0.5 migration](MIGRATION_0.5.md) for upgrading. The v0.4 changelog and validation
remain historical evidence, not the current tool schema.
