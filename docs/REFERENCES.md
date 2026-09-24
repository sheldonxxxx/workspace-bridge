# Primary references

Checked 2026-09-18. References establish external interfaces, not proof of a real deployment.

- OpenAI Secure MCP Tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels — outbound private MCP connectivity, tunnel/workspace associations and permissions, ChatGPT connection flow.
- Official tunnel client: https://github.com/openai/tunnel-client — installation and operation.
- Official profile schema: https://github.com/openai/tunnel-client/blob/master/docs/configuration.md — `server_urls`, channels, `extra_headers`, `discovery_extra_headers`, `env:` secrets. Workspace Bridge uses one endpoint/channel.
- MCP Streamable HTTP: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http — current protocol transport; this project retains legacy 2025 support in a small explicit adapter, not an externally certified SDK.
- Codex official repository: https://github.com/openai/codex — app-server v2 protocol source for the dedicated Codex host adapter.
- Python regex maintainer documentation: https://pypi.org/project/regex/ — matching timeout support used by bounded `grep_files`.

No shell, Codex/Pi SDK, ripgrep process, automatic agent control, OpenAI model client, or remote-fetch operation is imported by the MCP service. `scripts/run_tunnel.py` is a separately invoked local setup convenience, not an MCP execution path.

## Image implementation — checked 2026-09-18

- https://modelcontextprotocol.io/specification/2026-07-28/server/tools — native mixed tool content and image payload fields.
- https://pillow.readthedocs.io/en/stable/reference/Image.html — restricted formats, decoding and decompression-bomb limits.
- https://pillow.readthedocs.io/en/stable/reference/ImageOps.html — EXIF orientation.
- https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html — raster format handling.

These references establish implementation primitives, not actual ChatGPT/tunnel
compatibility. Live visual recognition is a separate pending validation step.

## Docker Compose deployment (v0.7, checked 2026-09-18)

- Services, bind options, non-root UID and health checks: https://docs.docker.com/reference/compose-file/services/
- Localhost port publication and old Engine caveat: https://docs.docker.com/engine/network/port-publishing/
- Bind mount write access and local/remote daemon semantics: https://docs.docker.com/engine/storage/bind-mounts/
- Compose .env quoting/interpolation: https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- Mount-target override merging: https://docs.docker.com/reference/compose-file/merge/
