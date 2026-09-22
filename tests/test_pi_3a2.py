"""Milestone 3A2: HttpPiRuntime transport, RuntimeRegistry, multi-runtime service.

Focused coverage for the Bridge-side Pi HTTP client and internal runtime
registry. All transport goes through a fake HTTP server: no Node, network,
provider credentials, or real model is required. No public MCP surface is
added in 3A2, so these tests also pin zero new public tools.
"""
import json
import urllib.error
import urllib.request

import pytest

from workspace_bridge.api import Handoff, TOOLS
from workspace_bridge.registry import RuntimeRegistry, runtime_registry_from_environment
from workspace_bridge.runtime import (HttpOpenCodeRuntime, HttpPiRuntime, OPENCODE_RUNTIME_ID,
                                       PI_RUNTIME_ID, RuntimeRejected, RuntimeUnavailable,
                                       RuntimeUnsupported, runtime_from_environment)
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, RecordingNotifier


# ------------------------------------------------------------- fake transport
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
        record = {"url": request.full_url, "method": request.get_method(), "timeout": timeout,
                  "headers": {k.lower(): v for k, v in request.header_items()}}
        record["body"] = json.loads(request.data.decode()) if request.data else None
        calls.append(record)
        result = handler(record)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def http_error(url, code, message="error"):
    return urllib.error.HTTPError(url, code, message, {}, None)


# ------------------------------------------------------------------ identity
def test_pi_identity_and_conservative_capabilities():
    assert PI_RUNTIME_ID == "pi"
    assert OPENCODE_RUNTIME_ID == "opencode"
    caps = HttpPiRuntime("http://127.0.0.1:8780").capabilities
    assert caps.model_discovery is True
    assert caps.session_reuse is True
    assert caps.session_status is True
    assert caps.event_polling is False
    # 3B1: the deployed adapter exposes exact-session permission
    # snapshots and replies; events and questions stay unsupported.
    assert caps.pending_snapshot is True
    assert caps.permission_response is True
    assert caps.question_detection is False
    assert caps.question_response is False
    assert caps.session_branching is False


def test_pi_url_validation():
    with pytest.raises(BridgeError):
        HttpPiRuntime("file:///etc/passwd")
    with pytest.raises(BridgeError):
        HttpPiRuntime("")
    assert HttpPiRuntime("http://127.0.0.1:8780").base_url == "http://127.0.0.1:8780"


# ------------------------------------------------------------------ transport
def pi_handler(record):
    url = record["url"]
    if record["method"] == "GET" and url.split("?")[0].endswith("/health"):
        return FakeResponse({"ok": True, "status": "ok", "locked": False,
                             "token_configured": True, "pi_configured": True,
                             "pi_usable": True, "pi_version": "0.86.1",
                             "projects_configured": True,
                             "adapter_version": "0.1.0", "instance": "pi-1",
                             "sessions": 2})
    if record["method"] == "GET" and "/models?" in url:
        assert "directory=" in url
        return FakeResponse({"models": [
            {"provider": "pi", "id": "default", "name": "Pi Default"},
            {"provider": "anthropic", "id": "claude-opus", "name": ""},
            {"no": "provider"},
        ], "scope": "global"})
    if record["method"] == "POST" and url.split("?")[0].endswith("/sessions"):
        return FakeResponse({"session": {"id": "pi_ses_1",
                                         "directory": record["body"]["directory"],
                                         "title": record["body"]["title"]}})
    if record["method"] == "GET" and "/sessions/pi_ses_1?" in url:
        return FakeResponse({"session": {"id": "pi_ses_1", "directory": "/projects/alpha",
                                         "title": "t"},
                             "status": "idle", "state": {"isStreaming": False}})
    if record["method"] == "GET" and "/sessions/pi_ses_1/status" in url:
        return FakeResponse({"status": "idle"})
    if record["method"] == "POST" and url.endswith("/prompt-async"):
        return FakeResponse({"accepted": True})
    if record["method"] == "GET" and "/sessions/pi_ses_1/messages" in url:
        return FakeResponse({"messages": [
            {"id": "m1", "role": "assistant", "created": 10, "completed": 12,
             "text": "read two files", "tools": ["read"],
             "content": [{"type": "thinking", "text": "hidden reasoning"},
                         {"type": "toolCall", "name": "read",
                          "arguments": {"secret": "sk-hidden"}}]},
        ]})
    if record["method"] == "POST" and url.endswith("/abort"):
        return FakeResponse({"ok": True})
    raise AssertionError(url)


def test_pi_health_models_and_session_calls(monkeypatch):
    calls = patch(monkeypatch, pi_handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780", "shared-token")
    health = runtime.health()
    assert health == {"ok": True, "version": "0.86.1", "adapter_version": "0.1.0",
                      "locked": False, "instance": "pi-1", "status": "ok",
                      "deployed_capabilities": {"pending_snapshot": False,
                                                "permission_response": False,
                                                "execution_history": False,
                                                "extension_inventory": False},
                      "permissions_supported": False,
                      "execution_supported": False,
                      "extension_inventory_supported": False}
    blob = json.dumps(health)
    assert "token" not in blob.lower() and "/projects" not in blob and "sessions" not in blob
    models = runtime.list_models("/projects/alpha")
    assert [m.selector for m in models] == ["pi/default", "anthropic/claude-opus"]
    assert models[1].name == "claude-opus" and models[0].default is False
    session = runtime.create_session("/projects/alpha", "Handoff")
    assert session.id == "pi_ses_1" and session.directory == "/projects/alpha"
    assert runtime.get_session("/projects/alpha", "pi_ses_1").id == "pi_ses_1"
    assert runtime.session_status("/projects/alpha", "pi_ses_1") == "idle"
    runtime.prompt_async("/projects/alpha", "pi_ses_1", "hello",
                         {"providerID": "pi", "modelID": "default"})
    messages = runtime.messages("/projects/alpha", "pi_ses_1")
    assert messages[0].text == "read two files" and messages[0].tools == ("read",)
    assert "hidden" not in json.dumps([m.text for m in messages])
    assert runtime.abort_session("/projects/alpha", "pi_ses_1") is True
    assert all(call["headers"].get("x-runtime-token") == "shared-token" for call in calls)
    assert "secret" not in json.dumps(calls).lower()


def test_pi_models_require_directory_fail_closed(monkeypatch):
    calls = patch(monkeypatch, pi_handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(BridgeError):
        runtime.list_models(None)
    with pytest.raises(BridgeError):
        runtime.list_models("")
    assert calls == []


def test_opencode_models_accept_and_ignore_directory(monkeypatch):
    def handler(record):
        assert record["url"].endswith("/models")
        return FakeResponse({"models": []})

    calls = patch(monkeypatch, handler)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    assert runtime.list_models() == []
    assert runtime.list_models(None) == []
    assert runtime.list_models("/projects/alpha") == []
    assert all("directory" not in call["url"] for call in calls)


def test_pi_get_session_none_only_on_404(monkeypatch):
    patch(monkeypatch, lambda record: http_error(record["url"], 404))
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    assert runtime.get_session("/d", "missing") is None
    patch(monkeypatch, lambda record: http_error(record["url"], 500))
    with pytest.raises(RuntimeUnavailable):
        runtime.get_session("/d", "missing")


def test_pi_get_session_malformed_success_raises(monkeypatch):
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    for payload in ({}, {"no": "session"}, {"session": {}},
                    {"session": {"id": ""}}, {"session": {"id": None}},
                    {"session": "not-a-dict"}, []):
        patch(monkeypatch, lambda record, p=payload: FakeResponse(p))
        with pytest.raises(RuntimeUnavailable):
            runtime.get_session("/d", "s")
    # A valid 200 still returns SessionInfo.
    patch(monkeypatch, lambda record: FakeResponse(
        {"session": {"id": "pi_ses_9", "directory": "/projects/alpha", "title": "t"}}))
    session = runtime.get_session("/projects/alpha", "pi_ses_9")
    assert session is not None and session.id == "pi_ses_9"
    assert session.directory == "/projects/alpha"


@pytest.mark.parametrize("status,expected", [(400, RuntimeRejected), (404, RuntimeRejected),
                                             (409, RuntimeRejected), (501, RuntimeUnsupported),
                                             (500, RuntimeUnavailable)])
def test_pi_error_status_mapping(monkeypatch, status, expected):
    patch(monkeypatch, lambda record: http_error(record["url"], status))
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(expected):
        runtime.abort_session("/d", "ses_1")


def test_pi_errors_are_bounded_without_raw_bodies(monkeypatch):
    def handler(record):
        raise urllib.error.HTTPError(record["url"], 400, "bad", {},
                                     FakeBody())
    patch(monkeypatch, handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(RuntimeRejected) as exc:
        runtime.abort_session("/d", "ses_1")
    # Bounded adapter detail only: the 8000-char raw body never surfaces.
    assert len(str(exc.value)) <= 320
    assert exc.value.status == 400


class FakeBody:
    def read(self, _=None):
        return b'{"error": "' + b"PAD-" * 2000 + b'"}'

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_pi_unsupported_capabilities_make_zero_http_calls(monkeypatch):
    # 3B1: event polling and questions stay unsupported (no HTTP call);
    # permission snapshot/reply support is covered by the 3B1 tests.
    calls = patch(monkeypatch, pi_handler)
    runtime = HttpPiRuntime("http://127.0.0.1:8780")
    with pytest.raises(RuntimeUnsupported):
        runtime.poll_events(0)
    with pytest.raises(RuntimeUnsupported):
        runtime.list_pending_questions("/d", "s")
    assert calls == []


# ------------------------------------------------------------------- registry
def test_registry_duplicate_and_empty_ids_rejected():
    registry = RuntimeRegistry()
    first = FakeRuntime("/tmp")
    registry.register(first)
    with pytest.raises(BridgeError):
        registry.register(FakeRuntime("/tmp"))
    broken = FakeRuntime("/tmp")
    broken._runtime_id = ""
    with pytest.raises(BridgeError):
        registry.register(broken)
    with pytest.raises(BridgeError):
        registry.get("nope")
    assert registry.optional("nope") is None
    assert registry.get("opencode") is first


def test_registry_matching_explicit_id_succeeds():
    registry = RuntimeRegistry()
    runtime = FakeRuntime("/tmp")
    assert registry.register(runtime, runtime_id="opencode") is runtime
    assert registry.get("opencode") is runtime


def test_registry_rejects_identity_mismatch():
    registry = RuntimeRegistry()
    pi = FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    with pytest.raises(BridgeError) as exc:
        registry.register(pi, runtime_id="opencode")
    assert exc.value.code == "runtime_mismatch"
    with pytest.raises(BridgeError) as exc:
        registry.register(FakeRuntime("/tmp"), runtime_id="pi")
    assert exc.value.code == "runtime_mismatch"
    # Nothing was registered by the failed calls.
    assert registry.ids() == []


def test_registry_rejects_empty_or_unimplemented_identity_even_with_key():
    registry = RuntimeRegistry()
    broken = FakeRuntime("/tmp")
    broken._runtime_id = ""
    with pytest.raises(BridgeError):
        registry.register(broken, runtime_id="pi")
    with pytest.raises(BridgeError):
        registry.register(broken)

    class NoIdentity(FakeRuntime):
        @property
        def runtime_id(self):
            raise NotImplementedError

    with pytest.raises(BridgeError):
        registry.register(NoIdentity("/tmp"), runtime_id="pi")
    with pytest.raises(BridgeError):
        registry.register(NoIdentity("/tmp"))
    with pytest.raises(BridgeError):
        registry.register(FakeRuntime("/tmp"), runtime_id="  ")
    assert registry.ids() == []


def test_registry_dict_constructor_enforces_match():
    pi = FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    with pytest.raises(BridgeError):
        RuntimeRegistry({"opencode": pi})
    assert RuntimeRegistry({"pi": pi}).ids() == ["pi"]


def test_service_runtimes_mapping_must_match_identity(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    mismatched = FakeRuntime(str(root))  # runtime_id "opencode", key "pi"
    with pytest.raises(BridgeError):
        Service(state, cfg, runtimes={"pi": mismatched},
                notifier=RecordingNotifier(), orchestrator_background=False)
    matched = FakeRuntime(str(root))
    matched._runtime_id = "pi"
    service = Service(state, cfg, runtimes={"pi": matched},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        assert service.orchestrators["pi"].runtime is matched
    finally:
        service.close()


def test_registry_close_is_safe_and_complete():
    closed = []

    class Closing(FakeRuntime):
        def close(self):
            closed.append(self._runtime_id)

    class Failing(FakeRuntime):
        def close(self):
            raise RuntimeError("boom")

    opencode, pi = Closing("/tmp"), Failing("/tmp")
    pi._runtime_id = "pi"
    registry = RuntimeRegistry({"opencode": opencode, "pi": pi})
    registry.close()
    assert closed == ["opencode"]


def test_environment_factory_either_both_neither_without_network(monkeypatch):
    calls = patch(monkeypatch, lambda record: (_ for _ in ()).throw(AssertionError("no network")))
    assert runtime_registry_from_environment({}).ids() == []
    only_open = runtime_registry_from_environment({"WB_OPENCODE_RUNTIME_URL": "http://a:8770",
                                                   "WB_RUNTIME_TOKEN": "tok"})
    assert only_open.ids() == ["opencode"]
    assert only_open.get("opencode").token == "tok"
    only_pi = runtime_registry_from_environment({"WB_PI_RUNTIME_URL": "http://127.0.0.1:8780",
                                                 "WB_RUNTIME_TOKEN": "tok"})
    assert only_pi.ids() == ["pi"]
    assert only_pi.get("pi").token == "tok"
    both = runtime_registry_from_environment({"WB_OPENCODE_RUNTIME_URL": "http://a:8770",
                                              "WB_PI_RUNTIME_URL": "http://127.0.0.1:8780",
                                              "WB_RUNTIME_TOKEN": "tok"})
    assert both.ids() == ["opencode", "pi"]
    assert calls == []
    # runtime_from_environment stays OpenCode-only compatibility behavior.
    assert runtime_from_environment({}) is None
    legacy = runtime_from_environment({"WB_OPENCODE_RUNTIME_URL": "http://a:8770",
                                       "WB_PI_RUNTIME_URL": "http://127.0.0.1:8780",
                                       "WB_RUNTIME_TOKEN": "tok"})
    assert isinstance(legacy, HttpOpenCodeRuntime)
    assert "127.0.0.1" not in legacy.base_url


# -------------------------------------------------------------------- service
def make_service(tmp_path, runtimes):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    service = Service(state, cfg, registry=RuntimeRegistry(dict(runtimes)),
                      notifier=RecordingNotifier(), orchestrator_background=False)
    return service, root, parent


def test_service_dual_orchestrators_and_compat_path(tmp_path):
    opencode, pi = FakeRuntime("/tmp"), FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    service, _, _ = make_service(tmp_path, {"opencode": opencode, "pi": pi})
    try:
        assert set(service.orchestrators) == {"opencode", "pi"}
        assert service.orchestrator is service.orchestrators["opencode"]
        assert service.orchestrator.runtime is opencode
        assert service.orchestrators["pi"].runtime is pi
        assert service.orchestrator_for_runtime("pi").runtime is pi
        with pytest.raises(BridgeError):
            service.orchestrator_for_runtime("other")
        with pytest.raises(BridgeError):
            service.orchestrator_for_runtime("")
    finally:
        service.close()


def test_service_without_opencode_keeps_compat_orchestrator(tmp_path):
    pi = FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    service, _, _ = make_service(tmp_path, {"pi": pi})
    try:
        assert set(service.orchestrators) == {"pi"}
        assert service.orchestrator.runtime is None
        assert service.orchestrator.configured is False
        with pytest.raises(BridgeError) as exc:
            service.orchestrator._require_runtime()
        assert exc.value.code == "runtime_unavailable"
    finally:
        service.close()


def test_orchestrator_for_run_routes_by_persisted_runtime(tmp_path):
    opencode, pi = FakeRuntime("/tmp"), FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    service, root, _ = make_service(tmp_path, {"opencode": opencode, "pi": pi})
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        service.manage_workspace(ws_id, "enable")
        token = service.manage_bridge("rotate_token")["token"]
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate({"request_id": "route-1", "title": "Route",
                                                   "goal": "g", "plan": "p", "acceptance": "a",
                                                   "constraints": "c", "context": "x",
                                                   "context_hashes": {}}).model_dump())
        with service.lock, service.db:
            service.db.execute(
                "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
                "parent_run,session,model,state,error_code,error_message,result,notification,"
                "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_pi_1", ws_id, "pi", job["id"], "req-1", "hash", None, "ses_pi",
                 "pi/default", "running", None, None, "{}", "{}",
                 "2026-09-21T00:00:00+00:00", None, "2026-09-21T00:00:00+00:00",
                 None, 0, 0, "[]"))
        assert service.orchestrator_for_run("run_pi_1").runtime is pi
        assert service.orchestrator_for_run("run_pi_1", ws_id).runtime is pi
        with pytest.raises(BridgeError):
            service.orchestrator_for_run("run_missing")
        # OpenCode never owns the Pi row and vice versa.
        assert service.orchestrators["opencode"]._is_owned_run({"runtime": "pi"}) is False
        assert service.orchestrators["pi"]._is_owned_run({"runtime": "pi"}) is True
    finally:
        service.close()


def test_service_close_stops_all_orchestrators_once(tmp_path):
    opencode, pi = FakeRuntime("/tmp"), FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    service, _, _ = make_service(tmp_path, {"opencode": opencode, "pi": pi})
    stops = []
    for orch in service.orchestrators.values():
        original = orch.stop
        stops.append(orch)

        def counting_stop(o=orch, orig=original):
            o._stopped_count = getattr(o, "_stopped_count", 0) + 1
            return orig()

        orch.stop = counting_stop
    service.close()
    assert [getattr(o, "_stopped_count", 0) for o in stops] == [1, 1]


def test_pi_policy_isolated_and_unconfigured(tmp_path):
    opencode = FakeRuntime("/tmp")
    pi = FakeRuntime("/tmp")
    pi._runtime_id = "pi"
    service, root, _ = make_service(tmp_path, {"opencode": opencode, "pi": pi})
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        service.orchestrators["opencode"].set_model_policy(
            ["anthropic/claude-sonnet"], "anthropic/claude-sonnet")
        # Legacy key behavior is unchanged for OpenCode.
        assert service.orchestrator.model_policy_status()["configured"] is True
        pi_orch = service.orchestrators["pi"]
        assert pi_orch._policy_setting() == "model_policy:pi"
        assert pi_orch.model_policy_status() == {"configured": False, "enabled": [],
                                                 "default": None, "enabled_count": 0}
        assert pi_orch.get_model_policy() is None
        ws = service.workspace(ws_id)
        job = service.call(ws_id, service.manage_bridge("rotate_token")["token"],
                           "prepare_handoff",
                           Handoff.model_validate({"request_id": "pi-pol-1", "title": "Pi",
                                                   "goal": "g", "plan": "p", "acceptance": "a",
                                                   "constraints": "c", "context": "x",
                                                   "context_hashes": {}}).model_dump())
        with pytest.raises(BridgeError) as exc:
            pi_orch.start_run(ws, job["id"], "pi-run-1")
        assert exc.value.code == "model_policy_unconfigured"
        assert pi.sessions == [] and opencode.sessions == []
    finally:
        service.close()


def test_no_new_public_mcp_tools_in_3a2():
    # 3A2 pinned zero new public tools; 3A3 adds the seven neutral agent
    # tools and 3C1 adds the two execution-audit tools alongside the
    # unchanged OpenCode compatibility surface.
    from workspace_bridge.service import NEUTRAL_AGENT_TOOLS
    assert NEUTRAL_AGENT_TOOLS == {"list_agent_models", "start_agent_run", "list_agent_runs",
                                   "read_agent_run", "read_agent_request",
                                   "respond_agent_permission", "cancel_agent_run",
                                   "list_agent_executions", "read_agent_execution"}
    assert NEUTRAL_AGENT_TOOLS <= set(TOOLS)
    assert "set_model_policy" not in TOOLS
    for name in TOOLS:
        assert not name.startswith("start_pi") and not name.startswith("list_pi")


def test_compose_pi_url_empty_by_default_no_pi_service():
    import yaml
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "compose.yaml").read_text())
    assert set(cfg["services"]) == {"bridge", "mcp-tunnel", "opencode-adapter"}
    env = cfg["services"]["bridge"]["environment"]
    assert "WB_PI_RUNTIME_URL" in env
    assert "host.docker.internal" in (root / "compose.yaml").read_text()


def test_admin_status_additive_runtimes(tmp_path):
    import asyncio
    import httpx
    from workspace_bridge.api import make_admin
    opencode = FakeRuntime("/tmp")
    service, _, _ = make_service(tmp_path, {"opencode": opencode})
    try:
        token = (service.state / "admin-token").read_text().strip()

        async def fetch():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=make_admin(service, service.config["admin_token_hash"])),
                    base_url="http://127.0.0.1:8766",
                    headers={"Authorization": "Bearer " + token}) as client:
                return (await client.get("/api/status")).json()

        status = asyncio.run(fetch())
        # Old fields stay intact.
        assert status["opencode"]["configured"] is True
        assert status["model_policy"]["configured"] is False
        # Additive bounded diagnostics.
        assert status["runtimes"]["configured"] == ["opencode"]
        assert status["runtimes"]["runtimes"]["opencode"]["healthy"] is True
        assert "token" not in json.dumps(status).lower()
    finally:
        service.close()
