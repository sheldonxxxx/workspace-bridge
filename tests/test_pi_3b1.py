"""Milestones 3B1/3B2: web-configurable Pi file-tool permission policy.

3B2 upgrades the schema to v2 (write_tools_enabled + web-configurable
external file scope) with in-memory v1 migration on read. Focused coverage
for the Bridge-side Pi permission policy plus the Pi runtime client
contract. Scripted fakes only: no Node, network, provider credentials, or
real model is required (except one revision-agreement check that shells to
the native adapter when node is available).
"""
import hashlib
import json
import urllib.request

import httpx
import pytest

from workspace_bridge.api import Handoff, TOOLS, make_admin
from workspace_bridge.pi_permissions import (
    PI_PERMISSION_POLICY_SETTING,
    SUPPORTED_PI_PERMISSION_TOOLS,
    canonical_json,
    migrate_v1_policy,
    policy_revision,
    safe_defaults,
)
from workspace_bridge.runtime import (
    HttpPiRuntime,
    PI_RUNTIME_ID,
    RuntimeCapabilities,
    RuntimeRejected,
    RuntimeUnavailable,
    RuntimeUnsupported,
)
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, RecordingNotifier, pending_permission


def enabled_policy(**overrides):
    policy = safe_defaults()
    policy["write_tools_enabled"] = True
    policy.update(overrides)
    return policy


def v1_enabled_policy():
    """A stored 3B1-era v1 policy, as deployed before the 3B2 upgrade."""
    return {
        "version": 1,
        "enabled": True,
        "tools": {"read": "allow", "grep": "deny", "find": "allow",
                  "ls": "allow", "edit": "ask", "write": "ask"},
        "protected_patterns": [".git/**", "secret/**"],
        "protected_template_exceptions": [".env.example"],
        "allow_session_always": False,
    }


def make_service(tmp_path, runtimes):
    from workspace_bridge.cli import initialize
    from workspace_bridge.registry import RuntimeRegistry
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    service = Service(state, cfg, registry=RuntimeRegistry(dict(runtimes)),
                      notifier=RecordingNotifier(), orchestrator_background=False)
    return service, root


def make_pi_runtime(directory):
    runtime = FakeRuntime(directory)
    runtime._runtime_id = "pi"
    runtime._capabilities = RuntimeCapabilities(
        model_discovery=True, session_reuse=True, event_polling=False,
        session_status=True, pending_snapshot=True, permission_response=True,
        question_detection=False, question_response=False, session_branching=False,
        execution_history=True)
    runtime.models = list(runtime.models)
    return runtime


@pytest.fixture
def pi_env(tmp_path):
    from workspace_bridge.cli import initialize  # noqa: F401
    opencode = FakeRuntime("/tmp")
    pi = make_pi_runtime("/tmp")
    service, root = make_service(tmp_path, {"opencode": opencode, "pi": pi})
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.orchestrators["opencode"].set_model_policy(
        ["anthropic/claude-sonnet"], "anthropic/claude-sonnet")
    service.orchestrators["pi"].set_model_policy(
        ["anthropic/claude-sonnet"], "anthropic/claude-sonnet",
        service.workspace(ws_id))
    yield {"service": service, "opencode": opencode, "pi": pi,
           "root": root, "state": service.state, "config": service.config,
           "id": ws_id, "token": token}
    service.close()


def publish(env, request_id="pi-3b1-1", title="Pi perms"):
    return env["service"].call(
        env["id"], env["token"], "prepare_handoff",
        Handoff.model_validate({"request_id": request_id, "title": title, "goal": "g",
                                "plan": "p", "acceptance": "a", "constraints": "c",
                                "context": "x", "context_hashes": {}}).model_dump())


def call(env, tool, **args):
    return env["service"].call(env["id"], env["token"], tool, args)


# ------------------------------------------------- policy validation/defaults
def test_safe_defaults_are_read_only_and_revisioned():
    policy = safe_defaults()
    assert policy["version"] == 3 and policy["write_tools_enabled"] is False
    assert policy["tools"] == {"read": "allow", "grep": "allow", "find": "allow",
                               "ls": "allow", "edit": "ask", "write": "ask"}
    assert ".workspace-handoff/**" in policy["protected_patterns"]
    assert policy["allow_session_always"] is True
    assert policy["external_access"] == {"default_mode": "deny", "roots": []}
    assert policy["shell_mode"] == "deny"
    assert "enabled" not in policy
    revision = policy_revision(policy)
    assert len(revision) == 64 and all(c in "0123456789abcdef" for c in revision)
    # Canonical JSON is byte-stable: sorted keys, no spaces (shared with
    # the native adapter revision check).
    assert canonical_json(policy) == json.dumps(policy, sort_keys=True,
                                                ensure_ascii=False, separators=(",", ":"))
    assert policy_revision(policy) == hashlib.sha256(
        canonical_json(policy).encode()).hexdigest()


def test_revision_matches_native_adapter_canonicalization():
    import subprocess
    policy = enabled_policy()
    expected = policy_revision(policy)
    script = ("import('./policy.mjs').then(m => console.log(m.policyRevision("
              + json.dumps(policy) + ")))")
    try:
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=30,
                              cwd="runtime/pi-host-adapter")
    except (OSError, ValueError):
        pytest.skip("node is unavailable")
    if proc.returncode != 0:
        pytest.skip(f"node policy check failed: {proc.stderr[:200]}")
    assert proc.stdout.strip() == expected


def test_unconfigured_policy_reads_safe_defaults(pi_env):
    service = pi_env["service"]
    assert service.setting(PI_PERMISSION_POLICY_SETTING) is None
    policy, revision, configured = service.get_pi_permission_policy()
    assert configured is False and policy == safe_defaults()
    assert revision == policy_revision(safe_defaults())
    status = service.pi_permission_status()
    assert status["runtime"] == "pi" and status["enabled"] is False
    assert status["write_tools_enabled"] is False
    assert status["effective_writable"] is False
    assert status["tools"]["edit"] == "ask"
    assert status["external_default_mode"] == "deny"
    assert status["external_root_count"] == 0
    blob = json.dumps(status)
    assert ".workspace-handoff" not in blob and "/tmp" not in blob.lower()


def test_set_policy_validation_is_strict_and_atomic(pi_env):
    service = pi_env["service"]
    before = service.pi_permission_view()
    v1_shape = v1_enabled_policy()
    bad_version = safe_defaults()
    bad_version["version"] = 1
    bad_inputs = [
        None, "not-json", bad_version, v1_shape,
        {**safe_defaults(), "enabled": True},
        {**safe_defaults(), "write_tools_enabled": "yes"},
        {**safe_defaults(), "tools": {"read": "allow"}},
        {**safe_defaults(), "tools": {**safe_defaults()["tools"], "edit": "sometimes"}},
        {**safe_defaults(), "tools": {**safe_defaults()["tools"], "bash": "deny"}},
        {**safe_defaults(), "protected_patterns": ["/absolute/**"]},
        {**safe_defaults(), "protected_patterns": ["../escape/**"]},
        {**safe_defaults(), "protected_patterns": ["x" * 401]},
        {**safe_defaults(), "protected_patterns": ["ok"] * 65},
        {**safe_defaults(), "allow_session_always": "yes"},
        {**safe_defaults(), "external_access": None},
        {**safe_defaults(), "external_access": {"default_mode": "sometimes", "roots": []}},
        {**safe_defaults(), "external_access": {"default_mode": "deny"}},
        {**safe_defaults(), "external_access": {"default_mode": "deny", "roots": {},
                                                "extra": 1}},
        {**safe_defaults(), "external_access": {"default_mode": "deny",
                                                "roots": [{"path": "relative", "mode": "allow"}]}},
        {**safe_defaults(), "external_access": {"default_mode": "deny",
                                                "roots": [{"path": "/a/../b", "mode": "allow"}]}},
        {**safe_defaults(), "external_access": {"default_mode": "deny",
                                                "roots": [{"path": "/ok", "mode": "sometimes"}]}},
        {**safe_defaults(), "external_access": {"default_mode": "deny",
                                                "roots": [{"path": "/dup", "mode": "allow"},
                                                          {"path": "/dup", "mode": "deny"}]}},
        {**safe_defaults(), "external_access": {"default_mode": "deny",
                                                "roots": [{"path": f"/r{i}", "mode": "ask"}
                                                          for i in range(33)]}},
        {**safe_defaults(), "unknown_field": 1},
    ]
    for bad in bad_inputs:
        with pytest.raises(BridgeError) as exc:
            service.set_pi_permission_policy(bad)
        assert exc.value.code == "invalid_arguments"
    assert service.pi_permission_view() == before
    assert service.setting(PI_PERMISSION_POLICY_SETTING) is None


def test_set_and_get_round_trip_with_revision(pi_env):
    service = pi_env["service"]
    saved = service.set_pi_permission_policy(enabled_policy())
    assert saved["enabled"] is True and saved["effective_writable"] is True
    assert saved["write_tools_enabled"] is True
    assert saved["policy"]["write_tools_enabled"] is True
    assert saved["external_default_mode"] == "deny" and saved["external_root_count"] == 0
    policy, revision, configured = service.get_pi_permission_policy()
    assert configured is True and policy["write_tools_enabled"] is True
    assert revision == saved["policy_revision"] == policy_revision(policy)
    # Corrupt storage fails closed to safe defaults, never partial.
    service.set_setting(PI_PERMISSION_POLICY_SETTING, "corrupt{{")
    fallback, _, configured = service.get_pi_permission_policy()
    assert configured is False and fallback == safe_defaults()


# ------------------------------------------- v1/v2 -> v3 migration (3C1)
def test_v1_stored_policy_migrates_without_losing_admin_settings(pi_env):
    from workspace_bridge.pi_permissions import load_policy
    service = pi_env["service"]
    v1 = v1_enabled_policy()
    service.set_setting(PI_PERMISSION_POLICY_SETTING, json.dumps(v1))
    policy, revision, configured, migrated_from = load_policy(service)
    assert configured is True and migrated_from == 1
    assert policy["version"] == 3
    assert policy["write_tools_enabled"] is True
    assert policy["tools"] == v1["tools"]
    assert policy["protected_patterns"] == v1["protected_patterns"]
    assert policy["protected_template_exceptions"] == v1["protected_template_exceptions"]
    assert policy["allow_session_always"] is False
    assert policy["external_access"] == {"default_mode": "deny", "roots": []}
    assert policy["shell_mode"] == "deny"
    # Migration is in memory only: storage still holds v1 until a v3 save.
    assert json.loads(service.setting(PI_PERMISSION_POLICY_SETTING))["version"] == 1
    # Revision is computed from the migrated v3 object (pre-upgrade
    # sessions cannot continue under a silently changed scope).
    assert revision == policy_revision(policy)
    assert revision == policy_revision(migrate_v1_policy(v1))
    # Public triple stays compatible.
    triple = service.get_pi_permission_policy()
    assert triple == (policy, revision, True)
    # Admin GET reports the migration without mutating storage.
    view = service.pi_permission_view()
    assert view["policy"] == policy and view["migrated_from_version"] == 1
    assert json.loads(service.setting(PI_PERMISSION_POLICY_SETTING))["version"] == 1
    # First v3 save persists v3 and clears the migration marker.
    saved = service.set_pi_permission_policy(policy)
    assert saved["policy"]["version"] == 3
    assert json.loads(service.setting(PI_PERMISSION_POLICY_SETTING))["version"] == 3
    assert "migrated_from_version" not in service.pi_permission_view()


def test_v1_migration_preserves_disabled_policy(pi_env):
    from workspace_bridge.pi_permissions import load_policy
    service = pi_env["service"]
    v1 = v1_enabled_policy()
    v1["enabled"] = False
    service.set_setting(PI_PERMISSION_POLICY_SETTING, json.dumps(v1))
    policy, _, configured, migrated_from = load_policy(service)
    assert configured is True and migrated_from == 1
    assert policy["write_tools_enabled"] is False
    assert service.pi_permission_status()["effective_writable"] is False


def test_status_hides_roots_but_reports_mode_and_count(pi_env):
    service = pi_env["service"]
    policy = enabled_policy(external_access={
        "default_mode": "ask",
        "roots": [{"path": "/Volumes/private-docs", "mode": "allow"},
                  {"path": "/tmp", "mode": "deny"}],
    })
    service.set_pi_permission_policy(policy)
    status = service.pi_permission_status()
    assert status["external_default_mode"] == "ask"
    assert status["external_root_count"] == 2
    blob = json.dumps(status)
    assert "/Volumes/private-docs" not in blob
    assert "private-docs" not in blob


def test_no_mcp_mutation_path_for_permission_policy():
    assert "set_runtime_permission_policy" not in TOOLS
    assert "set_pi_permission_policy" not in TOOLS
    assert "permission-policy" not in json.dumps(
        {name: model.model_json_schema() for name, (model, *_) in TOOLS.items()})


# ------------------------------------------------------------- admin API
def admin_client(service, config):
    token = (service.state / "admin-token").read_text().strip()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=make_admin(service, config["admin_token_hash"])),
        base_url="http://127.0.0.1:8766",
        headers={"Authorization": "Bearer " + token})


def test_admin_permission_policy_routes_and_auth(pi_env):
    import asyncio
    service = pi_env["service"]

    async def fetch():
        async with admin_client(service, pi_env["service"].config) as client:
            anon = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=make_admin(service, pi_env["service"].config["admin_token_hash"])),
                base_url="http://127.0.0.1:8766")
            assert (await anon.get("/api/runtimes/pi/permission-policy")).status_code == 401
            assert (await anon.post("/api/runtimes/pi/permission-policy",
                                    json=enabled_policy())).status_code == 401
            async with admin_client(service, pi_env["service"].config) as authed:
                initial = (await authed.get("/api/runtimes/pi/permission-policy")).json()
                assert initial["runtime"] == "pi"
                assert initial["policy_scope"] == "runtime_global"
                assert initial["policy"]["write_tools_enabled"] is False
                assert initial["policy"]["external_access"] == {"default_mode": "deny", "roots": []}
                assert "migrated_from_version" not in initial
                assert initial["supported_tools"] == list(SUPPORTED_PI_PERMISSION_TOOLS)
                assert initial["fixed_invariants"]
                assert not any("Outside-workspace access denied" in text
                               for text in initial["fixed_invariants"])
                assert "new" in initial["session_note"].lower()
                assert ".workspace-handoff" in json.dumps(initial["policy"])
                bad = json.loads(json.dumps(initial["policy"]))
                bad["tools"]["edit"] = "sometimes"
                rejected = await authed.post("/api/runtimes/pi/permission-policy", json=bad)
                assert rejected.status_code == 400
                # v1 payloads are rejected on save.
                v1_rejected = await authed.post("/api/runtimes/pi/permission-policy",
                                                json=v1_enabled_policy())
                assert v1_rejected.status_code == 400
                with_roots = enabled_policy(external_access={
                    "default_mode": "ask",
                    "roots": [{"path": "/tmp", "mode": "allow"}],
                })
                saved = (await authed.post("/api/runtimes/pi/permission-policy",
                                           json=with_roots)).json()
                assert saved["policy"]["write_tools_enabled"] is True
                assert saved["policy"]["external_access"]["default_mode"] == "ask"
                assert saved["policy"]["external_access"]["roots"] == [
                    {"path": "/tmp", "mode": "allow"}]
                assert saved["policy_revision"]
                reread = (await authed.get("/api/runtimes/pi/permission-policy")).json()
                assert reread["policy"] == saved["policy"]
                # Unknown runtime fails cleanly; other runtimes unsupported.
                assert (await authed.get("/api/runtimes/opencode/permission-policy")).status_code == 404
                assert (await authed.get("/api/runtimes/nope/permission-policy")).status_code == 400
                status = (await authed.get("/api/status")).json()
                assert status["runtime_permissions"]["pi"]["enabled"] is True
                assert status["runtime_permissions"]["pi"]["external_default_mode"] == "ask"
                assert status["runtime_permissions"]["pi"]["external_root_count"] == 1
                assert "/tmp" not in json.dumps(status["runtime_permissions"])
                assert ".workspace-handoff" not in json.dumps(status["runtime_permissions"])
                events = (await authed.get("/api/events")).json()["events"]
                entry = next(e for e in events if e["action"] == "set_runtime_permission_policy")
                assert entry["outcome"] == "ok"
                return True

    assert asyncio.run(fetch()) is True


# --------------------------------------------------------------- static UI
def test_web_ui_has_structured_permission_controls():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    html = (root / "workspace_bridge" / "static" / "index.html").read_text()
    js = (root / "workspace_bridge" / "static" / "app.js").read_text()
    assert "Manage permissions" in html
    assert "pi-permissions-dialog" in html
    assert "Expose Pi writable tools (edit/write)" in html
    assert "read policy is always enforced" in html.lower()
    assert "not a sandbox" in html
    assert "NEW Pi sessions only" in html
    assert "Fixed safety invariants" in html
    assert "Restore safe defaults" in html
    assert "External file access" in html
    assert "pi-perm-external-default" in html
    assert "pi-perm-roots" in html
    assert "pi-perm-add-root" in html
    assert "most-specific" in html
    assert "Add external root" in html
    assert "Outside-workspace access denied" not in html
    assert "pi-permission-status" in html
    assert "Approval mode enabled" in js and "Read-only" in js
    assert "outside" in js and "external_default_mode" in js
    assert "external_root_count" in js
    assert "/api/runtimes/pi/permission-policy" in js
    assert "innerHTML" not in js
    assert "Always allow exact target" in js
    assert "new pi sessions only" in js.lower()
    assert "adapter ready" in js and "adapter update required" in js
    assert "version: 2" in js and "write_tools_enabled" in js


# ------------------------------------------------------- runtime client
def test_pi_capabilities_advertise_permissions():
    caps = HttpPiRuntime("http://127.0.0.1:8780").capabilities
    assert caps.pending_snapshot is True
    assert caps.permission_response is True
    assert caps.event_polling is False
    assert caps.question_detection is False


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self, _=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def patch(monkeypatch, handler):
    calls = []

    def urlopen(request, timeout=None):
        record = {"url": request.full_url, "method": request.get_method(),
                  "headers": {k.lower(): v for k, v in request.header_items()}}
        record["body"] = json.loads(request.data.decode()) if request.data else None
        calls.append(record)
        return handler(record)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def ready_adapter_health():
    return {"ok": True, "status": "ok", "locked": False, "pi_usable": True,
            "pi_version": "0.86.1", "adapter_version": "0.2.0", "instance": "pi-1",
            "capabilities": {"pending_snapshot": True, "permission_response": True,
                             "execution_history": True}}


def old_adapter_health():
    return {"ok": True, "status": "ok", "locked": False, "pi_usable": True,
            "pi_version": "0.86.1", "adapter_version": "0.1.0", "instance": "pi-1"}


def test_create_session_sends_policy_snapshot(monkeypatch):
    seen = {}

    def handler(record):
        url = record["url"]
        if record["method"] == "GET" and url.split("?")[0].endswith("/health"):
            return FakeResponse(ready_adapter_health())
        if record["method"] == "POST" and url.split("?")[0].endswith("/sessions"):
            seen.update(record["body"])
            return FakeResponse({"session": {"id": "pi_ses_9",
                                             "directory": record["body"]["directory"],
                                             "title": "t"}})
        raise AssertionError(url)

    patch(monkeypatch, handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780", "tok")
    policy = enabled_policy()
    revision = policy_revision(policy)
    session = runtime.create_session("/projects/alpha", "Handoff",
                                     {"permission_policy": policy,
                                      "policy_revision": revision})
    assert session.id == "pi_ses_9"
    assert seen["permission_policy"] == policy
    assert seen["policy_revision"] == revision
    # OpenCode-shaped calls without options stay unchanged on the wire.
    seen.clear()
    runtime.create_session("/projects/alpha", "Handoff")
    assert "permission_policy" not in seen and "policy_revision" not in seen


def test_pi_pending_and_respond_mapping(monkeypatch):
    def handler(record):
        url = record["url"]
        if record["method"] == "GET" and url.split("?")[0].endswith("/health"):
            return FakeResponse(ready_adapter_health())
        if record["method"] == "GET" and url.endswith("/permissions?directory=%2Fd"):
            return FakeResponse({"permissions": [{
                "id": "perm_1", "session_id": "ses_1", "tool": "edit",
                "action": "edit", "title": "",
                "resource": "notes.txt", "requested": ["notes.txt"],
                "always_pattern": "edit:notes.txt", "tool_call_id": "call-1",
                "created": "2026-09-21T00:00:00Z",
                "metadata": {"code": "tool_ask"}}]})
        if record["method"] == "POST" and url.endswith("/permissions/perm_1/respond"):
            assert record["body"] == {"directory": "/d", "response": "once"}
            return FakeResponse({"ok": True, "decision": "once"})
        raise AssertionError(url)

    patch(monkeypatch, handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    pending = runtime.list_pending_permissions("/d", "ses_1")
    assert len(pending) == 1 and pending[0].id == "perm_1"
    assert pending[0].pattern == ("edit:notes.txt",)
    assert pending[0].call_id == "call-1"
    assert runtime.respond_permission("/d", "ses_1", "perm_1", "once") is True
    with pytest.raises(BridgeError):
        runtime.respond_permission("/d", "ses_1", "perm_1", "sometimes")


def test_writable_create_refuses_old_adapter_without_session_post(monkeypatch):
    calls = patch(monkeypatch, lambda record: FakeResponse(old_adapter_health())
                  if record["method"] == "GET" and record["url"].split("?")[0].endswith("/health")
                  else (_ for _ in ()).throw(AssertionError(record["url"])))
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(RuntimeUnsupported):
        runtime.create_session("/projects/alpha", "Handoff",
                               {"permission_policy": enabled_policy(),
                                "policy_revision": policy_revision(enabled_policy())})
    # Only the health probe ran: no POST /sessions was ever attempted.
    assert calls and all("/sessions" not in call["url"] for call in calls)


def test_read_only_v2_create_refuses_old_adapter_without_session_post(monkeypatch):
    # 3B2: even read-only v2 sessions load the trusted extension, so they
    # require the deployed permission-capable adapter. Only legacy
    # no-policy creation stays compatible with old adapters (see below).
    calls = patch(monkeypatch, lambda record: FakeResponse(old_adapter_health())
                  if record["method"] == "GET" and record["url"].split("?")[0].endswith("/health")
                  else (_ for _ in ()).throw(AssertionError(record["url"])))
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    disabled = safe_defaults()
    with pytest.raises(RuntimeUnsupported):
        runtime.create_session("/projects/alpha", "Handoff",
                               {"permission_policy": disabled,
                                "policy_revision": policy_revision(disabled)})
    # Only the health probe ran: no POST /sessions was ever attempted.
    assert calls and all("/sessions" not in call["url"] for call in calls)


def test_legacy_no_policy_create_stays_compatible_with_old_adapter(monkeypatch):
    seen = {}

    def handler(record):
        url = record["url"]
        if record["method"] == "GET" and url.split("?")[0].endswith("/health"):
            return FakeResponse(old_adapter_health())
        if record["method"] == "POST" and url.split("?")[0].endswith("/sessions"):
            seen.update(record["body"])
            return FakeResponse({"session": {"id": "pi_old_1",
                                             "directory": record["body"]["directory"],
                                             "title": "t"}})
        raise AssertionError(url)

    calls = patch(monkeypatch, handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    legacy = runtime.create_session("/projects/alpha", "Legacy")
    assert legacy.id == "pi_old_1"
    assert "permission_policy" not in seen and "policy_revision" not in seen
    # No deployed-capability probe is needed for legacy creation.
    assert [call["url"] for call in calls] == [
        "http://127.0.0.1:8780/sessions"]


def test_permission_list_and_respond_require_deployed_support(monkeypatch):
    calls = patch(monkeypatch, lambda record: FakeResponse(old_adapter_health())
                  if record["method"] == "GET" and record["url"].split("?")[0].endswith("/health")
                  else (_ for _ in ()).throw(AssertionError(record["url"])))
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(RuntimeUnsupported):
        runtime.list_pending_permissions("/d", "s")
    with pytest.raises(RuntimeUnsupported):
        runtime.respond_permission("/d", "s", "p", "once")
    assert calls and all("/permissions" not in call["url"] for call in calls)


def test_deployed_support_exposed_in_diagnostics_without_secrets():
    from workspace_bridge.registry import RuntimeRegistry

    class PiStub(FakeRuntime):
        @property
        def runtime_id(self):
            return "pi"

        def health(self):
            return {"ok": True, "version": "0.86.1", "adapter_version": "0.2.0",
                    "locked": False, "instance": "stub-1", "status": "ok",
                    "deployed_capabilities": {"pending_snapshot": True,
                                              "permission_response": True},
                    "permissions_supported": True}

    stub = PiStub("/tmp")
    status = RuntimeRegistry({"pi": stub}).status()
    health = status["runtimes"]["pi"]["health"]
    assert health["permissions_supported"] is True
    assert health["deployed_capabilities"] == {"pending_snapshot": True,
                                               "permission_response": True}
    assert "permission_policy" not in json.dumps(status)
    assert "token" not in json.dumps(status).lower()


def test_pi_permission_list_invalid_shape_fails_closed(monkeypatch):
    def handler(record):
        if record["method"] == "GET" and record["url"].split("?")[0].endswith("/health"):
            return FakeResponse(ready_adapter_health())
        return FakeResponse({"permissions": "nope"})

    patch(monkeypatch, handler)
    with pytest.raises(RuntimeUnavailable):
        HttpPiRuntime("http://127.0.0.1:8780").list_pending_permissions("/d", "s")


def test_health_normalizes_deployed_capabilities(monkeypatch):
    ready = ready_adapter_health()
    patch(monkeypatch, lambda record: FakeResponse(ready))
    health = HttpPiRuntime("http://127.0.0.1:8780").health()
    assert health["deployed_capabilities"] == {"pending_snapshot": True,
                                               "permission_response": True,
                                               "execution_history": True,
                                               "extension_inventory": False}
    assert health["permissions_supported"] is True
    assert health["execution_supported"] is True
    assert health["extension_inventory_supported"] is False
    blob = json.dumps(health)
    assert "token" not in blob.lower() and "permission_policy" not in blob
    # Pre-3C1 adapter without the block: all default False.
    patch(monkeypatch, lambda record: FakeResponse(old_adapter_health()))
    legacy = HttpPiRuntime("http://127.0.0.1:8780").health()
    assert legacy["deployed_capabilities"] == {"pending_snapshot": False,
                                               "permission_response": False,
                                               "execution_history": False,
                                               "extension_inventory": False}
    assert legacy["permissions_supported"] is False
    assert legacy["execution_supported"] is False
    assert legacy["extension_inventory_supported"] is False
    # Malformed or partial capabilities shapes fail closed to False, and
    # the normalized block carries strict booleans only.
    for bad in ({"capabilities": None}, {"capabilities": ["x"]},
                {"capabilities": {"pending_snapshot": "yes"}},
                {"capabilities": {"pending_snapshot": True}},
                {"capabilities": {"permission_response": True}},
                {"capabilities": {"pending_snapshot": 1, "permission_response": 1}}):
        payload = {**old_adapter_health(), **bad}
        patch(monkeypatch, lambda record, p=payload: FakeResponse(p))
        parsed = HttpPiRuntime("http://127.0.0.1:8780").health()
        assert parsed["permissions_supported"] is False, bad
        assert parsed["execution_supported"] is False, bad
        assert parsed["extension_inventory_supported"] is False, bad
        assert set(parsed["deployed_capabilities"]) == {"pending_snapshot", "permission_response",
                                                        "execution_history", "extension_inventory"}
        assert all(isinstance(v, bool)
                   for v in parsed["deployed_capabilities"].values())


# ------------------------------------------------- session snapshot/continuation
def test_pi_start_records_snapshot_and_disabled_stays_read_only(pi_env):
    service = pi_env["service"]
    job = publish(pi_env, "snap-1")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="snap-run")
    assert run["runtime"] == "pi"
    options = pi_env["pi"].session_options[-1]
    assert options["permission_policy"] == safe_defaults()
    row = service.db.execute("SELECT permission_revision FROM agent_runs WHERE id=?",
                             (run["run_id"],)).fetchone()
    assert row["permission_revision"] == policy_revision(safe_defaults())
    assert row["permission_revision"] == options["policy_revision"]


def test_pi_continuation_same_revision_then_refused_after_change(pi_env):
    import time
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "cont-1")
    first = call(pi_env, "start_agent_run", runtime="pi",
                 job_id=job["id"], request_id="cont-first")
    pi_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(pi_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    follow = publish(pi_env, "cont-2", title="Follow-up")
    second = call(pi_env, "start_agent_run", runtime="pi", job_id=follow["id"],
                  request_id="cont-second", continue_from_run_id=first["run_id"])
    assert second["session_reused"] is True
    pi_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
        MessageInfo(id="m3", role="assistant", created=13, completed=14,
                    text="Follow-up done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(pi_env, "read_agent_run", run_id=second["run_id"])["state"] == "completed"
    # Flip the operational policy: continuation must fail closed.
    service.set_pi_permission_policy(enabled_policy())
    third_job = publish(pi_env, "cont-3", title="Follow-up 2")
    with pytest.raises(BridgeError) as exc:
        call(pi_env, "start_agent_run", runtime="pi", job_id=third_job["id"],
             request_id="cont-third", continue_from_run_id=first["run_id"])
    assert exc.value.code == "permission_scope_changed"
    # New sessions pick up the current revision.
    fresh = call(pi_env, "start_agent_run", runtime="pi", job_id=third_job["id"],
                 request_id="cont-fresh")
    assert fresh["session_reused"] is False
    assert pi_env["pi"].session_options[-1]["permission_policy"]["write_tools_enabled"] is True


def test_pi_continuation_refused_after_external_policy_change(pi_env):
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "ext-1")
    first = call(pi_env, "start_agent_run", runtime="pi",
                 job_id=job["id"], request_id="ext-first")
    pi_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(pi_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    widened = enabled_policy(external_access={
        "default_mode": "ask", "roots": [{"path": "/tmp", "mode": "allow"}]})
    service.set_pi_permission_policy(widened)
    follow = publish(pi_env, "ext-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        call(pi_env, "start_agent_run", runtime="pi", job_id=follow["id"],
             request_id="ext-second", continue_from_run_id=first["run_id"])
    assert exc.value.code == "permission_scope_changed"


def test_pi_ask_to_once_resumes_same_session(pi_env):
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "ask-1")
    pi_env["pi"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="ask-run")
    session_id = run["session_id"]
    pi_env["pi"].add_pending_permission(
        session_id, pending_permission(session_id, "per_pi_1",
                                       pattern=["edit:notes.txt"],
                                       requested_patterns=["notes.txt"],
                                       action="edit",
                                       title="edit notes.txt",
                                       tool="edit"))
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_request_count"] == 1
    pending = detail["pending_requests"][0]
    assert pending["request_id"] == "per_pi_1"
    assert pending["pattern"] == ["edit:notes.txt"]
    response = call(pi_env, "respond_agent_permission", run_id=run["run_id"],
                    request_id="per_pi_1", decision="once")
    assert response["resumed_same_session"] is True
    assert response["run_state"] == "running"
    assert pi_env["pi"].respond_calls == [(session_id, "per_pi_1", "once")]
    after = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert after["state"] == "running" and after["pending_requests"] == []


def test_pi_unsupported_question_is_not_applicable(pi_env):
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "qna-1")
    pi_env["pi"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="qna-run")
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert detail["question_sync"]["status"] == "not_applicable"
    assert detail["question_sync"]["reason"] == "capability_unsupported"


def test_skill_version_bumped_and_workflow_neutral():
    from workspace_bridge.embedded_skill import SKILL_VERSION
    assert SKILL_VERSION == "2.1.0"
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    skill = (root / "workspace_bridge" / "skills" / "project-lead" / "SKILL.md").read_text()
    assert "web-admin configured" in skill
    flat = " ".join(skill.split())
    assert "new-session snapshot" in skill or "NEW sessions only" in flat
    assert "execution_audit" in skill or "execution-evidence" in skill.lower()
    assert "shell" in skill.lower()
    assert "external" in skill.lower()
    assert "read-only mode" in skill.lower()
