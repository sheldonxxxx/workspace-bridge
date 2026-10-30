# Pi plugin

The Pi agent plugin for [Workspace Bridge](https://github.com/sheldonxxxx/workspace-bridge):
plan in ChatGPT, build with your local agents. Install it on any host where
you want ChatGPT to start bounded Pi runs through Workspace Bridge.

```sh
npm install -g workspace-bridge-pi-host-adapter
```

This package owns Pi agent sessions on the host. It is not a service: a Workspace Bridge Node starts
`workspace-bridge-pi-adapter --stdio` as a child process and talks to it over
Agent Protocol v2 (JSON-RPC on stdin and stdout). It has no port and no
token. Run without `--stdio`, it prints usage and exits.

For runtime behavior, profiles, and troubleshooting see
[Runtimes](https://github.com/sheldonxxxx/workspace-bridge/blob/main/docs/RUNTIMES.md). For repo checks see
[Contributing](https://github.com/sheldonxxxx/workspace-bridge/blob/main/CONTRIBUTING.md).

## Topology

```text
Bridge control plane
  -> Node on this host
    -> this plugin (child process, Agent Protocol v2 over stdio)
      -> in-process Pi session (isolated agent dir, pinned Pi dependency)
```

The adapter owns its Pi conversations, runs, interaction requests,
and activity snapshots. It never adopts an unrelated Pi session. Workspace
Bridge owns run authorization and durable cross-runtime records; Pi owns
native session files, model syntax, and enforcement.

## Profiles and host authority

Security profiles live in the Bridge and arrive with every run. The trusted
permission extension judges each built-in file and shell tool call against the
run's rules, and tools the rules can never allow are not offered: the
built-in `read-only` profile offers read tools only. A run keeps the rules it
started with; a profile edit reaches the next run.

Pi runs with the host user's authority. These controls are pre-tool policy,
not an OS sandbox. Review external paths, protected paths, writable tools,
and shell mode before enabling a profile.

## Protocol surface

The plugin answers only the protocol's resource methods: descriptor, models,
conversations, runs, interactions, and activities. There is no
health, extension or command method. Logs go to stderr; stdout carries
protocol lines only.

A conversation is bound to a canonical project directory and model. Every
run carries the Bridge security profile rules it must enforce; a run under a
new profile revision reopens the idle session with those rules. A run starts
only in an idle owned conversation.
On adapter restart, an in-progress run is marked interrupted; the Bridge
never replays the prompt or adopts an unrelated session.

Tool audit records are stored privately under the isolated agent directory,
bound to the native session, canonical workspace directory, and permission
and extension revisions. The trusted tool hook persists bounded, credential
redacted intent before allowing execution; updates, decisions, and completion
records are persisted independently of Bridge polling. A storage failure
blocks subsequent tool execution and approvals.

After restart, committed tool completions remain visible and unfinished calls
are marked interrupted. An interrupted record cannot establish whether a
tool's external effect happened. Audit decisions do not restore permission
waits or session grants. Each journal retains up to 500 current tool records;
missing spans are reported as audit gaps. Compaction bounds each journal file.

Native session retry uses a bounded budget with exponential backoff;
quota and billing errors fail promptly without consuming it. Retry state is
observable through sanitized records containing only safe scalar fields.

## Add it to a Node

```sh
npm install -g workspace-bridge-pi-host-adapter
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter add pi --name Pi
workspace-bridge node --state "$HOME/.local/state/workspace-bridge-node" adapter test <adapter-id>
```

The Node passes the projects root and these settings on start (set with
`--set key=value`):

| Setting | Default | Notes |
|---|---|---|
| `pi_binary` | `pi` | Deployment signal only; sessions run in-process. |
| `agent_dir` | `$HOME/.pi/workspace-bridge` | Isolated agent dir; never the normal agent tree. Use a different one per Pi adapter. |
| `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |

The projects root is canonicalized and must exist; every session directory
must resolve beneath it. After a package update, restart the Node; there is
no auto-updater.

## Tests

```sh
npm test
```

Node test runner with an in-process fake transport; no network, provider,
or model calls.
