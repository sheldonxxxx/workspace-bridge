# Package a skill with an existing ChatGPT MCP app

Use this workflow for a private, local, or workspace plugin that combines one
self-contained skill with an already registered MCP app. Normal builds resolve
the app's technical ID from dedicated configuration, environment, or CLI,
without a downloaded plugin ZIP. The builder does not call
OpenAI APIs, upload anything, or change the MCP endpoint or Tunnel.

The repository owns the reusable presentation template at
[`packaging/chatgpt/plugin.json`](../packaging/chatgpt/plugin.json).
Workspace Bridge's canonical skill is
[`workspace_bridge/skills/project-lead/SKILL.md`](../workspace_bridge/skills/project-lead/SKILL.md).
The package contains a snapshot of that file. Editing its source does not
update an installed plugin; build and upload a new version after its bytes
change. Skills describe repeatable workflows around live MCP tools.
See OpenAI's [Build skills](https://developers.openai.com/plugins/build/skills).

## Register the app and establish the package identity

1. Register/connect the MCP app first. For Workspace Bridge, follow
   [Setup](SETUP.md) to use the existing Secure MCP Tunnel. Keep credentials in
   the local connection configuration.
2. In ChatGPT developer mode, open the registered connection's page and copy
   the technical ID from its browser URL. It starts with `plugin_asdk_app_`.
   The official [Package your plugin](https://developers.openai.com/plugins/build/plugins)
   guide describes registration and ID retrieval.
3. Establish the **exact existing plugin package name and current package
   version** from its management metadata. This is the manifest `name`, not a
   display name or the MCP app ID. Updates must keep that name; see
   [Plugin submission errors](https://developers.openai.com/plugins/deploy/submission-errors).
   If the values cannot be verified, stop instead of guessing.

Package names contain 1-64 ASCII letters, digits, underscores, or hyphens and
start with a letter or digit. `My_Plugin-2` is valid; dots are not. This rule is
separate from the lowercase-hyphen rule for skill frontmatter names.

A downloaded current release ZIP is an optional way to find those two values
during bootstrap: inspect only its root `plugin.json`, or its legacy
`.codex-plugin/plugin.json`, locally. Resolve any disagreement before continuing.
No ZIP migration/import helper is required, and routine builds never read an
export or ChatGPT/Codex cache. Do not derive a generated package name from an
app ID. For a deliberately new plugin, choose its package name explicitly and
bootstrap an explicit starting version with `WB_SKILL_PLUGIN_ALREADY_PACKAGED=false`
or `--no-skill-already-packaged`;
the first build adds the skill and advances the package version once.

## Bootstrap once

Run from the repository checkout with Python 3.11+ and PyYAML available in the
existing environment. PyYAML is part of the repository's test extra; see
[Contributing](../CONTRIBUTING.md#setup). `--no-sync` prevents these commands
from installing dependencies implicitly.

For an existing Workspace Bridge plugin that already embeds the exact current
project-lead skill, set **`WB_SKILL_PLUGIN_ALREADY_PACKAGED=true`** (or use
`--skill-already-packaged`) at bootstrap. This includes
a working skill-enabled package you have already uploaded: its next unchanged
build should immediately no-op. Verify that the installed release contains the
same skill bytes before making this declaration; the builder does not inspect
the live plugin.

Copy the tracked synthetic example to the dedicated repository-root file:

```sh
cp packaging/chatgpt/skill-plugin.env.example .env.skill-plugin
```

Edit `.env.skill-plugin` locally, replacing its app ID, exact package name, and
current package version with verified values. The example's already-packaged
setting is true for the existing skill-enabled release scenario above. Set it
to false if the skill still needs adding. Keep the filled file private; it is
ignored by the existing `.env*` rule. Then bootstrap explicitly:

```sh
uv run --no-sync python scripts/build_skill_plugin.py --init
```

Initialization writes private local state and emits no ZIP. It records the
normalized app ID, exact package name/version, fixed app alias, a snapshot of
the selected manifest template, and the canonical skill's absolute source path.
With the already-packaged setting true, it immediately validates the selected SKILL.md
and records its SHA-256, name, and optional skill version as an
`existing_release` baseline. No local artifact is claimed, the package version
stays unchanged, and the next build is a no-op until those bytes change.

Use a false setting when the existing plugin does **not** yet contain that skill,
or when creating a new plugin. An absent setting also defaults to false. This
records an `unpackaged` baseline with no skill
digest; the first normal build then adds the skill with one patch bump. Neither
mode is inferred from the live plugin. The positive and negative CLI flags are
accepted only with `--init`.

The default alias is the template's `name` (`workspace-bridge`); it is independent
of the actual package name and app ID. Choose another alias with `--app-alias`
at initialization if your skill needs one. Alias validation remains a separate,
conservative lowercase rule; specify an alias if your template's name does not
fit it.

State defaults to `$XDG_STATE_HOME/workspace-bridge/skill-plugins` when that
absolute environment path is set. Otherwise it uses
`~/Library/Application Support/workspace-bridge/skill-plugins` on macOS or
`~/.local/state/workspace-bridge/skill-plugins` on Linux. `--state /path/to/private-state`
selects an explicit directory; pass the same directory on every command.
Each app's JSON filename is the SHA-256 of its normalized ID. Files and emitted
ZIPs are created with owner-only permissions. Initialization refuses to replace
an existing app's state.

If you previously initialized without the correct already-packaged declaration,
preserve that state and bootstrap a separate `--state` directory with verified
identity/version and the flag. Do not edit the digest or overwrite existing
state to change its meaning.

Keep state and artifacts outside tracked source. Back up the state together
with its artifacts: it owns the package version counter and presentation
snapshot. Template changes alone do not trigger this skill-update workflow or
change an existing state snapshot. If adopting an existing plugin, confirm
that the selected presentation template is the intended complete metadata.
This builder packages one SKILL.md; it does not preserve additional skills,
assets, hooks, or supporting resources from an arbitrary existing package.

## Configuration and precedence

The default file is always `.env.skill-plugin` at this repository's root, even
when the process CWD is elsewhere. It is separate from the Docker Compose
`.env`; the builder never auto-loads Docker `.env`, and never needs shell
`source`. A missing default file is harmless. Use `--env-file /path/to/private-config.env`
to select another file; a missing or unreadable explicit file fails.

Settings resolve as **CLI > process environment > env file > built-in/default**.
An explicit `--no-skill-already-packaged` overrides an inherited true value.
`--init` is always an explicit CLI action; no env key selects it.

| Env-file / process-environment key | CLI override | Used for |
| --- | --- | --- |
| `WB_SKILL_PLUGIN_APP_ID` | `--app-id` | Both actions; required after resolution |
| `WB_SKILL_PLUGIN_PACKAGE_NAME` | `--package-name` | Bootstrap only |
| `WB_SKILL_PLUGIN_PACKAGE_VERSION` | `--package-version` | Bootstrap only |
| `WB_SKILL_PLUGIN_ALREADY_PACKAGED` | `--skill-already-packaged` / `--no-skill-already-packaged` | Bootstrap only; defaults to false |
| `WB_SKILL_PLUGIN_STATE` | `--state` | Both actions |
| `WB_SKILL_PLUGIN_APP_ALIAS` | `--app-alias` | Bootstrap only |
| `WB_SKILL_PLUGIN_TEMPLATE` | `--template` | Bootstrap only |
| `WB_SKILL_PLUGIN_SKILL` | `--skill` | Remember source at bootstrap; override saved source for a build |
| `WB_SKILL_PLUGIN_OUTPUT` | `--output` | Normal builds; file setting is ignored during bootstrap |

Bootstrap settings can remain in the file during normal builds. Those builds
use the saved package identity/version and presentation; they do not
reinitialize or replace them from the file. Explicit bootstrap-only CLI options
remain errors without `--init`, and explicit CLI `--output` remains invalid
during bootstrap. Leave OUTPUT unset for automatic versioned artifact paths;
a fixed output path retains the existing refusal to overwrite differing bytes.

Env-file paths for STATE, TEMPLATE, SKILL, and OUTPUT are absolute or relative
to that file's directory. Process-environment and CLI paths retain process/CWD
semantics. There is no tilde or variable expansion by the builder. Leave optional
settings commented out to use the existing defaults or saved source path.

The stdlib parser accepts UTF-8 text, blank lines, `#` comments, optional
`export ` prefixes, `KEY=VALUE`, and simple unquoted/single-quoted/double-quoted
literals. Booleans accept true/false, 1/0, yes/no, and on/off, case-insensitively.
Backslashes remain literal; no escape decoding, shell execution, dollar-variable
expansion, or command substitution occurs. Dollar signs and backticks in file
values, unbalanced quotes, malformed assignments, duplicate supported keys,
invalid booleans, NULs, and unknown `WB_SKILL_PLUGIN_*` keys fail without echoing
raw values. Other valid non-prefixed keys are ignored. Configuration syntax
must remain valid even for settings overridden by a higher-priority source.

The app ID is a private binding identifier, not an API secret. Keep filled
configuration, state, and generated bindings out of public source and logs;
never paste them into chat. The
[example config](../packaging/chatgpt/skill-plugin.env.example) contains only
synthetic placeholders. No credentials or new dependency are needed for env
configuration.

## Build and upload changes

After bootstrap, normal Workspace Bridge builds need no repeated arguments:

```sh
uv run --no-sync python scripts/build_skill_plugin.py
```

Both `plugin_asdk_app_...` and `asdk_app_...` are accepted; the former is
normalized by removing `plugin_`. The generated binding is:

```json
{
  "apps": {
    "workspace-bridge": {"id": "asdk_app_example123"}
  }
}
```

The alias comes from saved local state. This `.app.json` shape and the ID
format follow the local/workspace package rules in
[Plugin submission errors](https://developers.openai.com/plugins/deploy/submission-errors).
The generated archive has these root-relative paths, with no enclosing directory:

```text
plugin.json
.codex-plugin/plugin.json
.app.json
skills/project-lead/SKILL.md
```

Root `plugin.json` is the Agent Plugins 1.0 manifest. Its
`extensions.com.openai.apps` points to `./.app.json`. The compatibility manifest
has synchronized name, version, and presentation for older clients. No `mcp.json`
or Tunnel URL is generated. These canonical paths and mapping are described in
[Package your plugin](https://developers.openai.com/plugins/build/plugins).

- **Identical source bytes:** exit successfully with `No change`, without
  changing state/version or writing a replacement ZIP. Even an explicit
  `--output` is left untouched.
- **Unpackaged baseline or changed bytes:** advance the package patch version exactly
  once, write and validate the complete ZIP, then atomically save its skill
  digest, skill name/version, artifact path/hash, and new package version.
  Bootstrap at `1.0.0` with the already-packaged setting false produces `1.0.1`
  on the first build. With a true setting, unchanged bytes keep `1.0.0` and emit no ZIP;
  their first change produces `1.0.1`.
- **Invalid/missing state:** fail with bootstrap guidance. Restore a trusted
  backup or initialize a separate state directory with verified identity and
  the latest packaged/published version. Do not overwrite state to force a bump.

The state marker `skill_baseline_origin` distinguishes `unpackaged`,
`existing_release`, and `generated`. Only an explicit existing-release baseline
can have a packaged skill digest without local artifact metadata. Once the
builder writes a ZIP, state becomes `generated` and requires its artifact
path, SHA-256, and matching version. Older unmarked state retains its original
empty-baseline/generated-artifact checks; it is never treated as an imported
existing-release baseline.

By default the printed ZIP path is under the private state directory's
`artifacts/<app-key>/` folder. Use `--output /path/to/upload.zip` for an explicit
file path in an existing directory. Existing differing files are never
overwritten. ZIP timestamps, file order, permissions, and serialization are
fixed, giving deterministic bytes for the same package inputs.

Use ChatGPT's **upload new version** flow for the same existing private/workspace
plugin and select the emitted ZIP. Keep its ownership and audience unchanged.
Test the installed update in a new chat to verify skill discovery and access
to the already registered app. Local packaging tests do not prove app
eligibility, upload acceptance, or host behavior.

If upload fails, **reuse the existing generated ZIP**. Local state means
successfully packaged or explicitly declared as an existing-release baseline,
not successfully uploaded by this builder; another build of the same skill is
a no-op. For builder-generated packages, `last_artifact.path` and its SHA-256
are recorded in the local state JSON for recovery. Keep that artifact until
upload succeeds. If it was deleted, restore it from backup; the no-op rule does
not regenerate it. An existing-release bootstrap emits no ZIP and has no local
artifact to upload until a later skill change is packaged.

An archive write/validation failure leaves the previous state intact. If the
ZIP succeeded but state saving failed, keep the ZIP, fix the state directory's
permissions, and retry the same command/output. A byte-identical artifact is
reused at the same candidate version; differing files fail closed. Per-app
locking prevents concurrent builds from racing the version counter.

## Embed your own skill

Create a self-contained UTF-8 SKILL.md with YAML `name` and `description`, and
optionally its own string `version`:

```markdown
---
name: support-review
description: Review support cases using the connected MCP app.
version: 2.0.0
---

Read the case and its evidence before proposing a response.
```

Use a lowercase name of at most 64 letters/digits with single hyphen separators.
The combined identity `plugin-name:skill-name` must be at most 64 characters, including the colon.
Maintainers should bump the skill's own version when behavior changes. It is
independent of the package version; the byte digest determines whether to build.
Names, descriptions, duplicate YAML keys, and malformed frontmatter are checked.

Prepare your own root manifest template by copying
[`packaging/chatgpt/plugin.json`](../packaging/chatgpt/plugin.json) to a local
file and adjusting its description, author, and `extensions.com.openai.interface`.
Keep the Agent Plugins schema and `apps: "./.app.json"`; keep
`shortDescription` at most 30 characters. Bootstrap overrides the template's
name/version with your verified existing identity/version. The supported
template contains self-contained metadata, without referenced assets or hooks.

Create an alternate private config, for example `/path/to/private-support.env`,
with your own verified values and paths. This example assumes the support skill
has not yet been bundled, so it explicitly sets the already-packaged setting
false. If the existing release contains these exact bytes, set true instead;
the next normal build will no-op.

```dotenv
WB_SKILL_PLUGIN_APP_ID=plugin_asdk_app_example456
WB_SKILL_PLUGIN_PACKAGE_NAME=my-existing-support-plugin
WB_SKILL_PLUGIN_PACKAGE_VERSION=1.5.0
WB_SKILL_PLUGIN_ALREADY_PACKAGED=false
WB_SKILL_PLUGIN_STATE=/path/to/private-support-state
WB_SKILL_PLUGIN_APP_ALIAS=support
WB_SKILL_PLUGIN_TEMPLATE=/path/to/support-plugin.json
WB_SKILL_PLUGIN_SKILL=/path/to/support-review/SKILL.md
```

```sh
uv run --no-sync python scripts/build_skill_plugin.py \
  --env-file /path/to/private-support.env --init

uv run --no-sync python scripts/build_skill_plugin.py \
  --env-file /path/to/private-support.env
```

Later builds use that saved custom skill path and presentation snapshot. An
optional normal-build `--skill /path/to/SKILL.md` overrides the source for that
invocation; it does not change the bootstrapped path. If the source moves, use
the override explicitly or restore the saved path. A successful changed build
embeds it at `skills/<frontmatter-name>/SKILL.md`.

## MCP snapshots and private app bindings

The [MCP Skills extension](MCP_TOOLS.md#project-lead-skill-mcp-skills-extension)
remains available. Clients that import its skill take a submission-time
snapshot and need their import/scan workflow after changes. This guide instead
packages the canonical source directly alongside an existing app reference;
it does not require re-exporting or re-scanning to generate the ZIP.

Hosted MCP tool-code changes do not change the packaged skill digest and do
not require a skill plugin ZIP update. Use the MCP server's own deployment
and any client-required tool refresh. If changed tools require revised skill
instructions, update the canonical SKILL.md and package that change too.

`.app.json`, local state, and generated ZIPs contain a private account/workspace
binding. They are not API credentials, but keep them local and out of public
source, logs, and chat. No credentials are needed by this builder. Existing app
references are for eligible private/local/workspace use; **public-directory
submission uses the With MCP server submission flow**, not this app-bound ZIP.
See [Plugin submission errors](https://developers.openai.com/plugins/deploy/submission-errors).

## Agent procedure

1. Confirm the registered app ID and exact existing package identity/version
   from authorized local metadata. Ask for missing verified values; never
   derive names, inspect credential files, call registration APIs, or search
   ChatGPT/Codex caches.
2. Read the chosen skill and repository-owned template. Preserve unrelated
   work and use a private state directory. With authorization, prepare the
   dedicated `.env.skill-plugin` from the synthetic example, or select an
   alternate private file with `--env-file`. Do not read Docker `.env` or
   credential files. Confirm effective CLI/process/file precedence without
   printing private IDs or raw config. Bootstrap only once with explicit
   `--init`, verified identity/version, alias, and canonical skill source.
   Set already-packaged true only after confirming those exact bytes are
   present in the current release; set false when the skill still needs adding.
3. Run the same command without `--init` for a normal build. For a no-op, report no version change
   and no artifact. For a changed build, inspect the generated ZIP's manifest,
   skill path/digest, and binding locally without printing private IDs.
4. Report the actual output path, version, checks, and limits. Keep the ZIP
   for retry. Upload only when the user explicitly requests it; packaging alone
   does not authorize registration, publishing, or Tunnel changes.

Repository verification uses synthetic app IDs and isolated temporary state:

```sh
uv run --no-sync pytest -q tests/test_plugin_skill_packaging.py tests/test_embedded_skill.py tests/test_public_docs.py
```
