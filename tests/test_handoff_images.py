"""Image uploads kept in Node state and image references in handoffs.

Uploads go through the real MCP and Node APIs into isolated temporary Node
state; nothing is written into the workspace.
"""
import base64
from io import BytesIO
import json
import random

import httpx
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
import pytest

from workspace_bridge import plan_files
from workspace_bridge.api import DESTRUCTIVE_TOOLS, Handoff, TOOLS, make_mcp
from workspace_bridge.media import (IMAGE_MIME_TYPES, MAX_IMAGE_BASE64_CHARS,
                                    MAX_IMAGE_REQUEST_BYTES, MAX_INPUT_BYTES)
from mcp_helpers import McpTransport, MODERN, PREFIX, VERSIONS
from workspace_bridge.security import BridgeError, digest


def raster(fmt="PNG", *, size=(80, 60), noisy=False, **kwargs):
    if noisy:
        image = Image.frombytes("RGB", size, random.Random(123).randbytes(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (20, 90, 160))
    with image, BytesIO() as out:
        image.save(out, fmt, **kwargs)
        return out.getvalue()


def upload(env, data=None, mime_type="image/png", **kwargs):
    return env["service"].call(env["id"], env["token"], "upload_image", {
        "data_base64": base64.b64encode(data if data is not None else raster()).decode(),
        "mime_type": mime_type, **kwargs})


def uploads(env):
    folder = env["node"]["service"].state / "uploads" / env["id"]
    return sorted(folder.iterdir()) if folder.exists() else []


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def publish(env, payload, attachments):
    args = Handoff.model_validate({**payload, "attachments": attachments}).model_dump()
    return env["service"].call(env["id"], env["token"], "save_handoff", args)


def reference(sha, path=None):
    item = {"description": "Use this screenshot to reproduce the settings layout.", "sha256": sha}
    return item if path is None else {**item, "path": path}


def manifest(env, job):
    text = json.loads(env["service"].job(env["service"].workspace(env["id"]), job["id"])["content"])["CONTEXT.md"]
    return json.loads(text.split("## Image attachments", 1)[1].split("```json\n", 1)[1].split("```", 1)[0])


@pytest.mark.parametrize("fmt,ext", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"),
                                    ("GIF", "gif"), ("BMP", "bmp"), ("TIFF", "tiff")])
def test_upload_keeps_original_bytes_in_node_state_not_the_workspace(env, fmt, ext):
    before = tree(env["root"])
    data = raster(fmt)
    written = upload(env, data, IMAGE_MIME_TYPES[fmt])
    assert written["sha256"] == digest(data) and written["bytes"] == len(data)
    assert written["source_format"] == fmt and (written["width"], written["height"]) == (80, 60)
    assert written["stored"] == "node_state" and written["file"] == f"{digest(data)}.{ext}"
    [stored] = uploads(env)
    assert stored.name == written["file"] and stored.read_bytes() == data
    assert stored.stat().st_mode & 0o777 == 0o400
    assert tree(env["root"]) == before


def test_upload_keeps_metadata(env):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "Reference capture metadata")
    data = raster(pnginfo=metadata)
    assert upload(env, data)["embedded_metadata"] == "preserved"
    assert uploads(env)[0].read_bytes() == data


def test_repeated_upload_is_the_same_file(env):
    first, second = upload(env), upload(env)
    assert first == second and len(uploads(env)) == 1


def test_read_only_workspace_takes_uploads(env):
    assert env["service"].workspace(env["id"])["write_scope"] == "none"
    before = tree(env["root"])
    upload(env)
    assert len(uploads(env)) == 1 and tree(env["root"]) == before


@pytest.mark.parametrize("policy", ["disabled", "revoked"])
def test_upload_auth_fails_before_decoding(env, policy, monkeypatch):
    service = env["service"]
    if policy == "disabled":
        service.manage_workspace(env["id"], "disable")
    else:
        service.manage_bridge("rotate_token")
    monkeypatch.setattr("workspace_bridge.media.read_image", lambda *a, **k: pytest.fail("denied upload decoded"))
    with pytest.raises(BridgeError):
        upload(env)
    assert uploads(env) == []


@pytest.mark.parametrize("kind", ["mime", "corrupt", "text", "base64", "data_url", "oversized"])
def test_invalid_upload_stores_nothing(env, kind):
    args = {"data_base64": base64.b64encode(raster()).decode(), "mime_type": "image/png"}
    if kind == "mime":
        args["mime_type"] = "image/jpeg"
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
    assert uploads(env) == []


def test_uploads_are_capped_per_workspace(env, monkeypatch):
    first = upload(env)
    monkeypatch.setattr(plan_files, "MAX_UPLOAD_BYTES", first["bytes"] + 10)
    assert upload(env) == first  # The same image takes no more room.
    with pytest.raises(BridgeError) as exc:
        upload(env, raster(size=(60, 80)))
    assert exc.value.code == "storage_limit"
    assert len(uploads(env)) == 1


def test_uploaded_attachment_is_copied_into_the_plan_folder(env, payload):
    written = upload(env)
    attachments = [reference(written["sha256"])]
    job = publish(env, payload, attachments)
    again = publish(env, payload, attachments)
    assert job["id"] == again["id"] and job["state"] == "prepared"
    assert manifest(env, job) == [{**attachments[0], "mime_type": "image/png", "source_format": "PNG",
                                   "width": 80, "height": 60, "bytes": written["bytes"],
                                   "file": "attachments/" + written["file"]}]
    copy = env["node"]["service"].state / "handoffs" / env["id"] / job["id"] / "attachments" / written["file"]
    assert job["plan_folder"] == str(copy.parent.parent)
    assert copy.read_bytes() == raster() and copy.stat().st_mode & 0o777 == 0o400
    assert "attachments/" in job["copy_prompt"]
    assert env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0
    # A rewrite of the plan restores a changed copy.
    copy.chmod(0o600)
    copy.write_bytes(b"changed")
    env["service"].write_plan(env["service"].workspace(env["id"]),
                              env["service"].job(env["service"].workspace(env["id"]), job["id"]))
    assert copy.read_bytes() == raster()
    with pytest.raises(BridgeError) as exc:
        publish(env, payload, [{**attachments[0], "description": "A different purpose"}])
    assert exc.value.code == "conflict"


@pytest.mark.parametrize("kind", ["stale", "missing", "excluded", "text", "secret_description", "unknown_upload"])
def test_invalid_attachments_fail_before_publishing(env, payload, kind):
    path = "reference.png"
    data = b"text" if kind == "text" else raster()
    (env["root"] / path).write_bytes(data)
    attachment = reference("0" * 64 if kind == "stale" else digest(data), path)
    if kind == "missing":
        attachment["path"] = "missing.png"
    elif kind == "excluded":
        env["service"].manage_workspace(env["id"], "set_excludes", ["*.png"])
    elif kind == "secret_description":
        attachment["description"] = "sk-" + "A" * 30
    elif kind == "unknown_upload":
        attachment = reference(digest(data))  # Never uploaded.
    with pytest.raises(BridgeError):
        publish(env, payload, [attachment])
    assert env["service"].db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_attachment_schema_rejects_duplicates_conflicting_hashes_and_unknown_fields(payload):
    attachment = reference("a" * 64, "reference.png")
    for changes in ({"attachments": [attachment, attachment]},
                    {"attachments": [reference("a" * 64), reference("a" * 64)]},
                    {"attachments": [attachment], "context_hashes": {"reference.png": "b" * 64}},
                    {"attachments": [{**attachment, "url": "https://example.invalid/image.png"}]}):
        with pytest.raises(ValidationError):
            Handoff.model_validate({**payload, **changes})


def test_existing_workspace_image_is_referenced_in_place(env, payload):
    path = "reference.jpg"
    data = raster("JPEG")
    (env["root"] / path).write_bytes(data)
    job = publish(env, {**payload, "context_hashes": {path: digest(data)}}, [reference(digest(data), path)])
    [item] = manifest(env, job)
    assert item["path"] == path and item["mime_type"] == "image/jpeg" and "file" not in item
    assert not (env["node"]["service"].state / "handoffs" / env["id"] / job["id"] / "attachments").exists()
    assert (env["root"] / path).read_bytes() == data


def test_empty_attachments_preserve_previous_handoff_retry_identity(env, payload):
    job = env["service"].call(env["id"], env["token"], "save_handoff", payload)
    assert publish(env, payload, [])["id"] == job["id"]


def test_node_rejects_unexpected_upload_arguments(env):
    workspace = env["service"].workspace(env["id"])
    response = env["node"]["transport"].request("POST", "/v1/workspaces/upload_image", json.dumps({
            "workspace": {"id": workspace["id"], "root": workspace["root"],
                          "excludes": json.loads(workspace["excludes"]), "write_scope": "none"},
            "arguments": {"path": "src/reference.png", "mime_type": "image/png",
                          "data_base64": base64.b64encode(raster()).decode()}}).encode())
    assert response.status_code == 400
    assert json.loads(response.content)["code"] == "invalid_arguments"
    assert uploads(env) == []


def test_pixel_limit_and_decoder_timeout_store_nothing(env, monkeypatch):
    with pytest.raises(BridgeError) as exc:
        upload(env, raster(size=(10000, 5000)))
    assert exc.value.code == "image_limit"

    def timeout(*args, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired("fixed-image-decoder", 10)

    monkeypatch.setattr("workspace_bridge.media.subprocess.run", timeout)
    with pytest.raises(BridgeError) as exc:
        upload(env)
    assert exc.value.code == "image_timeout"
    assert uploads(env) == []


async def rpc(client, env, version, name, arguments):
    params = {"name": name, "arguments": {"workspace_id": env["id"], **arguments}}
    headers = {"X-Bridge-Token": env["token"], "Accept": "application/json", "MCP-Protocol-Version": version}
    if version == MODERN:
        params["_meta"] = {PREFIX + "protocolVersion": version, PREFIX + "clientCapabilities": {}}
        headers.update({"Mcp-Method": "tools/call", "Mcp-Name": name})
    return await client.post("/mcp", headers=headers,
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


@pytest.mark.parametrize("version", VERSIONS)
async def test_large_image_upload_and_publication_over_http(env, payload, version):
    # Crosses the 1 MiB MCP and 2 MiB Node client limits other calls keep.
    data = raster(size=(1100, 900), noisy=True)
    assert len(data) > 2 * 1024 * 1024
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        response = await rpc(client, env, version, "upload_image", {
            "data_base64": base64.b64encode(data).decode(), "mime_type": "image/png"})
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result["isError"], result
        written = json.loads(result["content"][0]["text"])
        prepared = await rpc(client, env, version, "save_handoff", {
            **payload, "attachments": [reference(written["sha256"])]})
        job = json.loads(prepared.json()["result"]["content"][0]["text"])
        assert (env["node"]["service"].state / "handoffs" / env["id"] / job["id"] / "attachments"
                / written["file"]).read_bytes() == data
        # Only an image upload may use the large body; text keeps its own bound.
        too_large_text = await rpc(client, env, version, "apply_patch", {
            "message": "Add a large note.",
            "changes": [{"op": "create", "path": "blocked.md", "content": "x" * (1024 * 1024 + 1)}]})
        assert too_large_text.json()["error"]["code"] == -32602
        assert not (env["root"] / "blocked.md").exists()


def test_image_capabilities_and_tool_permissions(env):
    schema, _, readonly, idempotent = TOOLS["upload_image"]
    assert not readonly and idempotent and "upload_image" not in DESTRUCTIVE_TOOLS
    assert set(schema.model_json_schema()["required"]) == {"workspace_id", "data_base64", "mime_type"}
    assert "path" not in schema.model_json_schema()["properties"]
    info = env["service"].info(env["service"].workspace(env["id"]))
    assert info["image_writing"]["max_input_bytes"] == MAX_INPUT_BYTES
    assert info["image_writing"]["max_request_bytes"] == MAX_IMAGE_REQUEST_BYTES
    assert info["image_writing"]["stored"] == "node_state"
