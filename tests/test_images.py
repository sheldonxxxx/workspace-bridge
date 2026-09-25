"""Image parsing/delivery and access tests; not ChatGPT/tunnel vision validation."""
from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import subprocess
import zlib

import httpx
from PIL import Image, PngImagePlugin
import pytest

from workspace_bridge.api import TOOLS, make_mcp
from workspace_bridge.embedded_skill import read_project_lead_skill
from workspace_bridge.media import (ImageReadResult, MAX_IMAGE_RESPONSE_BYTES, MAX_INPUT_BYTES,
                                    MAX_PIXELS, MAX_PREVIEW_BYTES, read_image, sniff_image)
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX
from workspace_bridge.security import BridgeError, HANDOFF, MAX_FILE, SafeRoot, digest
from workspace_bridge.service import Service


def image_bytes(fmt="PNG", size=(120, 80), mode="RGB", **kwargs):
    with Image.new(mode, size, (20, 90, 160, 70) if mode == "RGBA" else (20, 90, 160) if mode == "RGB" else 80) as image:
        with BytesIO() as out:
            image.save(out, fmt, **kwargs)
            return out.getvalue()


def read(env, path="image.png", **kwargs):
    return env["service"].call(env["id"], env["token"], "read_file", {
        "path": path, "start_line": 1, "max_lines": 200, "expected_sha256": None, **kwargs})


def check_preview(result):
    assert isinstance(result, ImageReadResult)
    with Image.open(BytesIO(result.data)) as image:
        image.load()
        assert image.size == (result.metadata["width"], result.metadata["height"])
        assert not image.getexif()
        assert not any(k in image.info for k in ("icc_profile", "exif", "xmp", "comment"))
    assert digest(result.data) == result.metadata["preview_sha256"]
    assert len(result.data) <= MAX_PREVIEW_BYTES
    blocks = result.tool_result()["content"]
    assert [x["type"] for x in blocks] == ["text", "image"]
    assert base64.b64decode(blocks[1]["data"], validate=True) == result.data
    assert "data" not in json.loads(blocks[0]["text"])


@pytest.mark.parametrize("fmt,ext", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"),
                                     ("GIF", "gif"), ("BMP", "bmp"), ("TIFF", "tiff")])
def test_supported_formats_use_native_blocks(env, fmt, ext):
    data = image_bytes(fmt)
    (env["root"] / ("image." + ext)).write_bytes(data)
    result = read(env, "image." + ext)
    check_preview(result)
    assert result.metadata["source_format"] == fmt
    assert result.metadata["sha256"] == digest(data)
    assert result.metadata["workspace_id"] == env["id"]
    assert result.metadata["pixel_redaction"] == "none"
    assert result.metadata["frame_policy"] == "first_frame_only"
    assert (env["root"] / ("image." + ext)).read_bytes() == data


@pytest.mark.parametrize("name", ["diagram", "diagram.txt", "diagram.JPG", "diagram.png", "diagram.webp"])
def test_signature_not_extension_decides_valid_image_format(env, name):
    (env["root"] / name).write_bytes(image_bytes("JPEG"))
    assert read(env, name).metadata["source_format"] == "JPEG"


@pytest.mark.parametrize("scope", ["none", "handoff", "workspace"])
def test_read_in_every_write_scope_without_any_mutation(env, scope):
    env["service"].manage_workspace(env["id"], "set_write_scope", write_scope=scope)
    (env["root"] / "image.png").write_bytes(image_bytes())
    folder = env["root"] / HANDOFF
    folder.mkdir()
    (folder / "shot.png").write_bytes(image_bytes())
    before = {p.relative_to(env["root"]): p.read_bytes() for p in env["root"].rglob("*") if p.is_file()}
    check_preview(read(env))
    check_preview(read(env, f"{HANDOFF}/shot.png"))
    after = {p.relative_to(env["root"]): p.read_bytes() for p in env["root"].rglob("*") if p.is_file()}
    assert before == after
    assert env["service"].workspace(env["id"])["write_scope"] == scope


def test_same_relative_path_is_bound_to_selected_workspace(env):
    (env["root"] / "image.png").write_bytes(image_bytes(size=(100, 60)))
    beta = env["parent"] / "beta"; beta.mkdir()
    (beta / "image.png").write_bytes(image_bytes(size=(90, 70)))
    ident = env["service"].add_workspace("Beta", str(beta), [])["workspace"]["id"]
    env["service"].manage_workspace(ident, "enable")
    result = env["service"].call(ident, env["token"], "read_file", {
        "path": "image.png", "start_line": 1, "max_lines": 200, "expected_sha256": None})
    assert result.metadata["source_width"] == 90
    assert read(env).metadata["source_width"] == 100
    with pytest.raises(BridgeError):
        read(env, "../beta/image.png")


@pytest.mark.parametrize("path", ["../escape.png", "/tmp/escape.png", "a/../image.png", "a\\image.png", ".env.png",
                                  "secrets/image.png", ".git/image.png", ".wb-write-x.png",
                                  f"{HANDOFF}/.env.png", f"{HANDOFF}/.wb-write-x.png", "https://example.com/a.png"])
def test_unsafe_paths_never_reach_decoder(env, monkeypatch, path):
    def forbidden(*args, **kwargs):
        raise AssertionError("decoder must not run on denied paths")
    monkeypatch.setattr("workspace_bridge.service.read_image", forbidden)
    with pytest.raises(BridgeError):
        read(env, path)


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo", "directory"])
def test_nonregular_or_linked_input_rejected(env, kind):
    target = env["root"] / "image.png"
    source = env["tmp"] / "outside.png"; source.write_bytes(image_bytes())
    if kind == "symlink": target.symlink_to(source)
    elif kind == "directory_symlink":
        (env["root"] / "shortcut").symlink_to(env["tmp"], target_is_directory=True)
        target = env["root"] / "shortcut/outside.png"
    elif kind == "hardlink": os.link(source, target)
    elif kind == "fifo": os.mkfifo(target)
    else: target.mkdir()
    with pytest.raises(BridgeError):
        read(env, str(target.relative_to(env["root"])))


def test_admin_exclusions_root_change_and_disabled_mapping(env):
    (env["root"] / "image.png").write_bytes(image_bytes())
    env["service"].manage_workspace(env["id"], "set_excludes", excludes=["*.png"])
    with pytest.raises(BridgeError): read(env)
    env["service"].manage_workspace(env["id"], "set_excludes", excludes=[])
    env["service"].manage_workspace(env["id"], "disable")
    with pytest.raises(BridgeError): read(env)
    env["service"].manage_workspace(env["id"], "enable")
    env["root"].rename(env["root"].with_name("old-root")); env["root"].mkdir()
    # Same configured path with a fresh empty directory stays usable: the
    # previous image is simply gone, not a root_changed failure.
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "not_found"


def test_stale_hash_rejected_before_decode(env, monkeypatch):
    (env["root"] / "image.png").write_bytes(image_bytes())
    original = read(env)
    (env["root"] / "image.png").write_bytes(image_bytes(size=(80, 120)))
    def forbidden(*a, **kw): raise AssertionError("must check hash before decode")
    monkeypatch.setattr("workspace_bridge.service.read_image", forbidden)
    with pytest.raises(BridgeError) as exc:
        read(env, expected_sha256=original.metadata["sha256"])
    assert exc.value.code == "stale_evidence"


def test_source_hash_can_be_reused_for_another_preview_size(env):
    (env["root"] / "image.png").write_bytes(image_bytes(size=(1600, 800)))
    a = read(env, max_image_dimension=512)
    b = read(env, max_image_dimension=1024, expected_sha256=a.metadata["sha256"])
    assert a.metadata["sha256"] == b.metadata["sha256"]
    assert a.metadata["preview_sha256"] != b.metadata["preview_sha256"]
    assert (a.metadata["width"], a.metadata["height"]) == (512, 256)
    assert b.metadata["width"] == 1024


@pytest.mark.parametrize("dimension", [None, 256, 512, 2048, 4096])
def test_dimension_cap_and_no_upscale(env, dimension):
    (env["root"] / "image.png").write_bytes(image_bytes(size=(600, 300)))
    result = read(env, max_image_dimension=dimension)
    expected = min(600, dimension or 2048)
    assert result.metadata["width"] == expected
    assert result.metadata["height"] == expected // 2


def test_preview_byte_budget_downsizes_noisy_png(env):
    with Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3)) as im:
        im.save(env["root"] / "image.png")
    assert (env["root"] / "image.png").stat().st_size > MAX_FILE
    result = read(env)
    check_preview(result)
    assert result.metadata["reduced_for_byte_limit"] and result.metadata["width"] < 2048


def test_transparency_and_embedded_metadata_are_handled(env):
    info = PngImagePlugin.PngInfo()
    info.add_text("comment", "PRIVATE-METADATA-SENTINEL")
    info.add_text("XML:com.adobe.xmp", "PRIVATE-XMP-SENTINEL")
    data = image_bytes(mode="RGBA", pnginfo=info, icc_profile=b"PRIVATE-ICC-SENTINEL")
    (env["root"] / "image.png").write_bytes(data)
    result = read(env)
    check_preview(result)
    assert b"PRIVATE" not in result.data
    assert "PRIVATE" not in json.dumps(result.metadata)
    assert result.metadata["alpha_preserved"]
    with Image.open(BytesIO(result.data)) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 70


def test_exif_orientation_is_applied_and_not_exported(env):
    exif = Image.Exif(); exif[274] = 6; exif[315] = "PRIVATE-AUTHOR-SENTINEL"
    (env["root"] / "image.jpg").write_bytes(image_bytes("JPEG", size=(120, 80), exif=exif))
    result = read(env, "image.jpg")
    check_preview(result)
    assert result.metadata["orientation_applied"]
    assert (result.metadata["width"], result.metadata["height"]) == (80, 120)
    assert b"PRIVATE-AUTHOR" not in result.data


@pytest.mark.parametrize("fmt,ext", [("GIF", "gif"), ("PNG", "png"), ("WEBP", "webp"), ("TIFF", "tiff")])
def test_only_first_frame_or_page_is_returned(env, fmt, ext):
    with Image.new("RGB", (60, 40), "red") as first, Image.new("RGB", (60, 40), "blue") as second:
        first.save(env["root"] / ("image." + ext), fmt, save_all=True, append_images=[second], duration=100)
    result = read(env, "image." + ext)
    assert result.metadata["frame"] == 0
    with Image.open(BytesIO(result.data)) as image:
        r, g, b = image.convert("RGB").getpixel((10, 10))
        assert r > 200 and b < 30
        assert not getattr(image, "is_animated", False)


@pytest.mark.parametrize("fmt,mode", [("JPEG", "CMYK"), ("PNG", "L"), ("TIFF", "I;16"), ("GIF", "P")])
def test_other_color_modes_are_previewed(env, fmt, mode):
    data = image_bytes(fmt, mode=mode)
    (env["root"] / "image").write_bytes(data)
    check_preview(read(env, "image"))


def test_input_image_limit_rejected_without_decoder(env, monkeypatch):
    (env["root"] / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_INPUT_BYTES)
    def forbidden(*a, **kw): raise AssertionError("oversized image must not decode")
    monkeypatch.setattr("workspace_bridge.service.read_image", forbidden)
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "too_large"


@pytest.mark.parametrize("name", ["large.txt", "large.png", "large"])
def test_nonimage_does_not_get_larger_read_budget(env, name):
    (env["root"] / name).write_bytes(b"x" * (MAX_FILE + 1))
    with pytest.raises(BridgeError) as exc: read(env, name)
    assert exc.value.code == "too_large"


def test_pixel_bomb_rejected_by_worker(env):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    # Valid PNG chunk CRCs, but a >40MP header; never allocate the stated pixels.
    huge = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 9000, 9000, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    (env["root"] / "image.png").write_bytes(huge)
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "image_limit"


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\nnot an image", b"\xff\xd8\xff" + b"x" * 80,
                                  b"GIF89a" + b"x" * 50, b"BM" + b"x" * 50,
                                  b"RIFFxxxxWEBP" + b"x" * 50, b"II*\x00" + b"x" * 50])
def test_corrupt_recognized_format_has_safe_error(env, data):
    (env["root"] / "image.png").write_bytes(data)
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code in ("invalid_image", "image_limit")
    assert "not an image" not in str(exc.value)


def test_truncated_png_fails(env):
    (env["root"] / "image.png").write_bytes(image_bytes()[:45])
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "invalid_image"


@pytest.mark.parametrize("name,data", [("x.svg", b"<svg></svg>"), ("x.pdf", b"%PDF-1.7\n"),
                                       ("x.eps", b"%!PS-Adobe-3.0"), ("x.heic", b"\x00\x00\x00\x18ftypheic")])
def test_no_external_document_or_vector_renderer(env, name, data, monkeypatch):
    (env["root"] / name).write_bytes(data)
    def forbidden(*a, **kw): raise AssertionError("unsupported format must not launch decoder")
    monkeypatch.setattr("workspace_bridge.media.subprocess.run", forbidden)
    with pytest.raises(BridgeError) as exc: read(env, name, representation="image")
    assert exc.value.code == "unsupported_image"
    if name.endswith(".svg"):
        assert read(env, name)["lines"][0]["text"] == "<svg></svg>"


def test_forced_text_and_explicit_image_mode(env):
    (env["root"] / "image.png").write_bytes(image_bytes())
    with pytest.raises(BridgeError) as exc: read(env, representation="text")
    assert exc.value.code == "binary_file"
    check_preview(read(env, representation="image"))
    (env["root"] / "named.png").write_text("actually plain text\n")
    assert read(env, "named.png", representation="text")["lines"][0]["text"] == "actually plain text"
    with pytest.raises(BridgeError) as exc: read(env, "named.png")
    assert exc.value.code == "unsupported_image"


def test_text_compatibility_and_image_parameter_misuse(env):
    text = read(env, "README.md", start_line=2, max_lines=1)
    assert text["lines"] == [{"line": 2, "text": "Small example project"}]
    assert text["next_line"] is None
    with pytest.raises(BridgeError): read(env, "README.md", max_image_dimension=512)
    (env["root"] / "image.png").write_bytes(image_bytes())
    for kw in ({"start_line": 2}, {"max_lines": 100}, {"max_image_dimension": 99}, {"representation": "pdf"}):
        with pytest.raises(BridgeError) as exc: read(env, **kw)
        assert exc.value.code == "invalid_arguments"


def test_metadata_and_pixels_not_written_to_events(env):
    (env["root"] / "private-filename.png").write_bytes(image_bytes())
    result = read(env, "private-filename.png")
    rows = [dict(row) for row in env["service"].db.execute("SELECT * FROM events")]
    log = json.dumps(rows)
    assert "read_file" in log and "private-filename" not in log
    assert result.metadata["sha256"] not in log
    assert base64.b64encode(result.data).decode() not in log


def test_worker_is_fixed_isolated_bytes_only(env, monkeypatch):
    from workspace_bridge import media
    (env["root"] / "image.png").write_bytes(image_bytes())
    real = media.subprocess.run
    def inspect(command, **kw):
        assert command[1:3] == ["-I", "-B"]
        assert Path(command[3]).name == "image_worker.py"
        assert str(env["root"]) not in str(command)
        assert kw["input"] == image_bytes()
        assert kw["env"] == {"PATH": os.defpath}
        assert kw["close_fds"] is True and not kw.get("shell")
        return real(command, **kw)
    monkeypatch.setattr(media.subprocess, "run", inspect)
    check_preview(read(env))


def test_worker_timeout_and_crash_are_safe_errors(env, monkeypatch):
    (env["root"] / "image.png").write_bytes(image_bytes())
    def timeout(*a, **kw): raise subprocess.TimeoutExpired(a[0], 10)
    monkeypatch.setattr("workspace_bridge.media.subprocess.run", timeout)
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "image_timeout"
    monkeypatch.setattr("workspace_bridge.media.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a[0], -9))
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "image_decode_failed"


def test_real_worker_timeout_is_killed(env, monkeypatch):
    (env["root"] / "image.png").write_bytes(image_bytes())
    monkeypatch.setattr("workspace_bridge.media.IMAGE_TIMEOUT_SECONDS", .000001)
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == "image_timeout"


@pytest.mark.parametrize("output,expected", [(b"not json", "image_decode_failed"), (b"[]", "image_decode_failed"),
                                           (b'{"error":"image_decoder_unavailable"}', "image_decoder_unavailable"),
                                           (b'{"data":"bad","mime_type":"image/png"}', "image_decode_failed"),
                                           (b"x" * (MAX_IMAGE_RESPONSE_BYTES + 1), "image_limit")])
def test_invalid_worker_output_is_never_exposed(env, monkeypatch, output, expected):
    (env["root"] / "image.png").write_bytes(image_bytes())
    monkeypatch.setattr("workspace_bridge.media.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=output))
    with pytest.raises(BridgeError) as exc: read(env)
    assert exc.value.code == expected


def test_typed_result_has_separate_bounded_native_output():
    for result in (ImageReadResult({}, b"x" * (MAX_PREVIEW_BYTES + 1), "image/png"),
                   ImageReadResult({}, b"x", "text/html"),
                   ImageReadResult({"x": "x" * 25000}, b"x", "image/png")):
        with pytest.raises(BridgeError): result.tool_result()


def test_image_read_never_changes_migration_or_writing(env, payload):
    env["service"].manage_workspace(env["id"], "set_write_scope", write_scope="none")
    before = env["service"].workspace(env["id"])
    (env["root"] / "image.png").write_bytes(image_bytes())
    read(env)
    assert env["service"].workspace(env["id"]) == before
    with pytest.raises(BridgeError):
        env["service"].call(env["id"], env["token"], "prepare_handoff", payload)
    # Reopening existing state must preserve the explicitly selected no-write policy.
    other = Service(env["state"], env["config"])
    try: assert other.workspace(env["id"])["write_scope"] == "none"
    finally: other.close()


def test_skill_and_capabilities_describe_image_boundary(env):
    skill = read_project_lead_skill()
    assert skill["version"] == "3.0.0"
    for fragment in ("native image", "visible secrets", "first frame", "text-only", "actually"):
        assert fragment in skill["content"]
    info = env["service"].info(env["service"].workspace(env["id"]))
    assert info["image_reading"]["formats"] == ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"]
    assert info["limits"]["max_file_bytes"] == MAX_FILE
    assert info["image_reading"]["max_input_bytes"] == MAX_INPUT_BYTES
    assert len(TOOLS) == 26
    schema = TOOLS["read_file"][0].model_json_schema()
    assert schema["properties"]["representation"]["default"] == "auto"
    assert "representation" not in schema["required"]


async def rpc(env, args, version=LEGACY[-1], token=None):
    params = {"name": "read_file", "arguments": {"workspace_id": env["id"], **args}}
    headers = {"X-Bridge-Token": env["token"] if token is None else token,
               "Accept": "application/json", "MCP-Protocol-Version": version}
    if version == MODERN:
        params["_meta"] = {PREFIX + "protocolVersion": version, PREFIX + "clientCapabilities": {}}
        headers.update({"Mcp-Method": "tools/call", "Mcp-Name": "read_file"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        return await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                                                               "method": "tools/call", "params": params})


@pytest.mark.parametrize("version", [*LEGACY, MODERN])
async def test_every_protocol_returns_actual_native_image(env, version):
    (env["root"] / "image.png").write_bytes(image_bytes())
    response = await rpc(env, {"path": "image.png"}, version)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    value = response.json()["result"]
    assert value["isError"] is False
    assert [b["type"] for b in value["content"]] == ["text", "image"]
    text, block = value["content"]
    assert block["mimeType"] == "image/png" and block["data"] not in text["text"]
    assert "_meta" not in block  # Do not deliver pixels through hidden/UI-only metadata.
    preview = base64.b64decode(block["data"], validate=True)
    with Image.open(BytesIO(preview)) as image: assert image.size == (120, 80)
    if version == MODERN: assert value["resultType"] == "complete"
    assert len(response.content) <= MAX_IMAGE_RESPONSE_BYTES


@pytest.mark.parametrize("args", [{"representation": "binary"}, {"max_image_dimension": 0},
                                  {"max_image_dimension": True}, {"max_image_dimension": 4097},
                                  {"max_image_dimension": "512"}, {"frame": 1}, {"url": "http://example.com/a.png"},
                                  {"write_scope": "workspace"}, {"representation": None}])
async def test_invalid_image_api_arguments(env, args):
    response = await rpc(env, {"path": "image.png", **args})
    assert response.json()["error"]["code"] == -32602


async def test_native_image_requires_valid_token_and_enablement(env):
    (env["root"] / "image.png").write_bytes(image_bytes())
    assert (await rpc(env, {"path": "image.png"}, token="wrong")).status_code == 401
    env["service"].manage_bridge("disable")
    assert (await rpc(env, {"path": "image.png"})).status_code == 401
    env["service"].manage_bridge("enable")
    env["service"].manage_bridge("rotate_token")
    assert (await rpc(env, {"path": "image.png"})).status_code == 401


async def test_large_native_output_not_treated_as_text_but_text_limits_stay(env):
    with Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3)) as im:
        im.save(env["root"] / "image.png")
    response = await rpc(env, {"path": "image.png"})
    assert not response.json()["result"]["isError"] and len(response.content) > 24000
    (env["root"] / "oversized-line.txt").write_text("x" * 30000)
    text = await rpc(env, {"path": "oversized-line.txt"})
    assert text.json()["result"]["isError"]
    assert json.loads(text.json()["result"]["content"][0]["text"])["error"] == "output_limit"


def test_image_context_hash_uses_source_bytes_not_preview(env, payload):
    with Image.frombytes("RGB", (500, 500), os.urandom(500 * 500 * 3)) as image:
        image.save(env["root"] / "image.png")
    result = read(env)
    assert result.metadata["size_bytes"] > MAX_FILE
    payload["context_hashes"] = {"image.png": result.metadata["sha256"]}
    job = env["service"].call(env["id"], env["token"], "prepare_handoff", payload)
    assert job["completion_tracking"] == "not_tracked"
    payload["request_id"] = "bad-preview-hash"
    payload["context_hashes"]["image.png"] = result.metadata["preview_sha256"]
    # Re-encoding is not assumed to produce identical bytes.
    if result.metadata["preview_sha256"] != result.metadata["sha256"]:
        with pytest.raises(BridgeError) as exc:
            env["service"].call(env["id"], env["token"], "prepare_handoff", payload)
        assert exc.value.code == "stale_context"


@pytest.mark.parametrize('content', ['BM25 documentation\n', 'GIF89a file format notes\n'])
def test_explicit_text_keeps_old_decoding_semantics(env, content):
    (env['root'] / 'format-notes.txt').write_text(content)
    result = read(env, 'format-notes.txt', representation='text')
    assert result['lines'][0]['text'] == content.strip()


def test_targeted_context_hashes_have_an_aggregate_budget(env, payload, monkeypatch):
    data = b'\x89PNG\r\n\x1a\n' + b'x' * (20 * 1024 * 1024 - 8)
    payload['context_hashes'] = {f'image-{i}.png': digest(data) for i in range(4)}
    monkeypatch.setattr(SafeRoot, 'read', lambda *args, **kw: (data, None))
    with pytest.raises(BridgeError) as exc:
        env['service'].call(env['id'], env['token'], 'prepare_handoff', payload)
    assert exc.value.code == 'context_limit'
    assert not (env['root'] / HANDOFF).exists()
