"""M4.3B support bundle projection, sanitization and ZIP safety."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import zipfile

import pytest

from workspace_bridge import support_bundle
from workspace_bridge.support_bundle import (
    collect_file_excerpts,
    collect_journal_excerpts,
    project_adapter_status,
    project_diagnostics,
    project_node_status,
    sanitize_support_line,
)


FORBIDDEN_TOKEN = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX1234567890abcdef"
FORBIDDEN_BEARER = "Bearer abcdefghijklmnopqrstuvwx123456"
FORBIDDEN_HOME = "/Users/testuser/secrets-project"
FORBIDDEN_STATE = "/Users/testuser/.local/state/workspace-bridge-node"
FORBIDDEN_WS = "/Volumes/data2/super-secret-workspace"
FORBIDDEN_URL = "https://hooks.example.com/services/T000/B000/XXXX"
FORBIDDEN_EMAIL = "operator@example.com"
FORBIDDEN_PROMPT = "summarize my secret diary about cats"
FORBIDDEN_TOOL = "tool_args exploit payload with rm -rf"
FORBIDDEN_RESULT = "final response with private diary contents"
FORBIDDEN_KEY = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
FORBIDDEN_SSH = "/tmp/ssh-AbCdEf123/agent.1234"


def _all_bundle_text(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        parts = list(archive.namelist())
        blob = " ".join(parts)
        for name in archive.namelist():
            blob += "\n" + archive.read(name).decode("utf-8", "replace")
        # Include manifest separately for filename checks.
        return blob, parts


def test_sanitize_json_keeps_safe_fields_and_drops_hostile_keys():
    line = json.dumps({
        "timestamp": "2026-09-26T00:00:00+00:00",
        "level": "INFO",
        "component": "bridge",
        "event": "bridge_ready",
        "version": "0.1.0",
        "workspace_count": 3,
        "prompt": FORBIDDEN_PROMPT,
        "message": "hello world",
        "result": FORBIDDEN_RESULT,
        "tool_args": {"cmd": "rm -rf /"},
        "projects_root": FORBIDDEN_WS,
        "model": "secret-model",
        "http_body": "sensitive",
        "env": {"HOME": FORBIDDEN_HOME},
        "token": FORBIDDEN_TOKEN,
    })
    cleaned = sanitize_support_line(line)
    assert cleaned is not None
    assert FORBIDDEN_PROMPT not in cleaned
    assert FORBIDDEN_RESULT not in cleaned
    assert FORBIDDEN_WS not in cleaned
    assert FORBIDDEN_TOKEN not in cleaned
    assert "prompt" not in cleaned
    assert "tool_args" not in cleaned
    # Safe fields survive.
    assert "bridge_ready" in cleaned
    assert "0.1.0" in cleaned


def test_sanitize_plain_redacts_paths_urls_secrets_and_bounds():
    line = (
        f"INFO adapter ready token={FORBIDDEN_TOKEN} {FORBIDDEN_BEARER} "
        f"path {FORBIDDEN_HOME}/file.txt url {FORBIDDEN_URL} "
        f"email {FORBIDDEN_EMAIL} ssh {FORBIDDEN_SSH}"
    )
    # This line contains no drop keywords (prompt/tool/result/message etc.),
    # so it is redacted rather than dropped.
    cleaned = sanitize_support_line(line)
    assert cleaned is not None
    assert FORBIDDEN_TOKEN not in cleaned
    assert "abcdefghijklmnopqrstuvwx123456" not in cleaned
    assert FORBIDDEN_HOME not in cleaned
    assert FORBIDDEN_URL not in cleaned
    assert FORBIDDEN_EMAIL not in cleaned
    assert FORBIDDEN_SSH not in cleaned
    assert "hooks.example.com" not in cleaned
    assert len(cleaned) <= 500
    # Long lines are bounded.
    long_line = "INFO operational heartbeat " + "x" * 2000
    bounded = sanitize_support_line(long_line)
    assert bounded is not None and len(bounded) <= 500


def test_sanitize_drops_prompt_tool_result_and_private_keys():
    assert sanitize_support_line(f"prompt: {FORBIDDEN_PROMPT}") is None
    assert sanitize_support_line(f"tool result: {FORBIDDEN_TOOL}") is None
    assert sanitize_support_line(f"response: {FORBIDDEN_RESULT}") is None
    assert sanitize_support_line("message hello world") is None
    assert sanitize_support_line(FORBIDDEN_KEY) is None
    assert sanitize_support_line("line with -----BEGIN PRIVATE KEY----- marker") is None
    assert sanitize_support_line("") is None
    assert sanitize_support_line("   ") is None
    assert sanitize_support_line(None) is None


def test_project_diagnostics_drops_names_endpoints_and_details():
    report = {
        "generated_at": "2026-09-26T00:00:00+00:00",
        "mode": "live",
        "overall": {"status": "pass", "summary": "All good",
                    "counts": {"pass": 2, "warning": 0, "unknown": 0,
                               "action_required": 0, "failed": 0}},
        "checks": [{
            "id": "workspace.root_accessible:ws1",
            "code": "workspace.root_accessible",
            "section": "workspaces",
            "status": "pass",
            "summary": "Workspace root is authorized.",
            "detail": f"raw detail with {FORBIDDEN_WS} and {FORBIDDEN_TOKEN}",
            "remediation": "Check the local configuration.",
            "workspace_id": "ws-0123456789ab",
            "adapter_id": "ad-0123456789ab",
            "runtime_type": "pi",
        }],
        "runnable_routes": [{
            "workspace_id": "ws-0123456789ab",
            "adapter_id": "ad-0123456789ab",
            "node_id": "node-0123456789ab",
            "workspace_name": "My Secret Workspace",
            "adapter_name": "My Adapter",
            "node_name": "My Node",
            "runtime_type": "pi",
            "ready": True,
            "status": "ready",
            "summary": "Ready",
            "blockers": [],
            "is_default": False,
            "security_source": "profile",
            "profile": {"id": "reviewed", "revision": "rev-1"},
            "default_model_selector": "secret-model",
        }],
        "release": {
            "bridge": {"contract": 1, "product": "workspace-bridge",
                       "product_version": "0.1.0", "component": "bridge",
                       "component_version": "0.1.0",
                       "build_id": "sha256:" + "ab" * 32},
            "manager": None,
            "nodes": {},
            "adapters": {},
        },
    }
    projected = project_diagnostics(report)
    blob = json.dumps(projected)
    assert "My Secret Workspace" not in blob
    assert "My Adapter" not in blob
    assert "secret-model" not in blob
    assert FORBIDDEN_WS not in blob
    assert FORBIDDEN_TOKEN not in blob
    assert "detail" not in projected["checks"][0]
    assert "workspace_name" not in blob
    assert projected["checks"][0]["code"] == "workspace.root_accessible"
    assert projected["runnable_routes"][0]["workspace_id"] == "ws-0123456789ab"
    assert "profile" not in projected["runnable_routes"][0]
    assert projected["release"]["bridge"]["product_version"] == "0.1.0"


def test_project_services_are_path_free_and_secret_safe():
    node_raw = {
        "service_manager": "launchd",
        "label": "com.workspace-bridge.node",
        "domain": "gui/501",
        "target": "gui/501/com.workspace-bridge.node",
        "plist_path": FORBIDDEN_STATE + "/x.plist",
        "state_path": FORBIDDEN_STATE,
        "stdout_path": FORBIDDEN_STATE + "/logs/stdout.log",
        "stderr_path": FORBIDDEN_STATE + "/logs/stderr.log",
        "installed": True,
        "plist": "managed",
        "launchd": {"available": True, "loaded": True, "running": True,
                    "pid": 4321, "state": "running"},
        "state": "running",
        "health": {"status": "healthy", "code": "ok", "protocol": 1,
                   "node_version": "0.1.0",
                   "root_status": {"status": "ready", "total": 1,
                                   "available": 1, "unavailable": 0,
                                   "unavailable_labels": []}},
        "listen": {"host": "127.0.0.1", "port": 8770,
                   "endpoint": "http://127.0.0.1:8770"},
    }
    projected = project_node_status(node_raw)
    blob = json.dumps(projected)
    assert FORBIDDEN_STATE not in blob
    assert "plist_path" not in projected
    assert "state_path" not in projected
    assert "stdout_path" not in projected
    assert "domain" not in projected
    assert "target" not in projected
    assert projected["label"] == "com.workspace-bridge.node"
    assert projected["listen"] == {"port": 8770}
    assert projected["health"]["status"] == "healthy"
    adapter_raw = {
        "service_manager": "launchd",
        "label": "com.workspace-bridge.adapter.pi.0123456789ab",
        "domain": "gui/501",
        "target": "gui/501/com.workspace-bridge.adapter.pi.0123456789ab",
        "installed": True,
        "plist": "managed",
        "launchd": {"available": True, "loaded": True, "running": True,
                    "pid": 9999, "state": "running"},
        "state": "running",
        "health": {"status": "healthy", "code": "ok", "protocol": 1,
                   "adapter_version": "0.1.0", "native_version": "0.87.0"},
        "runtime_type": "pi",
        "service_id": "0123456789ab",
        "listen": {"port": 8780, "endpoint": "http://127.0.0.1:8780"},
        "adapter_version": "0.1.0",
        "native_version": "0.87.0",
    }
    projected_a = project_adapter_status(adapter_raw)
    blob_a = json.dumps(projected_a)
    assert FORBIDDEN_STATE not in blob_a
    assert "domain" not in projected_a
    assert "target" not in projected_a
    assert "executable" not in blob_a
    assert projected_a["label"] == "com.workspace-bridge.adapter.pi.0123456789ab"
    assert projected_a["listen"] == {"port": 8780}


def _seeded_log_dir(state: Path):
    log_dir = state / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    # Safe operational JSON line.
    safe = json.dumps({"timestamp": "2026-09-26T00:00:00+00:00",
                       "level": "INFO", "component": "bridge",
                       "event": "bridge_ready", "version": "0.1.0"})
    hostile_json = json.dumps({"event": "bridge_ready",
                               "prompt": FORBIDDEN_PROMPT,
                               "tool_args": FORBIDDEN_TOOL,
                               "result": FORBIDDEN_RESULT,
                               "token": FORBIDDEN_TOKEN})
    plain_secret = f"INFO heartbeat path {FORBIDDEN_WS}/x url {FORBIDDEN_URL}"
    plain_prompt = f"prompt {FORBIDDEN_PROMPT}"
    key_line = FORBIDDEN_KEY.replace("\n", " ")
    content = "\n".join([safe, hostile_json, plain_secret, plain_prompt,
                         key_line, f"email {FORBIDDEN_EMAIL}",
                         FORBIDDEN_BEARER]) + "\n"
    for name in ("stdout.log", "stderr.log"):
        p = log_dir / name
        p.write_text(content)
        os.chmod(p, 0o600)


def test_collect_file_excerpts_sanitizes_and_bounds(tmp_path):
    from workspace_bridge.node_cli import initialize_node
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    state = tmp_path / "node-state"
    initialize_node(state, [str(root)], "127.0.0.1", 8770)
    _seeded_log_dir(state)
    lines, omissions = collect_file_excerpts(state)
    blob = "\n".join(lines)
    for forbidden in (FORBIDDEN_TOKEN, FORBIDDEN_BEARER.split()[-1],
                      FORBIDDEN_WS, FORBIDDEN_URL, FORBIDDEN_EMAIL,
                      FORBIDDEN_PROMPT, FORBIDDEN_TOOL, FORBIDDEN_RESULT,
                      "BEGIN PRIVATE KEY"):
        assert forbidden not in blob, forbidden
    # Safe operational event survives.
    assert any("bridge_ready" in line for line in lines)
    assert len(lines) <= 200


def test_collect_journal_invalid_unit_is_omission_not_fatal():
    lines, omissions = collect_journal_excerpts("not-a-valid-unit!!")
    assert lines == []
    assert omissions and "omitted-invalid-unit" in omissions[0]


def _init_adapter(tmp_path: Path, name: str, runtime: str, port: int) -> Path:
    from workspace_bridge.adapter_service import initialize_adapter
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    exe_name = ("workspace-bridge-pi-adapter" if runtime == "pi"
                else "workspace-bridge-codex-adapter")
    exe = tmp_path / "bin" / exe_name
    exe.parent.mkdir(parents=True, exist_ok=True)
    if not exe.exists():
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o700)
    state = tmp_path / name
    initialize_adapter(state, runtime_type=runtime,
                       projects_root=str(projects), port=port,
                       executable=str(exe))
    return state


def test_bundle_with_seeded_logs_never_leaks(tmp_path, monkeypatch):
    from workspace_bridge.node_cli import initialize_node
    # Force file-log collection regardless of host platform.
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    node_state = tmp_path / "node-state"
    initialize_node(node_state, [str(root)], "127.0.0.1", 8771)
    _seeded_log_dir(node_state)
    adapter_pi = _init_adapter(tmp_path, "adapter-pi", "pi", 18881)
    _seeded_log_dir(adapter_pi)
    adapter_codex = _init_adapter(tmp_path, "adapter-codex", "codex", 18882)
    _seeded_log_dir(adapter_codex)
    out = tmp_path / "out" 
    out.mkdir()
    bundle_path = out / "support.zip"
    result = support_bundle.build_support_bundle(
        output=bundle_path, offline=True, node_state=node_state,
        adapter_states=[adapter_pi, adapter_codex], platform_name="Darwin")
    assert bundle_path.exists()
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
    # Deterministic ordering.
    assert result["entries"] == sorted(result["entries"])
    assert "manifest.json" in result["entries"]
    assert "diagnostics.json" in result["entries"]
    assert "services.json" in result["entries"]
    # Read every entry and assert no forbidden strings anywhere.
    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        blob = " ".join(names)
        for name in names:
            # Fixed generic names only, never user paths.
            assert str(node_state) not in name
            assert str(adapter_pi) not in name
            assert FORBIDDEN_WS not in name
            assert name in {"manifest.json", "diagnostics.json",
                            "services.json", "logs/node.jsonl"} or \
                name.startswith("logs/adapter-")
            data = archive.read(name).decode("utf-8", "replace")
            blob += "\n" + data
        for forbidden in (FORBIDDEN_TOKEN, FORBIDDEN_BEARER.split()[-1],
                          FORBIDDEN_HOME, FORBIDDEN_STATE, FORBIDDEN_WS,
                          FORBIDDEN_URL, "hooks.example.com",
                          FORBIDDEN_EMAIL, FORBIDDEN_PROMPT, FORBIDDEN_TOOL,
                          FORBIDDEN_RESULT, "BEGIN PRIVATE KEY",
                          FORBIDDEN_SSH, "runtime-token", "node-token",
                          ".sqlite3", "config.json"):
            assert forbidden not in blob, f"leaked {forbidden!r} in bundle"
        # Manifest is path-free and has required keys.
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["bundle_format"] == "workspace-bridge-support-bundle"
        assert manifest["platform"] in {"macos", "linux", "unknown"}
        assert manifest["collection_mode"] == "offline"
        mblob = json.dumps(manifest)
        assert str(node_state) not in mblob
        assert FORBIDDEN_WS not in mblob
        # Services are path-free.
        services = json.loads(archive.read("services.json"))
        sblob = json.dumps(services)
        assert str(node_state) not in sblob
        assert FORBIDDEN_WS not in sblob
    # Total logical payload stays bounded (exact <=5 MiB including manifest).
    total = sum(len(zipfile.ZipFile(bundle_path).read(n)) for n in names)
    assert total <= 5 * 1024 * 1024
    # Physical container also stays bounded.
    assert bundle_path.stat().st_size <= 5 * 1024 * 1024


def test_bundle_new_file_only_and_parent_rules(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    first = out_dir / "a.zip"
    first.write_bytes(b"existing")
    with pytest.raises(Exception) as exc:
        support_bundle.build_support_bundle(output=first, offline=True)
    assert "overwrite" in str(exc.value).lower() or getattr(exc.value, "code", "") == "bundle-exists"
    link = out_dir / "link.zip"
    try:
        link.symlink_to(first)
    except OSError:
        pass
    else:
        with pytest.raises(Exception):
            support_bundle.build_support_bundle(output=link, offline=True)
    missing_parent = tmp_path / "nope" / "b.zip"
    with pytest.raises(Exception):
        support_bundle.build_support_bundle(output=missing_parent, offline=True)
    bad_suffix = out_dir / "c.txt"
    with pytest.raises(Exception):
        support_bundle.build_support_bundle(output=bad_suffix, offline=True)


def test_bundle_missing_bridge_and_local_states(tmp_path, monkeypatch):
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    bundle_path = out_dir / "empty.zip"
    missing_node = tmp_path / "missing-node"
    missing_adapter = tmp_path / "missing-adapter"
    result = support_bundle.build_support_bundle(
        output=bundle_path, offline=True, node_state=missing_node,
        adapter_states=[missing_adapter], platform_name="Darwin")
    assert bundle_path.exists()
    with zipfile.ZipFile(bundle_path) as archive:
        assert "manifest.json" in archive.namelist()
        assert "diagnostics.json" in archive.namelist()
        assert "services.json" in archive.namelist()
        diagnostics = json.loads(archive.read("diagnostics.json"))
        assert diagnostics["overall"]["status"] in {"failed", "unknown", "pass",
                                                    "warning", "action_required"}
        # No crash on missing states; omissions are bounded codes.
        manifest = json.loads(archive.read("manifest.json"))
        assert isinstance(manifest["omitted"], list)


def test_bundle_max_adapters_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    states = [_init_adapter(tmp_path, f"adapter-{i:02d}", "pi", 19000 + i)
              for i in range(17)]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    bundle_path = out_dir / "many.zip"
    result = support_bundle.build_support_bundle(
        output=bundle_path, offline=True, node_state=None,
        adapter_states=states, platform_name="Darwin")
    with zipfile.ZipFile(bundle_path) as archive:
        services = json.loads(archive.read("services.json"))
        assert len(services.get("adapters", [])) <= 16
        manifest = json.loads(archive.read("manifest.json"))
        assert any("adapter-limit" in code for code in manifest["omitted"]) or \
            any("adapter-limit" in code for code in services.get("omissions", []))


def test_sanitize_hostile_inside_allowlisted_keys_never_survive():
    allowlisted_keys = ["reason", "source", "version", "session_id",
                        "workspace_id", "instance", "tool_call_id"]
    hostiles = [
        FORBIDDEN_PROMPT,
        FORBIDDEN_RESULT,
        FORBIDDEN_TOOL,
        FORBIDDEN_BEARER,
        FORBIDDEN_BEARER.split()[-1],
        FORBIDDEN_URL,
        FORBIDDEN_HOME,
        FORBIDDEN_STATE,
        FORBIDDEN_WS,
        FORBIDDEN_EMAIL,
        FORBIDDEN_KEY,
        FORBIDDEN_KEY.replace("\n", " "),
        FORBIDDEN_SSH,
        FORBIDDEN_TOKEN,
        "ok\x01bad",
        "caf\u00e9",
        "x" * 200,
        "https://evil.example.com/hook",
        "user@evil.example.com",
        "/etc/passwd",
    ]
    for key in allowlisted_keys:
        for hostile in hostiles:
            line = json.dumps({"event": "bridge_ready", key: hostile})
            cleaned = sanitize_support_line(line)
            blob = cleaned or ""
            assert hostile not in blob, f"leaked {hostile!r} in {key}"
            # No URL/email/path/secret fragments survive either.
            assert "hooks.example.com" not in blob
            assert "evil.example.com" not in blob
            assert "BEGIN PRIVATE KEY" not in blob
            if cleaned is not None:
                # If anything survives, it must be only the safe event.
                parsed = json.loads(cleaned)
                assert set(parsed) <= {"event", "timestamp", "level",
                                        "component"} | {key} or key not in parsed
                if key in parsed:
                    # Surviving values must be strict tokens/IDs, never
                    # containing spaces, slashes, @, colons for URLs, or
                    # control/Unicode.
                    surviving = parsed[key]
                    assert isinstance(surviving, str)
                    assert " " not in surviving
                    assert "/" not in surviving
                    assert "@" not in surviving
                    assert "://" not in surviving
    # Legitimate operational records still work.
    good = json.dumps({"timestamp": "2026-09-26T00:00:00+00:00",
                       "level": "INFO", "component": "bridge",
                       "event": "bridge_ready", "version": "0.1.0",
                       "workspace_count": 3,
                       "workspace_id": "ws-0123456789ab",
                       "session_id": "sess_abc123",
                       "runtime_configured": True})
    kept = sanitize_support_line(good)
    assert kept is not None
    assert "bridge_ready" in kept
    assert "0.1.0" in kept
    assert "ws-0123456789ab" in kept
    # A record with only invalid fields is omitted.
    assert sanitize_support_line(json.dumps({"reason": FORBIDDEN_PROMPT})) is None
    assert sanitize_support_line(json.dumps({"reason": FORBIDDEN_PROMPT,
                                             "source": FORBIDDEN_URL})) is None
    # With a valid event, hostile fields are dropped but the record survives.
    with_event = json.dumps({"event": "bridge_ready",
                             "reason": FORBIDDEN_PROMPT,
                             "source": FORBIDDEN_URL})
    kept_event = sanitize_support_line(with_event)
    assert kept_event is not None
    assert "bridge_ready" in kept_event
    assert FORBIDDEN_PROMPT not in kept_event
    assert FORBIDDEN_URL not in kept_event


def test_bundle_exact_5mib_bound_and_manifest_consistency(tmp_path, monkeypatch):
    from workspace_bridge.node_cli import initialize_node
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    monkeypatch.setattr(support_bundle, "MAX_TOTAL_BYTES", 4096)
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    node_state = tmp_path / "node-state"
    initialize_node(node_state, [str(root)], "127.0.0.1", 8771)
    # Seed large but sanitizable logs to force oversize omission.
    log_dir = node_state / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass
    big_line = json.dumps({"timestamp": "2026-09-26T00:00:00+00:00",
                           "level": "INFO", "component": "bridge",
                           "event": "bridge_ready", "version": "0.1.0"})
    content = "\n".join([big_line] * 500) + "\n"
    for name in ("stdout.log", "stderr.log"):
        p = log_dir / name
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
            except OSError:
                pass
        p.write_text(content)
        os.chmod(p, 0o600)
    out = tmp_path / "out"
    out.mkdir()
    bundle_path = out / "bounded.zip"
    result = support_bundle.build_support_bundle(
        output=bundle_path, offline=True, node_state=node_state,
        adapter_states=[], platform_name="Darwin")
    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        manifest = json.loads(archive.read("manifest.json"))
        logical = sum(len(archive.read(n)) for n in names)
        assert logical <= 4096, logical
        assert bundle_path.stat().st_size <= 4096
        # Manifest included/omitted exactly matches archive for logs.
        archived_logs = {n for n in names if n.startswith("logs/")}
        included_logs = {n for n in manifest["included"] if n.startswith("logs/")}
        assert archived_logs == included_logs
        for name in archived_logs:
            assert name not in " ".join(manifest["omitted"])
        # Removed oversize logs are not listed as included.
        for code in manifest["omitted"]:
            if code.startswith("omitted-oversize-"):
                victim = code[len("omitted-oversize-"):]
                assert victim not in names
                assert victim not in manifest["included"]


def test_bundle_uses_selected_bridge_state(tmp_path, monkeypatch):
    from workspace_bridge.cli import initialize as bridge_init, load_config
    from workspace_bridge.service import Service
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    state_a = tmp_path / "bridge-a"
    bridge_init(state_a, 8765, 8766)
    # Materialize the SQLite DB so live diagnostics succeed (read-only open
    # requires an existing database file).
    cfg_a = load_config(state_a.resolve())
    svc_a = Service(state_a.resolve(), cfg_a, run_coordinator_background=False)
    try:
        pass
    finally:
        try:
            svc_a.close()
        except Exception:
            pass
    missing_b = tmp_path / "bridge-missing"
    out = tmp_path / "out"
    out.mkdir()
    # Valid custom state yields live diagnostics (many checks).
    good_path = out / "good.zip"
    support_bundle.build_support_bundle(output=good_path, offline=True,
                                         node_state=None, adapter_states=[],
                                         platform_name="Darwin",
                                         bridge_state=state_a)
    # Missing custom state yields sanitized initialization diagnostic.
    bad_path = out / "bad.zip"
    support_bundle.build_support_bundle(output=bad_path, offline=True,
                                         node_state=None, adapter_states=[],
                                         platform_name="Darwin",
                                         bridge_state=missing_b)
    with zipfile.ZipFile(good_path) as za, zipfile.ZipFile(bad_path) as zb:
        good_diag = json.loads(za.read("diagnostics.json"))
        bad_diag = json.loads(zb.read("diagnostics.json"))
        assert len(good_diag.get("checks", [])) > 1
        assert len(bad_diag.get("checks", [])) == 1
        assert bad_diag["checks"][0]["code"] == "core.config_state_readable"
        assert good_diag != bad_diag


def test_cli_global_state_selects_bridge_state(tmp_path, monkeypatch):
    from workspace_bridge.cli import initialize as bridge_init
    state_custom = tmp_path / "bridge-custom"
    bridge_init(state_custom, 8765, 8766)
    out = tmp_path / "out"
    out.mkdir()
    out_path = out / "cli.zip"
    captured: dict = {}
    real_build = support_bundle.build_support_bundle

    def _capture(**kwargs):
        captured.update(kwargs)
        return real_build(**kwargs)
    # build_support_bundle is imported inside cli.main, so patch the
    # support_bundle module attribute directly.
    monkeypatch.setattr(support_bundle, "build_support_bundle", _capture)
    from workspace_bridge import cli as cli_mod
    cli_mod.main(["--state", str(state_custom), "support", "bundle",
                  "--output", str(out_path), "--offline"])
    assert out_path.exists()
    # The already-resolved global state must have been passed through.
    assert "bridge_state" in captured
    assert str(captured["bridge_state"]) == str(state_custom.resolve())


def test_output_race_no_clobber_preserves_existing(tmp_path, monkeypatch):
    from workspace_bridge.node_cli import initialize_node
    monkeypatch.setattr(support_bundle, "_platform_category", lambda *a, **k: "macos")
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    node_state = tmp_path / "node-state"
    initialize_node(node_state, [str(root)], "127.0.0.1", 8771)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "race.zip"
    # Deterministic race: create the destination after preflight but before
    # atomic link, then prove the pre-existing bytes remain untouched and
    # the bundle fails bundle-exists without replacement.
    real_link = os.link

    def _racing_link(src, dst, *a, **k):
        Path(dst).write_bytes(b"PRE-EXISTING-DO-NOT-TOUCH")
        return real_link(src, dst, *a, **k)
    monkeypatch.setattr(os, "link", _racing_link)
    with pytest.raises(Exception) as exc:
        support_bundle.build_support_bundle(output=target, offline=True,
                                             node_state=node_state,
                                             adapter_states=[],
                                             platform_name="Darwin")
    assert getattr(exc.value, "code", "") == "bundle-exists"
    assert target.read_bytes() == b"PRE-EXISTING-DO-NOT-TOUCH"
    # No temp leftovers in the parent.
    leftovers = [p for p in out.iterdir() if p.name.startswith(".support-bundle-")]
    assert leftovers == []
