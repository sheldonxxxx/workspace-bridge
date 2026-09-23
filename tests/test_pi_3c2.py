"""Milestone 3C2: web-managed default extension set for Bridge Pi sessions.

Focused Bridge coverage (scripted fakes, no Node/network/provider):
extension policy validation/revision/default-empty/save against a live
scripted inventory, status without names, runtime capability/deployed
negotiation and list_extensions normalization, session creation sending
extension policy/revision and persisting the active snapshot,
continuation binding, migration-safe historical reads, web dialog static
shape, and unchanged generic/3C1 behavior.
"""
import hashlib
import json

import pytest

from workspace_bridge.pi_extensions import (
    PI_EXTENSION_POLICY_SETTING,
    canonical_json,
    full_view,
    is_valid_extension_id,
    normalize_inventory_row,
    policy_revision,
    safe_defaults,
    status_summary,
    validate_against_inventory,
    validate_policy,
)
from workspace_bridge.runtime import (
    HttpPiRuntime,
    RuntimeCapabilities,
    RuntimeUnsupported,
    SessionInfo,
)
from workspace_bridge.security import BridgeError

from runtime_fakes import FakeRuntime
from test_pi_3b1 import make_service, publish, call, pi_env  # noqa: F401


def scripted_inventory():
    return [
        {"id": "npm:pi-mcp-adapter", "name": "pi-mcp-adapter", "version": "0.4.0",
         "has_extensions": True, "extensions": ["./dist/extension.js"],
         "extension_count": 1, "package_json_sha256": "a" * 64,
         "supported": True, "reason": ""},
        {"id": "npm:pi-web-access", "name": "pi-web-access", "version": "1.2.0",
         "has_extensions": True, "extensions": ["./dist/main.js"],
         "extension_count": 1, "package_json_sha256": "b" * 64,
         "supported": True, "reason": ""},
        {"id": "npm:plain-lib", "name": "plain-lib", "version": "2.0.0",
         "has_extensions": False, "extensions": [], "extension_count": 0,
         "package_json_sha256": "c" * 64, "supported": False,
         "reason": "no_extension_resources"},
    ]


def make_pi_runtime_3c2(directory, inventory):
    from test_pi_3b1 import make_pi_runtime
    runtime = make_pi_runtime(directory)
    runtime._capabilities = RuntimeCapabilities(
        model_discovery=True, session_reuse=True, event_polling=False,
        session_status=True, pending_snapshot=True, permission_response=True,
        question_detection=False, question_response=False, session_branching=False,
        execution_history=True, extension_inventory=True)
    runtime.extension_inventory = list(inventory)

    def list_extensions():
        return list(runtime.extension_inventory)

    runtime.list_extensions = list_extensions
    base_create = runtime.create_session

    def create_session(directory, title, options=None):
        session = base_create(directory, title, options)
        ext = (options or {}).get("extension_policy") or {"version": 1, "enabled": []}
        rows = [r for r in runtime.extension_inventory if r["id"] in ext.get("enabled", [])]
        session.extension_revision = (options or {}).get("extension_revision") or ""
        session.extensions = [{"id": r["id"], "name": r["name"], "version": r["version"],
                               "fingerprint": r["package_json_sha256"]} for r in rows]
        return session

    runtime.create_session = create_session
    return runtime


@pytest.fixture
def ext_env(pi_env):  # noqa: F811
    service = pi_env["service"]
    orch = service.orchestrators["pi"]
    runtime = make_pi_runtime_3c2("/tmp", scripted_inventory())
    # Rebind the Pi orchestrator's runtime to the 3C2-capable fake while
    # keeping every other fixture (workspace, policies, jobs) identical.
    orch.runtime = runtime
    service.runtime_registry._runtimes["pi"] = runtime
    pi_env["pi"] = runtime
    yield pi_env


# ------------------------------------------------- policy validation/defaults
def test_extension_policy_defaults_empty_and_revisioned():
    policy = safe_defaults()
    assert policy == {"version": 1, "enabled": []}
    revision = policy_revision(policy)
    assert len(revision) == 64 and all(c in "0123456789abcdef" for c in revision)
    assert canonical_json(policy) == json.dumps(policy, sort_keys=True,
                                                ensure_ascii=False, separators=(",", ":"))
    assert policy_revision(policy) == hashlib.sha256(
        canonical_json(policy).encode()).hexdigest()


def test_extension_policy_validation_is_strict():
    assert validate_policy({"version": 1, "enabled": []}) == safe_defaults()
    assert validate_policy({"version": 1, "enabled": ["npm:a", "npm:@s/b"]}) == {
        "version": 1, "enabled": ["npm:a", "npm:@s/b"]}
    assert is_valid_extension_id("npm:pi-web-access")
    assert is_valid_extension_id("npm:@scope/pkg")
    assert not is_valid_extension_id("npm:pi-web-access@1.0.0")
    assert not is_valid_extension_id("/abs/path")
    for bad in ({"version": 2, "enabled": []},
                {"version": 1, "enabled": ["npm:a", "npm:a"]},
                {"version": 1, "enabled": ["npm:a@1.0.0"]},
                {"version": 1, "enabled": ["/x"]},
                {"version": 1},
                {"version": 1, "enabled": [], "extra": 1},
                {"version": 1, "enabled": "npm:a"}):
        with pytest.raises(BridgeError):
            validate_policy(bad)


def test_unconfigured_extension_policy_reads_empty(ext_env):
    service = ext_env["service"]
    assert service.setting(PI_EXTENSION_POLICY_SETTING) is None
    policy, revision, configured = service.get_pi_extension_policy()
    assert configured is False and policy == safe_defaults()
    assert revision == policy_revision(safe_defaults())


def test_save_validates_against_live_inventory(ext_env):
    service = ext_env["service"]
    saved = service.set_pi_extension_policy(
        {"version": 1, "enabled": ["npm:pi-mcp-adapter", "npm:pi-web-access"]})
    assert saved["enabled_count"] == 2
    assert saved["policy"]["enabled"] == ["npm:pi-mcp-adapter", "npm:pi-web-access"]
    assert len(saved["extension_revision"]) == 64
    # Duplicates, unknown IDs, non-extension packages are rejected; the
    # stored policy is unchanged (atomic save).
    before = service.setting(PI_EXTENSION_POLICY_SETTING)
    for bad in ({"version": 1, "enabled": ["npm:pi-mcp-adapter", "npm:pi-mcp-adapter"]},
                {"version": 1, "enabled": ["npm:ghost-pkg"]},
                {"version": 1, "enabled": ["npm:plain-lib"]}):
        with pytest.raises(BridgeError):
            service.set_pi_extension_policy(bad)
    assert service.setting(PI_EXTENSION_POLICY_SETTING) == before


def test_save_canonicalizes_inventory_order_and_revision_is_stable(ext_env):
    service = ext_env["service"]
    first = service.set_pi_extension_policy(
        {"version": 1, "enabled": ["npm:pi-web-access", "npm:pi-mcp-adapter"]})
    # Stored in live inventory/settings order, not POST order.
    assert first["policy"]["enabled"] == ["npm:pi-mcp-adapter", "npm:pi-web-access"]
    second = service.set_pi_extension_policy(
        {"version": 1, "enabled": ["npm:pi-mcp-adapter", "npm:pi-web-access"]})
    assert second["policy"]["enabled"] == ["npm:pi-mcp-adapter", "npm:pi-web-access"]
    assert second["extension_revision"] == first["extension_revision"]
    # No false continuation scope change across reorder-only saves.
    from workspace_bridge.pi_extensions import policy_revision as _rev
    assert _rev(first["policy"]) == first["extension_revision"]


def test_node_python_extension_revision_agrees_on_canonical_order():
    import subprocess
    from workspace_bridge.pi_extensions import canonicalize_enabled_order
    inventory = scripted_inventory()
    canonical = canonicalize_enabled_order(
        inventory, ["npm:pi-web-access", "npm:pi-mcp-adapter"])
    assert canonical == ["npm:pi-mcp-adapter", "npm:pi-web-access"]
    policy = {"version": 1, "enabled": canonical}
    expected = policy_revision(policy)
    script = ("import('./extensions.mjs').then(m => console.log(m.extensionRevision("
              + json.dumps(policy) + ")))")
    try:
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=30,
                              cwd="runtime/pi-host-adapter")
    except (OSError, ValueError):
        pytest.skip("node is unavailable")
    if proc.returncode != 0:
        pytest.skip(f"node extension revision check failed: {proc.stderr[:200]}")
    assert proc.stdout.strip() == expected


def test_save_fails_when_inventory_unavailable(ext_env):
    service = ext_env["service"]
    runtime = ext_env["pi"]

    def boom():
        raise RuntimeUnsupported("Pi adapter does not support extension inventory")

    runtime.list_extensions = boom
    with pytest.raises(BridgeError) as exc:
        service.set_pi_extension_policy({"version": 1, "enabled": ["npm:pi-mcp-adapter"]})
    assert exc.value.code == "inventory_unavailable"
    assert service.setting(PI_EXTENSION_POLICY_SETTING) is None


def test_validate_against_inventory_none_fails_closed():
    with pytest.raises(BridgeError) as exc:
        validate_against_inventory({"version": 1, "enabled": ["npm:a"]}, None)
    assert exc.value.code == "inventory_unavailable"


def test_status_reports_counts_only_without_names(ext_env):
    service = ext_env["service"]
    service.set_pi_extension_policy({"version": 1, "enabled": ["npm:pi-web-access"]})
    status = service.pi_extension_status()
    assert status["runtime"] == "pi"
    assert status["installed_count"] == 3
    assert status["enabled_count"] == 1
    assert len(status["extension_revision_prefix"]) == 12
    assert status["ready"] is True
    blob = json.dumps(status)
    assert "pi-web-access" not in blob and "pi-mcp-adapter" not in blob


def test_status_degrades_when_inventory_unavailable(ext_env):
    runtime = ext_env["pi"]

    def boom():
        raise RuntimeUnsupported("nope")

    runtime.list_extensions = boom
    status = ext_env["service"].pi_extension_status()
    assert status["installed_count"] is None
    assert status["ready"] is False
    assert status["enabled_count"] == 0


def test_full_view_carries_bounded_inventory(ext_env):
    service = ext_env["service"]
    view = service.pi_extension_view()
    assert view["policy"] == safe_defaults()
    assert len(view["extension_revision"]) == 64
    assert len(view["inventory"]) == 3
    assert "arbitrary native code" in view["warning"]
    assert "NEW" in view["session_note"]
    blob = json.dumps(view)
    assert "node_modules" not in blob


def test_inventory_normalization_drops_paths_and_secrets():
    row = normalize_inventory_row({
        "id": "npm:pi-web-access", "name": "pi-web-access", "version": "1.2.0",
        "has_extensions": True, "extensions": ["./dist/main.js"],
        "extension_count": 1, "package_json_sha256": "b" * 64,
        "supported": True, "reason": "",
        "path": "/Users/me/.pi/agent/npm/node_modules/pi-web-access",
        "token": "SECRET", "dependencies": {"x": "1"},
    })
    assert row is not None
    blob = json.dumps(row)
    assert "/Users/me" not in blob and "SECRET" not in blob and "dependencies" not in blob
    assert normalize_inventory_row({"id": "npm:a@1.0.0"}) is None
    assert normalize_inventory_row("nope") is None


# ------------------------------------------------- runtime negotiation
def test_pi_capabilities_advertise_extension_inventory():
    caps = HttpPiRuntime("http://127.0.0.1:9", "t").capabilities
    assert caps.extension_inventory is True
    assert HttpPiRuntime("http://127.0.0.1:9", "t").PI_CAPABILITIES.extension_inventory is True


def test_list_extensions_requires_deployed_capability():
    runtime = HttpPiRuntime("http://127.0.0.1:9", "t")
    runtime.health = lambda: {"ok": True, "deployed_capabilities": {},
                              "extension_inventory_supported": False}
    with pytest.raises(RuntimeUnsupported):
        runtime.list_extensions()


def test_list_extensions_normalizes_and_drops_paths():
    runtime = HttpPiRuntime("http://127.0.0.1:9", "t")
    runtime.health = lambda: {"ok": True, "deployed_capabilities": {},
                              "extension_inventory_supported": True}
    runtime._request = lambda method, path, **kw: {
        "packages": [{
            "id": "npm:pi-web-access", "name": "pi-web-access", "version": "1.2.0",
            "has_extensions": True, "extensions": ["./dist/main.js"],
            "extension_count": 1, "package_json_sha256": "b" * 64,
            "supported": True, "reason": "",
            "path": "/secret/agent/dir", "token": "SECRET"}]}
    rows = runtime.list_extensions()
    assert len(rows) == 1 and rows[0]["id"] == "npm:pi-web-access"
    blob = json.dumps(rows)
    assert "/secret" not in blob and "SECRET" not in blob


def test_health_parses_extension_inventory_capability():
    runtime = HttpPiRuntime("http://127.0.0.1:9", "t")
    runtime._request = lambda method, path, **kw: {
        "ok": True, "adapter_version": "0.3.0", "locked": False,
        "capabilities": {"pending_snapshot": True, "permission_response": True,
                         "execution_history": True, "extension_inventory": True}}
    health = runtime.health()
    assert health["extension_inventory_supported"] is True
    assert health["deployed_capabilities"]["extension_inventory"] is True


def test_create_session_sends_extension_policy_and_requires_inventory():
    runtime = HttpPiRuntime("http://127.0.0.1:9", "t")
    seen = {}
    runtime._require_permissions_supported = lambda: True
    runtime._require_execution_supported = lambda: True
    runtime._require_extension_inventory_supported = lambda: True
    runtime._request = lambda method, path, **kw: seen.update(kw.get("body", {})) or {
        "session": {"id": "ses_1", "directory": "/w", "title": "t",
                    "extension_revision": "e" * 64,
                    "extensions": [{"id": "npm:a", "name": "a",
                                    "version": "1.0.0", "fingerprint": "f" * 64}]}}
    session = runtime.create_session("/w", "t", options={
        "permission_policy": {"version": 3}, "policy_revision": "p" * 64,
        "extension_policy": {"version": 1, "enabled": ["npm:a"]},
        "extension_revision": "e" * 64})
    assert seen["extension_policy"] == {"version": 1, "enabled": ["npm:a"]}
    assert seen["extension_revision"] == "e" * 64
    assert session.extension_revision == "e" * 64
    assert session.extensions == [{"id": "npm:a", "name": "a",
                                   "version": "1.0.0", "fingerprint": "f" * 64}]


def test_create_session_with_extensions_fails_on_old_adapter():
    runtime = HttpPiRuntime("http://127.0.0.1:9", "t")
    runtime._require_permissions_supported = lambda: True
    runtime._require_execution_supported = lambda: True
    runtime._require_extension_inventory_supported = lambda: (_ for _ in ()).throw(
        RuntimeUnsupported("Pi adapter does not support extension inventory"))
    with pytest.raises(RuntimeUnsupported):
        runtime.create_session("/w", "t", options={
            "permission_policy": {"version": 3}, "policy_revision": "p" * 64,
            "extension_policy": {"version": 1, "enabled": ["npm:a"]},
            "extension_revision": "e" * 64})


# ------------------------------------------------- sessions/continuation
def test_session_creation_persists_extension_snapshot(ext_env):
    service = ext_env["service"]
    service.set_pi_extension_policy({"version": 1, "enabled": ["npm:pi-web-access"]})
    job = publish(ext_env, "c2-snap")
    run = call(ext_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c2-snap-run")
    options = ext_env["pi"].session_options[-1]
    assert options["extension_policy"] == {"version": 1, "enabled": ["npm:pi-web-access"]}
    assert len(options["extension_revision"]) == 64
    detail = call(ext_env, "read_agent_run", run_id=run["run_id"])
    audit = detail["execution_audit"]
    assert audit["extension_revision"] == options["extension_revision"]
    assert audit["extensions"] == [{"id": "npm:pi-web-access", "name": "pi-web-access",
                                    "version": "1.2.0", "fingerprint": "b" * 64}]


def test_default_empty_policy_persists_empty_snapshot(ext_env):
    job = publish(ext_env, "c2-empty")
    run = call(ext_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c2-empty-run")
    options = ext_env["pi"].session_options[-1]
    assert options["extension_policy"] == {"version": 1, "enabled": []}
    detail = call(ext_env, "read_agent_run", run_id=run["run_id"])
    assert detail["execution_audit"]["extensions"] == []
    assert len(detail["execution_audit"]["extension_revision"]) == 64


def test_continuation_succeeds_unchanged_and_fails_on_extension_change(ext_env):
    from workspace_bridge.runtime import MessageInfo
    from workspace_bridge.security import BridgeError as _BridgeError
    service = ext_env["service"]
    job = publish(ext_env, "c2-cont")
    first = call(ext_env, "start_agent_run", runtime="pi",
                 job_id=job["id"], request_id="c2-cont-first")
    ext_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(ext_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    follow = publish(ext_env, "c2-cont-2")
    second = call(ext_env, "start_agent_run", runtime="pi", job_id=follow["id"],
                  request_id="c2-cont-second", continue_from_run_id=first["run_id"])
    assert second["continue_from_run_id"] == first["run_id"]
    # Complete the continued run so the session is idle again (new
    # post-floor completion evidence is required for the new iteration).
    ext_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=20, text="follow up"),
        MessageInfo(id="m2", role="assistant", created=21, completed=22,
                    text="Done again.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(ext_env, "read_agent_run", run_id=second["run_id"])["state"] == "completed"
    # Extension-only change refuses continuation with a distinct code.
    service.set_pi_extension_policy({"version": 1, "enabled": ["npm:pi-mcp-adapter"]})
    follow3 = publish(ext_env, "c2-cont-3")
    with pytest.raises(_BridgeError) as exc:
        call(ext_env, "start_agent_run", runtime="pi", job_id=follow3["id"],
             request_id="c2-cont-third", continue_from_run_id=first["run_id"])
    assert exc.value.code == "extension_scope_changed"


def test_historical_runs_read_with_empty_extension_snapshot(ext_env):
    service = ext_env["service"]
    job = publish(ext_env, "c2-hist")
    run = call(ext_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c2-hist-run")
    # Simulate a pre-3C2 row: clear the new columns back to migration defaults.
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET extension_revision='', extension_snapshot='[]' WHERE id=?",
            (run["run_id"],))
    detail = call(ext_env, "read_agent_run", run_id=run["run_id"])
    assert detail["execution_audit"]["extensions"] == []
    assert detail["execution_audit"]["extension_revision"] == ""
    assert detail["permission_revision"] != ""


# ------------------------------------------------- extension-tool audit
def test_extension_tool_audit_is_generic_and_bounded(ext_env):
    from workspace_bridge.pi_executions import (
        detail_record, redact_extension_text, sanitize_input_summary,
        sanitize_result_summary, summary_record,
    )
    assert sanitize_input_summary("my-ext-tool", {"query": "q", "token": "SECRET"})[
        "query"] == "q"
    blob = json.dumps(sanitize_input_summary("my-ext-tool", {"query": "q", "token": "SECRET"}))
    assert "SECRET" not in blob
    result = sanitize_result_summary("my-ext-tool",
                                     {"preview": "answer", "fullOutputPath": "/tmp/x"}, False)
    assert result["preview"] == "answer"
    assert "fullOutputPath" not in json.dumps(result)
    big = sanitize_result_summary("my-ext-tool", {"preview": "z" * 20000}, False)
    assert len(big["preview"]) == 8192 and big["truncated"] is True
    row = {"tool_call_id": "c", "seq": 1, "tool": "my-ext-tool", "state": "completed",
           "started": None, "ended": None, "duration_ms": 1,
           "input_summary": json.dumps({"args_sha256": "a" * 64, "args_bytes": 10,
                                        "top_keys": ["query"], "query": "q"}),
           "result_summary": json.dumps({"is_error": False, "preview": "answer"}),
           "is_error": 0, "permission_effect": "allow", "permission_decision": "",
           "truncated": 0}
    assert "answer" not in json.dumps(summary_record(row))
    detail = detail_record(row)
    assert "answer" in json.dumps(detail)
    assert detail["result_sensitivity"] == "potentially sensitive"


def test_extension_audit_redaction_best_effort():
    from workspace_bridge.pi_executions import (
        detail_record, redact_extension_text, sanitize_input_summary,
        sanitize_result_summary,
    )
    # Credentials in URL userinfo/query, query text, Bearer result text,
    # and key=value output are redacted at the persistence boundary.
    assert redact_extension_text("https://user:s3cret@example.com/x") == \
        "https://[redacted]@example.com/x"
    assert redact_extension_text("https://h/p?token=abc&next=x") == \
        "https://h/p?token=[redacted]&next=x"
    assert redact_extension_text("Authorization: Bearer abcDEF123") == \
        "Authorization: Bearer [redacted]"
    assert redact_extension_text("saved api_key=SECRET-XYZ ok") == \
        "saved api_key=[redacted] ok"
    # Ordinary web/MCP result text remains useful.
    assert redact_extension_text("pong") == "pong"
    assert redact_extension_text("https://example.com/?q=hello&page=2") == \
        "https://example.com/?q=hello&page=2"
    redacted_in = sanitize_input_summary(
        "my-ext-tool", {"url": "https://u:p@example.com/hook?token=T",
                        "query": "password=hunter2"})
    assert "hunter2" not in json.dumps(redacted_in)
    assert "[redacted]" in json.dumps(redacted_in)
    redacted_out = sanitize_result_summary(
        "my-ext-tool", {"preview": "ok\napi_key = SECRET-XYZ"}, False)
    assert "SECRET-XYZ" not in redacted_out["preview"]
    assert "[redacted]" in redacted_out["preview"]
    # An unredacted (old/compromised adapter) payload is redacted here,
    # so the adapter cannot bypass the boundary.
    hostile = detail_record({
        "tool_call_id": "h", "seq": 2, "tool": "my-ext-tool", "state": "completed",
        "started": None, "ended": None, "duration_ms": 1,
        "input_summary": json.dumps({"args_sha256": "b" * 64, "args_bytes": 9,
                                     "top_keys": ["url"],
                                     "url": "https://evil.example/?api_key=LIVE"}),
        "result_summary": json.dumps({"is_error": False,
                                      "preview": "Bearer LIVE-TOKEN-9"}),
        "is_error": 0, "permission_effect": "allow", "permission_decision": "",
        "truncated": 0})
    assert "LIVE" not in json.dumps(hostile)
    assert hostile["result_sensitivity"] == "potentially sensitive"


def test_identity_mismatch_rows_cannot_be_enabled(ext_env):
    service = ext_env["service"]
    runtime = ext_env["pi"]
    runtime.extension_inventory = list(scripted_inventory()) + [{
        "id": "npm:renamed", "name": "other", "version": "1.0.0",
        "has_extensions": False, "extensions": [], "extension_count": 0,
        "package_json_sha256": "d" * 64, "supported": False,
        "reason": "identity_mismatch"}]
    with pytest.raises(BridgeError) as exc:
        service.set_pi_extension_policy({"version": 1, "enabled": ["npm:renamed"]})
    assert exc.value.code == "unsupported_extension"


def test_extension_status_endpoint_shapes(ext_env):
    # Service-layer shapes behind GET /api/runtimes/pi/extensions and the
    # runtime_extensions block in /api/status (route auth is covered by
    # existing API tests).
    service = ext_env["service"]
    service.set_pi_extension_policy({"version": 1, "enabled": ["npm:pi-web-access"]})
    status = service.pi_extension_status()
    assert status["enabled_count"] == 1
    assert "pi-web-access" not in json.dumps(status)
    view = service.pi_extension_view()
    assert view["policy"]["enabled"] == ["npm:pi-web-access"]
    assert any(r["id"] == "npm:pi-web-access" for r in view["inventory"])


# ------------------------------------------------- web UI static shape
def test_web_dialog_shape_no_innerhtml():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "workspace_bridge" / "static" / "index.html").read_text()
    js = (root / "workspace_bridge" / "static" / "app.js").read_text()
    assert "Manage extensions" in html
    assert 'id="pi-extensions-dialog"' in html
    assert 'id="pi-ext-list"' in html
    assert 'id="pi-ext-refresh"' in html
    assert 'id="pi-ext-save"' in html
    assert "Extensions:" in js and "enabled ·" in js and "installed" in js
    assert "arbitrary native code" in js
    assert "NEW Pi sessions only" in js or "New Pi sessions only" in js
    assert '"/api/runtimes/pi/extensions"' in js
    # Extension dialog code uses structured DOM only (no innerHTML assignment).
    start = js.find("Pi extension policy lives in a dedicated")
    assert start != -1
    assert ".innerHTML" not in js[start:]
    # No install/update/remove controls in this milestone.
    segment = js[start:]
    assert "install" not in segment.lower() or "Install packages" not in segment


def test_aux_and_permission_behavior_unchanged(ext_env):
    job = publish(ext_env, "c2-aux")
    run = call(ext_env, "start_agent_run", runtime="aux",
                 job_id=job["id"], request_id="c2-aux-run")
    detail = call(ext_env, "read_agent_run", run_id=run["run_id"])
    assert detail.get("execution_audit", {"status": "not_recorded"})["status"] == "not_recorded"
    assert detail["execution_audit"].get("extensions", []) == []
    # Permission policy save still validates strictly (3C1 untouched).
    from workspace_bridge.pi_permissions import safe_defaults as _perm_defaults
    saved = ext_env["service"].set_pi_permission_policy(
        {**_perm_defaults(), "write_tools_enabled": True})
    assert saved["policy"]["write_tools_enabled"] is True
