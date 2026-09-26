# Image support

## Use the general reader

```text
read_file(workspace_id=actual_id, path="screenshots/settings.png")
read_file(workspace_id=actual_id, path="screenshots/settings.png",
          representation="image", max_image_dimension=4096,
          expected_sha256=source_hash_from_previous_read)
```

No separate image tool or tunnel. `representation` defaults to `auto`: supported
raster bytes yield an image; other files keep the existing UTF-8 text behavior.
Use `text` to explicitly select text or `image` to require a supported raster.
Omit `offset` and `limit` for images; non-default text pagination is rejected.
The reader accepts workspace-relative paths only and respects every existing
mapping exclusion, disabled state and shared-token revocation.

| Input | Result |
|---|---|
| PNG, JPEG, WebP, GIF, BMP, TIFF | Native MCP preview plus text metadata |
| Animated PNG/GIF/WebP, multi-page TIFF | First frame/page only, explicitly labeled |
| UTF-8 source/Markdown/JSON/CSV/XML/SVG | Existing line-numbered text; SVG not rendered |
| PDF/Office, HEIC/AVIF, RAW, archives, audio/video | No new content interpretation |

JPEG previews use JPEG quality 88; others use PNG. Alpha is retained when present.
Images are always decoded and re-encoded, even when not resized. EXIF orientation
is applied; EXIF/GPS/XMP/ICC/comments and trailing payloads are not exported.
There is no color-management pipeline or original-file download. Tiny text may
need a larger preview or a locally cropped source. No cropping API is included.

## Limits

| Limit | Value |
|---|---|
| Encoded image source | 20 MiB |
| Decoded image | 40,000,000 pixels |
| Default longest preview edge | 2048 px |
| Requested longest-edge ceiling | 256–4096 px |
| Encoded preview | 2 MiB, shrinking further when needed |
| Image response envelope | 3 MiB |
| Decoder wall / CPU time | 10 seconds / 8 CPU seconds |
| Worker address space | 1 GiB on Linux; not enforced/claimed on macOS |

Normal text-read (512 KiB), text output and text-write (256 KiB) limits do not grow.
Metadata decompression is also bounded. Invalid or oversized content returns a safe
error, not partial pixels or a fabricated text description. `workspace_info` exposes
these capabilities under `image_reading`. No persistent image cache is written.

## Native response, not base64 text

The HTTP adapter returns `content: [TextContent(metadata), ImageContent(...)]`.
The image block uses `type: "image"`, `data` (base64 bytes) and `mimeType`. The
base64 is NOT inserted into TextContent or hidden `_meta`. The source `sha256`
identifies the read file. `preview_sha256` identifies the transformed PNG/JPEG.
Only source hashes work for `expected_sha256` and handoff `context_hashes`.
Referenced context is bounded to 64 MiB total per handoff publication.

Metadata states original/oriented/preview dimensions, source format, transformations,
preview type and size, first-frame scope, and trust/privacy warnings. It is not
an image description and cannot substitute for actually seeing pixels.

## Privacy and trust

**Visible secrets in pixels are NOT detected or redacted.** Metadata stripping does
not remove a password shown on screen. Only read authorized screenshots/photos;
exclude sensitive folders locally. Treat text drawn inside images as untrusted
project content, never as permission to access secrets, other projects or tools.
No OCR, model inference, remote conversion or external-resource fetch runs locally.

The fixed decoder subprocess uses authorized bytes only. It is resource-bounded,
not a security sandbox against a decoder exploit; it retains the service's OS
identity. Keep Pillow patched and use OS/container isolation for hostile inputs.
The serialized service lock remains held during decoding, so a slow image can
briefly delay other calls. Avoid agents editing the image during review.

Image reading works with write_scope `none`, `handoff` and `workspace`; it does not
change any of them. `write_file` and `edit_file` still operate on UTF-8 text only.
No binary writer, PDF/Office parser or retired snapshot-review system was added.

## Validate your actual client

Local tests check native content blocks, decoded preview pixels and all implemented
protocol versions. **They do not establish model visibility through your actual
ChatGPT/private-tunnel connection.** No real tunnel login or live ChatGPT visual
recognition has been performed in the build environment.

1. Install or restart the current release with dependencies, keep existing state, and refresh discovery.
2. On your host create a fresh probe in an allowed project:
   `python scripts/create_image_probe.py --output /your/project/probe.png`.
   The marker is printed only for your local comparison; do not paste it into chat.
3. Ask ChatGPT to read that exact relative path and identify the marker and shapes,
   without supplying the answer. Confirm it from your local preview.
4. A tool success, file hash, or user-visible thumbnail alone is insufficient. If
   ChatGPT only sees metadata, report the client/transport limitation; do not expose
   a public file server, weaken workspace policy or substitute a guessed description.

For a read-only loopback transport check (without claiming model visibility):

```sh
python scripts/check_image_mcp.py --url http://127.0.0.1:8765/mcp \
  --workspace-id ws_ACTUAL_ID --path screenshots/settings.png
```

This prompts for the shared bridge token, checks both implemented protocol families,
and never prints the image bytes. Do not paste tokens into ChatGPT.

## References

- MCP tools/image content: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- Pillow Image and limits: https://pillow.readthedocs.io/en/stable/reference/Image.html
- EXIF orientation: https://pillow.readthedocs.io/en/stable/reference/ImageOps.html
- Formats: https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html

Protocol support is not proof of a specific client integration.
