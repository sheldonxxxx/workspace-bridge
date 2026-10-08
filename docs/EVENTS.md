# MCP run events for ChatGPT

Workspace Bridge implements the webhook contract documented in
[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events).
ChatGPT can monitor run completion and pending interactions in Work chats on
ChatGPT web, Work chats with Cloud selected in the desktop app, and dots.
Workspace plugin and event-task controls still apply.

## Discovery and connection

Use the existing authenticated `/mcp` connection and shared `X-Bridge-Token`.
MCP 2.0 (`2026-07-28`) `server/discover` advertises `capabilities.events: {}`.
No feature flag, extra tool, public inbound listener, or Manager tunnel is needed.
Legacy MCP tools remain supported, but event methods require MCP 2.0. Polling,
streaming, `gap` and `terminated` control notifications are not implemented.

The Bridge needs outbound HTTPS access to ChatGPT's callback endpoint. Keep
raw tunnel/HTTP logging disabled: subscription requests carry a callback signing
secret. Callback destinations and signing keys stay in the private Bridge state
and are never returned through tools, status, diagnostics, logs, or events.

Rescan the existing MCP server on the ChatGPT plugin page after deploying this
version. The two events should appear alongside its tools. A skill-only package
update does not replace the MCP server or rescan its event catalog.

## Events and filters

`events/list` returns two definitions, each with `delivery: ["webhook"]`, an
`inputSchema` and a `payloadSchema`. The catalog fits one page;
`nextCursor` is null, and a non-null request cursor is rejected.

| Event | Data beyond common identifiers |
|---|---|
| `workspace_bridge.run.finished` | `outcome`: `succeeded`, `failed`, `cancelled`, `interrupted`, `orphaned` or `blocked` |
| `workspace_bridge.run.needs_attention` | Optional Bridge-issued `interaction_id` and allowlisted `request_kind` |

Every subscription requires `arguments.workspace_id`, an exact enabled mapping
returned by `list_workspaces`. Optional `arguments.run_id` restricts it to one
existing run in that workspace. Omitting it monitors future runs across the
workspace's adapters. Filters never enable a mapping, a route or a write scope.
There is one shared credential and no per-chat ACL.

A delivery has `eventId`, `name`, `timestamp`, `data` and `cursor: null`.
Common data fields are `workspace_id`, `run_id`, `adapter_id` and `runtime_type`.
Payloads contain no prompts, final answers, logs, filesystem paths, display
labels, quota information, callback destinations or approval responses.

Events use the canonical `notification_events` IDs and journal, independently
of Discord. `run.finished` projects recorded notification outcomes; it does not
invent a new runtime outcome or a blocked event that the coordinator never
recorded. An event is a signal to read `read_agent_run` and, when relevant,
`read_agent_interaction` for authoritative current state. Acceptance checks and
agent claims still need review. A pending interaction may already be stale.
The Bridge never starts a run or approves an interaction from an event.

## Subscription lifecycle

ChatGPT supplies `name`, `arguments`, and a `delivery` object containing
`mode: "webhook"`, an HTTPS `url`, and a `whsec_` base64 signing `secret`.
The key must decode to 24–64 bytes. Optional `ttlMs` requests a lifetime.
The server grants up to 24 hours, with a minimum of 60 seconds to avoid excessive
refreshes. Omission or `ttlMs: null` grants the finite default of 24 hours.

Before activation, the Bridge sends a fresh, signed, single-use verification
challenge. A bounded `2xx` response must echo that challenge exactly. Failure
returns JSON-RPC `-32015` with a categorized `error.data.reason`; no application
event is sent. Successful verification is cached by shared principal and callback
URL for at most five minutes. Network verification holds no Service DB lock.

The response contains a deterministic `id`, granted `refreshBefore`,
`cursor: null`, and `truncated: false`. Identity derives from the authenticated
principal, canonical callback URL, event name and canonical filter arguments.
Repeating or refreshing that identity updates the same subscription. Replacement
keys produce both old and new Standard Webhooks signatures for five minutes.

This implementation does not offer protocol replay. Subscribe before starting
the run to monitor. An expired subscription refreshed later starts at the current
journal position; events missed while expired cannot be recovered through the
subscription protocol. Already queued active delivery intent survives restarts.

`events/unsubscribe` takes the original `name`, `arguments`, and
`delivery: {mode: "webhook", url: ...}` and returns an empty result. It is safe
to repeat. After any in-flight delivery finishes, the subscription and queued
intent are removed. Disabled mappings, disabled gateway access, credential
rotation and expiration stop delivery. Subscription creation rechecks access
after verification, and the worker checks access before delivery.

## Delivery and persistence

The broker stores subscriptions in `mcp_event_subscriptions`, payload-free
journal sequence positions in `event_journal_positions`, and per-subscription
intent in `mcp_event_deliveries`. The journal remains the sole event payload
store. Permanent sequence numbers survive SQLite row-ID renumbering.

A separate background worker sends one event per POST with Standard Webhooks
`webhook-id`, `webhook-timestamp`, `webhook-signature` and
`X-MCP-Subscription-Id`. It signs the exact body bytes, preserves the event ID
and body across retries, and generates a fresh signing timestamp/signature for
each attempt. A `2xx` response acknowledges receipt; ChatGPT processes it
asynchronously. Receipt does not prove that ChatGPT completed the requested work.

Callbacks require HTTPS with normal certificate validation. On every connection,
the transport resolves and validates all destination addresses, connects to the
validated public IP, and preserves the original hostname for TLS and HTTP.
Private, local, reserved, multicast and IPv6 transition addresses are blocked.
No proxy or redirect is followed. Verification and event deliveries use the same
transport and bounded responses/timeouts.

Transient network errors, `408`, `429` and `5xx` receive exponential backoff with
at most six attempts. `410` terminates the subscription. `413`, redirects and
other permanent errors are not retried. An ambiguous in-flight request is
requeued after restart with the same event ID; consumers must handle duplicates.
Events may arrive out of order. Delivery failure does not change run state.

The server permits at most 1000 subscriptions and 10000 pending delivery intents.
A full outbox retains its journal position until it can enqueue more, rather
than discarding events. At most 10000 recent terminal delivery records are
retained for bounded local observability. Expired, revoked and obsolete-principal
subscriptions are reclaimed during subscription creation. The authenticated
Manager `/api/status` reports only aggregate subscription/delivery states under
`mcp_events`, without callback URLs or signing keys.
The existing bounded audit journal records event-method protocol mode and safe
completion/error codes, without subscription arguments, destinations or secrets.

## Test with ChatGPT

1. Rescan the existing Workspace Bridge MCP connection. Confirm both events
   appear on the plugin page alongside tools.
2. In a Work chat with Cloud selected, enable Workspace Bridge and ask:

   > Discover my workspaces. Subscribe to workspace_bridge.run.finished for
   > the workspace I select. When a run finishes, read its authoritative result
   > and report its outcome and validation evidence. Do not start another run
   > or modify files automatically.

3. Select the desired workspace from discovery. Confirm subscription and callback
   verification succeed before starting a small run on its configured route.
4. Complete that run. Confirm the Bridge records a `sent` delivery and ChatGPT
   receives the event and reports the run. Trigger a run in another workspace
   to verify the filter prevents delivery.
5. Ask ChatGPT to stop monitoring. Confirm it calls `events/unsubscribe` and
   subsequent matching runs do not trigger this subscription.

For interaction testing, monitor `workspace_bridge.run.needs_attention` and
ask ChatGPT to describe the current request and wait for your decision. It must
not approve automatically. Test refresh, restart recovery and access revocation
in an isolated test state before changing a live route or credential.

The focused checks are `tests/test_event_broker.py`, `tests/test_event_protocol.py`,
`tests/test_event_integration.py` and `tests/test_webhook_transport.py`.
