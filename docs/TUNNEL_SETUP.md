# OpenAI Secure MCP Tunnel — one connection for all mapped projects

Official-reference check: 2026-09-18. The profile is based on OpenAI's published client configuration. Account-side tunnel creation and the real ChatGPT connection have not been validated in this build environment.

## 1. Prepare the local bridge

Run `workspace-bridge serve`. In the loopback manager on port 8766, add project mappings, create one **shared bridge token**, and explicitly enable intended projects. Select **Tunnel setup** once. The generated profile contains no credentials and points only at port 8765.

One `workspace_id` per project does **not** mean one tunnel channel per project. There is one gateway and one channel. The local manager is never forwarded.

## 2. Install and authorize the official client

The official client repository documents this macOS installation:

```sh
brew install openai/tools/tunnel-client
```

Use the repository's appropriate installation instructions on other platforms. Follow the OpenAI Secure MCP Tunnel guide to create or select a tunnel associated with the intended OpenAI Platform organization and ChatGPT workspace, and create a properly scoped runtime key. Management requires the documented Tunnel Read/Manage permissions; runtime use requires the corresponding Read/Use permissions. Account/UI availability must be confirmed in your account.

Do not invent a tunnel ID or paste account keys into ChatGPT. The placeholder in the example is deliberately invalid until replaced with the real authorized ID.

## 3. Save one profile outside mapped workspaces

Copy `examples/tunnel-client.yaml` to a private configuration directory, or use the local manager's profile. Replace the tunnel ID and retain the actual configured local port:

```yaml
config_version: 1
control_plane:
  tunnel_id: tunnel_REPLACE_WITH_YOUR_32_HEX_ID
  api_key: env:CONTROL_PLANE_API_KEY
mcp:
  server_urls:
    - channel: main
      url: http://127.0.0.1:8765/mcp
  extra_headers:
    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN
  discovery_extra_headers:
    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN
health:
  listen_addr: 127.0.0.1:8790
```

The runtime and discovery header sections are distinct. Both need the same bridge credential. It is independent of the local administrator credential and the OpenAI runtime key. Keep environment secrets private; they can still be visible to sufficiently privileged local processes. Do not write them into shell arguments/history, handoffs or repositories.

The official client's configuration supports one or more server URL channels; **this project uses exactly one**. Its documented `env:` secret references are resolved locally. A fixed server header is not an end-user OAuth identity, so restrict app/tunnel sharing to people trusted with every enabled mapping.

## 4. Diagnose and run

With the bridge already running, use the included local helper to prompt for secrets instead of putting them in command arguments:

```sh
python scripts/run_tunnel.py --config "$HOME/.config/workspace-bridge/bridge-tunnel.yaml" doctor
python scripts/run_tunnel.py --config "$HOME/.config/workspace-bridge/bridge-tunnel.yaml" run
```

The helper prompts for the OpenAI runtime key and the shared bridge token, then runs only the official client when you explicitly invoke it. Equivalent commands after loading those environment variables securely are:

```sh
tunnel-client doctor --config "$HOME/.config/workspace-bridge/bridge-tunnel.yaml" --explain
tunnel-client run --config "$HOME/.config/workspace-bridge/bridge-tunnel.yaml"
```

Neither script is an MCP tool; the server cannot invoke it. The tunnel is an outbound connection. No public inbound service or public OAuth server is provided by Workspace Bridge.

## 5. Create one ChatGPT connection

Follow the current official developer-mode app/Plugins flow, choose **Connection: Tunnel**, and select the authorized tunnel ID. Keep the local bridge and tunnel process healthy for discovery and calls.

Start with `list_workspaces`, then `workspace_info` and `read_file` with the selected ID. Validate another enabled project through **the same connection**. Test that disabled mappings disappear, wrong-workspace job IDs fail, global pause denies requests, and rotation revokes the old credential. Only synthetic/nonsensitive data should be used for initial validation.

Adding workspaces afterward only changes local mappings; no new tunnel/app, extra channel, credential or bridge restart is necessary. Upgrading from v0.1 requires refreshing the changed tool schemas once; see `MIGRATION_0.2.md`.

## Troubleshooting without weakening controls

- **401:** gateway unconfigured/paused, invalid/rotated token, or missing runtime/discovery header. Use `X-Bridge-Token`, not the removed workspace header.
- **404:** old `/mcp/ws_...` URL; use `/mcp`.
- **Workspace unavailable/tool error:** wrong ID, disabled mapping or invalid root identity. Discovery contains only enabled mappings; do not auto-select another project as a fallback.
- **403:** incorrect Host/Origin or duplicated security headers. Keep the exact loopback target; do not allow arbitrary origins or tunnel the manager to work around this.
- **Unknown arguments/tool names:** stale v0.1 cached schemas. Refresh discovery; every project call now requires `workspace_id`.
- **Protocol error:** compare advertised versions and required per-request metadata. Unknown protocol versions are rejected rather than guessed.
- **Search timeout or stale cursor:** narrow the path/include/pattern or restart after writers stop. Do not remove the policy/time bounds.

Forwarded connector headers can override the official client's static extra headers. Do not intentionally configure a competing `X-Bridge-Token`. A wrong token still fails bridge authentication. The bridge does not authenticate separate users within the shared connection.

## Official sources

- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://github.com/openai/tunnel-client
- https://github.com/openai/tunnel-client/blob/master/docs/configuration.md

## Docker Compose (v0.7)

The provided Compose deployment runs only the bridge. Run this same official
client on the Docker host and use the published **host** MCP port. The local
manager's generated profile uses WB_MCP_PORT, not the fixed internal port.
For example, WB_MCP_PORT=8875 means `http://127.0.0.1:8875/mcp`.
Do not use `http://bridge:8765` with the shipped host-only profile and do not
publish or tunnel the management listener beyond host loopback. Changing .env
ports requires `docker compose up -d` to recreate the container and an updated
tunnel target followed by tunnel restart. See DOCKER.md for a complete setup.
