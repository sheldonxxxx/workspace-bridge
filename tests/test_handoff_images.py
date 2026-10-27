"""Image upload and reference publication through the real MCP and Node APIs.

All artifacts use isolated temporary workspaces, never repository handoff state.
"""
import base64
from io import BytesIO
import json
import os
import random

import httpx
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
import pytest

from workspace_bridge.api import Handoff, TOOLS, make_mcp
from workspace_bridge.media import (IMAGE_MIME_TYPES, MAX_IMAGE_BASE64_CHARS,
                                    MAX_IMAGE_REQUEST_BYTES, MAX_INPUT_BYTES)
from mcp_helpers import McpTransport, MODERN, PREFIX, VERSIONS
from workspace_bridge.security import BridgeError, HANDOFF, digest


PATH = HANDOFF + "/attachments/settings.png"



@pytest.fixture(autouse=True)
def _edit_scope(request):
    """Notes and images under .workspace-handoff/ are ordinary edits now."""
    if "env" in request.fixturenames:
        env = request.getfixturevalue("env")
        env["service"].manage_workspace(env["id"], "set_write_scope", write_scope="workspace")

def raster(fmt="PNG", *, size=(80, 60), noisy=False, **kwargs):
    if noisy:
        image = Image.frombytes("RGB", size, random.Random(123).randbytes(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (20, 90, 160))
    with image, BytesIO() as out:
        image.save(out, fmt, **kwargs)
        return out.getvalue()


def upload(env, data=None, path=PATH, mime_type="image/png", **kwargs):
    return env["service"].call(env["id"], env["token"], "upload_image", {
        "path": path, "data_base64": base64.b64encode(data if data is not None else raster()).decode(),
        "mime_type": mime_type, **kwargs})


def publish(env, payload, attachments):
    args = Handoff.model_validate({**payload, "attachments": attachments}).model_dump()
    return env["service"].call(env["id"], env["token"], "save_handoff", args)


def reference(path, sha):
    return {"path": path, "description": "Use this screenshot to reproduce the settings layout.", "sha256": sha}


@pytest.mark.parametrize("fmt,ext", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"),
                                    ("GIF", "gif"), ("BMP", "bmp"), ("TIFF", "tiff")])
def test_upload_preserves_original_and_can_be_read_as_native_image(env, fmt, ext):
    data = raster(fmt)
    path = HANDOFF + "/attachments/reference." + ext
    written = upload(env, data, path, IMAGE_MIME_TYPES[fmt])
    assert written["sha256"] == digest(data)
    assert written["bytes"] == len(data) and written["created"]
    assert written["source_format"] == fmt
    assert (written["width"], written["height"]) == (80, 60)
    target = env["root"] / path
    assert target.read_bytes() == data and target.stat().st_mode & 0o777 == 0o600
    preview = env["service"].call(env["id"], env["token"], "read_file", {
        "path": path, "start_line": 1, "max_lines": 200, "expected_sha256": written["sha256"]})
    assert [b["type"] for b in preview.tool_result()["content"]] == ["text", "image"]


def test_upload_keeps_metadata_while_reader_strips_it(env):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "Reference capture metadata")
    data = raster(pnginfo=metadata)
    written = upload(env, data)
    assert written["embedded_metadata"] == "preserved"
    assert (env["root"] / PATH).read_bytes() == data
    preview = env["service"].call(env["id"], env["token"], "read_file", {
        "path": PATH, "start_line": 1, "max_lines": 200, "expected_sha256": None})
    with Image.open(BytesIO(preview.data)) as image:
        assert "Comment" not in image.info


def test_upload_is_handoff_only_even_with_workspace_scope(env, monkeypatch):
    monkeypatch.setattr("workspace_bridge.media.read_image", lambda *a, **k: pytest.fail("denied path decoded"))
    with pytest.raises(BridgeError) as exc:
        upload(env, path="src/reference.png")
    assert exc.value.code == "policy_denied"
    assert not (env["root"] / HANDOFF).exists()


@pytest.mark.parametrize("policy", ["none", "excluded", "disabled", "revoked"])
def test_upload_policy_and_auth_fail_before_decoding(env, policy, monkeypatch):
    service = env["service"]
    if policy == "none":
        service.manage_workspace(env["id"], "set_write_scope", write_scope="none")
    elif policy == "excluded":
        service.manage_workspace(env["id"], "set_excludes", ["*.png"])
    elif policy == "disabled":
        service.manage_workspace(env["id"], "disable")
    else:
        service.manage_bridge("rotate_token")
    monkeypatch.setattr("workspace_bridge.media.read_image", lambda *a, **k: pytest.fail("denied upload decoded"))
    with pytest.raises(BridgeError):
        upload(env)
    assert not (env["root"] / HANDOFF).exists()


@pytest.mark.parametrize("path", [HANDOFF + "/../escape.png", "/tmp/escape.png",
                                  HANDOFF + "/.env.png", HANDOFF + "/secrets/ref.png",
                                  HANDOFF + "/.wb-write-hidden.png"])
def test_unsafe_paths_do_not_create_artifacts(env, path):
    with pytest.raises(BridgeError):
        upload(env, path=path)
    assert not (env["root"] / HANDOFF).exists()


@pytest.mark.parametrize("kind", ["mime", "suffix", "corrupt", "text", "base64", "data_url", "oversized"])
def test_invalid_upload_never_creates_parent_directories(env, kind):
    args = {"path": PATH, "data_base64": base64.b64encode(raster()).decode(), "mime_type": "image/png"}
    if kind == "mime":
        args["mime_type"] = "image/jpeg"
    elif kind == "suffix":
        args["path"] = HANDOFF + "/attachments/reference.py"
    elif kind in {"corrupt", "text"}:
        args["data_base64"] = base64.b64encode(b"\x89PNG\r\n\x1a\ncorrupt" if kind == "corrupt" else b"text").decode()
    elif kind == "base64":
        args["data_base64"] = "%%%"
    elif kind == "data_url":
        args["data_base64"] = "data:image/png;base64," + args["data_base64"]
    else:
        args["data_base64"] = "A" * (MAX_IMAGE_BASE64_CHARS + 4)
    with pytest.raises(BridgeError):
        env["service"].call(env["id"], env["token"], "upload_image", args)
    assert not (env["root"] / HANDOFF).exists()


def test_create_only_and_hash_protected_replacement(env):
    first = upload(env)
    with pytest.raises(BridgeError) as conflict:
        upload(env)
    assert conflict.value.code == "conflict"
    updated = raster(size=(60, 80))
    with pytest.raises(BridgeError) as stale:
        upload(env, updated, expected_sha256="0" * 64)
    assert stale.value.code == "stale_evidence"
    assert digest((env["root"] / PATH).read_bytes()) == first["sha256"]
    second = upload(env, updated, expected_sha256=first["sha256"])
    assert not second["created"] and second["previous_sha256"] == first["sha256"]
    assert (env["root"] / PATH).read_bytes() == updated


@pytest.mark.parametrize("kind", ["text", "corrupt", "symlink", "hardlink"])
def test_upload_cannot_replace_other_content_or_follow_links(env, kind):
    target = env["root"] / PATH
    target.parent.mkdir(parents=True)
    original = b"local text" if kind == "text" else b"\x89PNG\r\n\x1a\ncorrupt" if kind == "corrupt" else raster()
    outside = env["tmp"] / "outside.png"
    outside.write_bytes(original)
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        target.write_bytes(original)
    with pytest.raises(BridgeError):
        upload(env, expected_sha256=digest(original))
    assert target.read_bytes() == original and outside.read_bytes() == original


def test_attachment_manifest_uses_original_hash_and_retries_keep_same_job(env, payload):
    written = upload(env)
    attachments = [reference(PATH, written["sha256"])]
    job = publish(env, payload, attachments)
    again = publish(env, payload, attachments)
    assert job["id"] == again["id"] and job["state"] == "prepared"
    text = json.loads(env["service"].job(env["service"].workspace(env["id"]), job["id"])["content"])["CONTEXT.md"]
    manifest = json.loads(text.split("## Image attachments", 1)[1].split("```json\n", 1)[1].split("```", 1)[0])
    assert manifest == [{**attachments[0], "mime_type": "image/png", "source_format": "PNG",
                         "width": 80, "height": 60, "bytes": written["bytes"]}]
    assert (env["root"] / PATH).read_bytes() == raster()
    assert env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0
    changed = [{**attachments[0], "description": "A different purpose"}]
    with pytest.raises(BridgeError) as exc:
        publish(env, payload, changed)
    assert exc.value.code == "conflict"


@pytest.mark.parametrize("kind", ["stale", "missing", "excluded", "text", "secret_description"])
def test_invalid_attachments_fail_before_publishing_documents(env, payload, kind):
    path = "reference.png"
    data = b"text" if kind == "text" else raster()
    (env["root"] / path).write_bytes(data)
    attachment = reference(path, "0" * 64 if kind == "stale" else digest(data))
    if kind == "missing":
        attachment["path"] = "missing.png"
    elif kind == "excluded":
        env["service"].manage_workspace(env["id"], "set_excludes", ["*.png"])
    elif kind == "secret_description":
        attachment["description"] = "sk-" + "A" * 30
    with pytest.raises(BridgeError):
        publish(env, payload, [attachment])
    assert not (env["root"] / HANDOFF).exists()


def test_attachment_schema_rejects_duplicates_conflicting_hashes_and_unknown_fields(payload):
    attachment = reference(PATH, "a" * 64)
    for changes in ({"attachments": [attachment, attachment]},
                    {"attachments": [attachment], "context_hashes": {PATH: "b" * 64}},
                    {"attachments": [{**attachment, "url": "https://example.invalid/image.png"}]}):
        with pytest.raises(ValidationError):
            Handoff.model_validate({**payload, **changes})


def test_existing_workspace_image_and_matching_context_hash_are_supported(env, payload):
    path = "reference.jpg"
    data = raster("JPEG")
    (env["root"] / path).write_bytes(data)
    job = publish(env, {**payload, "context_hashes": {path: digest(data)}}, [reference(path, digest(data))])
    context = json.loads(env["service"].job(env["service"].workspace(env["id"]), job["id"])["content"])["CONTEXT.md"]
    assert '"mime_type": "image/jpeg"' in context
    assert (env["root"] / path).read_bytes() == data


def test_empty_attachments_preserve_previous_handoff_retry_identity(env, payload):
    job = env["service"].call(env["id"], env["token"], "save_handoff", payload)
    assert publish(env, payload, [])["id"] == job["id"]


def test_node_rejects_forged_scope_before_image_upload(env):
    env["service"].manage_workspace(env["id"], "set_write_scope", write_scope="none")
    # Use the Bridge's stored mapping envelope, but falsely claim a wider scope.
    workspace = env["service"].workspace(env["id"])
    response = env["node"]["transport"].request("POST", "/v1/workspaces/write_image", json.dumps({
            "workspace": {"id": workspace["id"], "root": workspace["root"],
                          "excludes": json.loads(workspace["excludes"]), "write_scope": "workspace"},
            "arguments": {"path": PATH, "mime_type": "image/png",
                          "data_base64": base64.b64encode(raster()).decode()}}).encode())
    assert response.status_code == 400
    assert json.loads(response.content)["code"] == "policy_denied"
    assert not (env["root"] / HANDOFF).exists()


def test_pixel_limit_and_decoder_timeout_fail_without_creating_artifacts(env, monkeypatch):
    with pytest.raises(BridgeError) as exc:
        upload(env, raster(size=(10000, 5000)))
    assert exc.value.code == "image_limit"
    assert not (env["root"] / HANDOFF).exists()

    def timeout(*args, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired("fixed-image-decoder", 10)

    monkeypatch.setattr("workspace_bridge.media.subprocess.run", timeout)
    with pytest.raises(BridgeError) as exc:
        upload(env)
    assert exc.value.code == "image_timeout"
    assert not (env["root"] / HANDOFF).exists()


async def rpc(client, env, version, name, arguments):
    params = {"name": name, "arguments": {"workspace_id": env["id"], **arguments}}
    headers = {"X-Bridge-Token": env["token"], "Accept": "application/json", "MCP-Protocol-Version": version}
    if version == MODERN:
        params["_meta"] = {PREFIX + "protocolVersion": version, PREFIX + "clientCapabilities": {}}
        headers.update({"Mcp-Method": "tools/call", "Mcp-Name": name})
    return await client.post("/mcp", headers=headers,
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


@pytest.mark.parametrize("version", VERSIONS)
async def test_large_image_upload_publication_and_native_readback_over_both_http_apis(env, payload, version):
    # Crosses both the old MCP/Node server 1 MiB and Node client 2 MiB limits.
    data = raster(size=(1100, 900), noisy=True)
    assert len(data) > 2 * 1024 * 1024
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        response = await rpc(client, env, version, "upload_image", {
            "path": PATH, "data_base64": base64.b64encode(data).decode(), "mime_type": "image/png"})
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result["isError"], result
        written = json.loads(result["content"][0]["text"])
        prepared = await rpc(client, env, version, "save_handoff", {
            **payload, "attachments": [reference(PATH, written["sha256"])]})
        assert not prepared.json()["result"]["isError"], prepared.text
        readback = await rpc(client, env, version, "read_file", {"path": PATH, "expected_sha256": written["sha256"]})
        assert [b["type"] for b in readback.json()["result"]["content"]] == ["text", "image"]
        assert (env["root"] / PATH).read_bytes() == data
        # Only an image upload may use the large body; text keeps its own bound.
        too_large_text = await rpc(client, env, version, "apply_patch", {
            "message": "Add a large note.",
            "changes": [{"op": "create", "path": "blocked.md", "content": "x" * (1024 * 1024 + 1)}]})
        assert too_large_text.json()["error"]["code"] == -32602
        assert not (env["root"] / "blocked.md").exists()


def test_image_capabilities_and_tool_permissions(env):
    schema, _, readonly, idempotent = TOOLS["upload_image"]
    assert not readonly and not idempotent
    assert "workspace_id" in schema.model_json_schema()["required"]
    info = env["service"].info(env["service"].workspace(env["id"]))
    assert info["image_writing"]["max_input_bytes"] == MAX_INPUT_BYTES
    assert info["image_writing"]["max_request_bytes"] == MAX_IMAGE_REQUEST_BYTES
    assert info["image_writing"]["path_prefix"] == HANDOFF + "/"
